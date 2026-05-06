"""Backfill ROI state + event log from a Tesla account data export.

Expects the directory layout produced by Tesla's "Download My Data":
    <root>/
      Vehicle Data/<YYYY-MM-DD>.csv      # high-frequency telemetry
      Charging Data/Charging Data.csv    # session summary
      Vehicle Details/Vehicle Details.csv

CSV quirks handled:
- Multi-state cells like "SNA, 615.27" or "DI_GEAR_D, 1.0" — we split on
  the first comma and parse a number on the right when present.
- Speed is in kph, odometer in km — converted to mph / mi.
- "Identity of the Active Key Device" is a numeric key ID. Map keys to
  driver names with --key-map "id=Carson,id=Other" (the most-frequent key
  defaults to "Carson" if no map is supplied).

Hard-brake / rapid-accel thresholds use the longitudinal acceleration
column directly (m/s²), which is more accurate than speed deltas.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from tesla_fleet import db
from tesla_fleet.monitor import RoiState

logger = logging.getLogger(__name__)

KM_TO_MI = 0.621371
KPH_TO_MPH = 0.621371

# m/s² thresholds (≈ ±0.3 g)
HARD_BRAKE_MS2 = -3.0
RAPID_ACCEL_MS2 = 3.0

SPEED_COL = "Vehicle Speed (kph) (Positive is forward direction)"
ACCEL_COL = "Longitudinal Acceleration (m/s^2) (positive indicates forward)"
ODO_COL = "Odometer (Kilometers)"
GEAR_COL = "Gear Selection"
KEY_COL = "Identity of the Active Key Device"
DATE_COL = "DATE (UTC)"
AP_STATE_COL = (
    "Autopilot State (Unavailable is recorded when Autopilot is not "
    "available, SNA is recorded when system state is not available)"
)
ACCEL_MODE_COL = "UI Setting - Acceleration Mode"
STEERING_MODE_COL = "UI Setting - Steering Mode"
STOPPING_MODE_COL = "UI Setting - Stopping Mode "  # trailing space is in the CSV header
NAV_ON_AP_COL = "UI Setting - Navigate on Autopilot"


def _normalize_state(raw: str | None, *, drop_prefix: str = "") -> str | None:
    """Strip Tesla's enum prefixes, e.g. 'STEERING_TUNE_STANDARD' → 'STANDARD'."""
    if not raw:
        return None
    s = raw.strip()
    if drop_prefix and s.startswith(drop_prefix):
        s = s[len(drop_prefix):]
    return s or None


def _split_state_value(cell: str) -> tuple[str | None, float | None]:
    """Parse cells like 'SNA, 615.27' or 'DI_GEAR_D, 1.0' or '42.5'."""
    if not cell:
        return None, None
    cell = cell.strip()
    if "," in cell:
        state, _, val = cell.partition(",")
        state = state.strip() or None
        try:
            return state, float(val.strip())
        except ValueError:
            return state, None
    try:
        return None, float(cell)
    except ValueError:
        return cell, None


def _parse_ts(s: str) -> float | None:
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


@dataclass
class ImportStats:
    files: int = 0
    rows: int = 0
    samples: int = 0
    fsd_samples: int = 0
    hard_brakes: int = 0
    rapid_accels: int = 0
    miles: float = 0.0
    kwh: float = 0.0
    charging_sessions: int = 0


def iter_vehicle_csvs(root: Path) -> Iterator[Path]:
    folder = root / "Vehicle Data"
    if not folder.exists():
        return iter([])
    return iter(sorted(folder.glob("*.csv")))


SENTINEL_KEY = "4294967295"  # 0xFFFFFFFF — "no key present"


def list_keys(root: Path) -> list[tuple[str, int]]:
    """Return [(key_id, count), ...] for every distinct phone/keyfob seen."""
    counter: Counter[str] = Counter()
    for csv_path in iter_vehicle_csvs(root):
        with csv_path.open(newline="") as f:
            for row in csv.DictReader(f):
                _, val = _split_state_value(row.get(KEY_COL, ""))
                if val is None:
                    continue
                key = str(int(val))
                if key == SENTINEL_KEY:
                    continue
                counter[key] += 1
    return counter.most_common()


def discover_key_map(root: Path) -> dict[str, str]:
    """Auto-map up to 3 most-frequent keys to Colin / Lindsey / Carson."""
    keys = list_keys(root)
    names = ["Colin", "Lindsey", "Carson"]
    return {kid: name for (kid, _), name in zip(keys, names)}


def load_charging(
    root: Path, start_ts: float | None = None, end_ts: float | None = None
) -> tuple[float, int, list[dict]]:
    p = root / "Charging Data" / "Charging Data.csv"
    if not p.exists():
        return 0.0, 0, []
    total = 0.0
    sessions = 0
    events: list[dict] = []
    with p.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                kwh = float(row.get("Energy Added (kWh)", 0) or 0)
            except ValueError:
                continue
            ts = _parse_ts(row.get("Charge End Time (UTC)", "")) or 0
            if start_ts is not None and ts and ts < start_ts:
                continue
            if end_ts is not None and ts and ts > end_ts:
                continue
            total += kwh
            sessions += 1
            events.append({
                "type": "charging_session",
                "ts": ts,
                "vin": (row.get("VIN") or "").strip(),
                "energy_kwh": round(kwh, 3),
                "duration_s": int(row.get("Charge Duration (s)") or 0),
                "charger_type": row.get("Charger Type"),
                "location": row.get("Location"),
            })
    return total, sessions, events


def process_vehicle_csv(
    csv_path: Path, key_map: dict[str, str]
) -> tuple[list[dict], float, ImportStats]:
    """Stream a daily CSV and emit driver_sample events + miles driven."""
    stats = ImportStats(files=1)
    out: list[dict] = []
    fsd_out: list[dict] = []
    miles_driven = 0.0
    last_odo_km: float | None = None
    current_driver: str | None = None  # carry forward — keys only log at session start
    # FSD/profile fields are also sparse — carry forward.
    cur_ap_state: str | None = None
    cur_accel: str | None = None
    cur_steering: str | None = None
    cur_stopping: str | None = None
    cur_nav_on_ap: int | None = None

    with csv_path.open(newline="") as f:
        for row in csv.DictReader(f):
            stats.rows += 1
            ts = _parse_ts(row.get(DATE_COL, ""))
            if ts is None:
                continue

            _, speed_kph = _split_state_value(row.get(SPEED_COL, ""))
            _, accel = _split_state_value(row.get(ACCEL_COL, ""))
            _, odo_km = _split_state_value(row.get(ODO_COL, ""))
            gear, _ = _split_state_value(row.get(GEAR_COL, ""))
            _, key_val = _split_state_value(row.get(KEY_COL, ""))

            # FSD / profile fields — split out the state portion.
            ap_state, _ = _split_state_value(row.get(AP_STATE_COL, ""))
            accel_mode, _ = _split_state_value(row.get(ACCEL_MODE_COL, ""))
            steering_mode, _ = _split_state_value(row.get(STEERING_MODE_COL, ""))
            stopping_mode, _ = _split_state_value(row.get(STOPPING_MODE_COL, ""))
            nav_on_ap_state, _ = _split_state_value(row.get(NAV_ON_AP_COL, ""))

            if ap_state and ap_state != "SNA":
                cur_ap_state = ap_state
            if accel_mode:
                cur_accel = accel_mode
            if steering_mode:
                cur_steering = _normalize_state(steering_mode, drop_prefix="STEERING_TUNE_")
            if stopping_mode:
                cur_stopping = stopping_mode
            if nav_on_ap_state:
                cur_nav_on_ap = 1 if nav_on_ap_state.upper() in {"ON", "ENABLED", "1"} else 0

            # Odometer-based mileage (most accurate).
            if odo_km is not None:
                if last_odo_km is not None and odo_km >= last_odo_km:
                    miles_driven += (odo_km - last_odo_km) * KM_TO_MI
                last_odo_km = odo_km

            # Skip impossible speed sentinels (e.g. SNA-state cells).
            if speed_kph is not None and (speed_kph < 0 or speed_kph > 200):
                speed_kph = None

            if key_val is not None:
                kid = str(int(key_val))
                if kid != SENTINEL_KEY:
                    current_driver = key_map.get(kid, current_driver)
            driver = current_driver
            if not driver:
                continue

            event = None
            if accel is not None:
                if accel <= HARD_BRAKE_MS2:
                    event = {"type": "hard_brake", "delta_mph_per_s": round(accel * 2.237, 2)}
                    stats.hard_brakes += 1
                elif accel >= RAPID_ACCEL_MS2:
                    event = {"type": "rapid_accel", "delta_mph_per_s": round(accel * 2.237, 2)}
                    stats.rapid_accels += 1

            shift = None
            if isinstance(gear, str) and gear.startswith("DI_GEAR_"):
                shift = gear.split("_")[-1]  # P/R/N/D

            speed_mph = round(speed_kph * KPH_TO_MPH, 1) if speed_kph is not None else None

            # Emit a sample row when something interesting happened: an
            # event, the car was moving, or the gear is engaged.
            if event or (speed_mph and speed_mph > 0) or shift in {"D", "R"}:
                out.append({
                    "type": "driver_sample",
                    "ts": ts,
                    "vin": row.get("VIN", "").strip(),
                    "driver": driver,
                    "speed_mph": speed_mph,
                    "speed_limit_mph": None,
                    "shift_state": shift,
                    "gps": None,
                    "maps_url": None,
                    "event": event,
                })
                stats.samples += 1

                # FSD profile snapshot — only emit while the car is being driven,
                # so settings idle in the parking lot don't dominate the totals.
                if cur_ap_state:
                    fsd_out.append({
                        "vin": row.get("VIN", "").strip(),
                        "ts": ts,
                        "driver": driver,
                        "ap_state": cur_ap_state,
                        "accel_mode": cur_accel,
                        "steering_mode": cur_steering,
                        "stopping_mode": cur_stopping,
                        "nav_on_ap": cur_nav_on_ap,
                    })
                    stats.fsd_samples += 1

    stats.miles = miles_driven
    return out, fsd_out, miles_driven, stats


def downsample(events: list[dict], every: float = 30.0) -> list[dict]:
    """Keep all incident events; thin out plain samples to one per `every` seconds."""
    out: list[dict] = []
    last_kept_ts = 0.0
    for ev in events:
        if ev.get("event"):  # always keep brakes/accels
            out.append(ev)
            last_kept_ts = ev["ts"]
            continue
        if ev["ts"] - last_kept_ts >= every:
            out.append(ev)
            last_kept_ts = ev["ts"]
    return out


def run_import(
    root: Path,
    log_path: Path,
    roi_path: Path,
    key_map: dict[str, str] | None = None,
    sample_every: float = 30.0,
    append: bool = False,
) -> ImportStats:
    if key_map is None:
        key_map = discover_key_map(root)
        logger.info("Auto-detected key map: %s", key_map)

    if not key_map:
        raise RuntimeError(
            "No active-key entries found. Provide --key-map id=Carson explicitly."
        )

    total = ImportStats()
    all_events: list[dict] = []
    all_fsd: list[dict] = []

    for csv_path in iter_vehicle_csvs(root):
        logger.info("Processing %s", csv_path.name)
        events, fsd, miles, stats = process_vehicle_csv(csv_path, key_map)
        all_events.extend(events)
        all_fsd.extend(fsd)
        total.files += stats.files
        total.rows += stats.rows
        total.samples += stats.samples
        total.fsd_samples += stats.fsd_samples
        total.hard_brakes += stats.hard_brakes
        total.rapid_accels += stats.rapid_accels
        total.miles += miles

    # Load the full charging history — used for lifetime ROI / TCO.
    kwh, sessions, charging_events = load_charging(root)
    total.kwh = kwh
    total.charging_sessions = sessions

    # Combine + sort driver samples chronologically, downsample, then ROI snapshot.
    combined = sorted(all_events, key=lambda e: e.get("ts") or 0)
    combined = downsample(combined, every=sample_every)

    # Sample-window kWh: only sessions whose end_ts falls inside the
    # vehicle-data export window. Used to derive mi/kWh.
    sample_ts = [e["ts"] for e in all_events if e.get("ts")]
    sample_window = (min(sample_ts), max(sample_ts)) if sample_ts else (None, None)
    sample_kwh = sum(
        c["energy_kwh"] for c in charging_events
        if sample_window[0] and sample_window[0] <= c["ts"] <= sample_window[1]
    )

    # RoiState here represents the *sample window* (miles driven + kWh
    # added during the same period). Lifetime data lives in DB.
    state = RoiState.load(roi_path) if append else RoiState()
    state.total_miles += total.miles
    state.total_kwh += sample_kwh
    state.save(roi_path)

    combined.append({"type": "roi_report", "ts": combined[-1]["ts"] if combined else 0, **state.report()})

    mode = "a" if append else "w"
    with log_path.open(mode) as f:
        for ev in combined:
            f.write(json.dumps(ev) + "\n")
        for ch in charging_events:
            f.write(json.dumps(ch) + "\n")

    # Mirror everything into SQLite — single source of truth for the API.
    db.init()
    with db.connect() as conn:
        conn.execute("BEGIN")
        for ev in combined:
            db.record_event(conn, ev)
        for ch in charging_events:
            ch_with_vin = {**ch, "vin": ch.get("vin", "")}
            db.record_charging(conn, ch_with_vin)
        for f in all_fsd:
            db.record_fsd(conn, f)
        db.set_roi_totals(conn, vin="", total_miles=state.total_miles,
                          total_kwh=state.total_kwh)
        conn.execute("COMMIT")

    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Path to the unzipped Tesla data export")
    parser.add_argument("--log-file", type=Path, default=Path("monitoring_log.json"))
    parser.add_argument("--roi-state", type=Path, default=Path(".roi_state.json"))
    parser.add_argument("--key-map", default="", help='e.g. "2851572947=Carson,1234=Alex"')
    parser.add_argument("--sample-every", type=float, default=30.0,
                        help="Seconds between retained non-incident samples")
    parser.add_argument("--append", action="store_true",
                        help="Append to existing log + ROI state instead of overwriting")
    parser.add_argument("--list-keys", action="store_true",
                        help="Print every distinct phone/key ID with sample count and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")

    if args.list_keys:
        for kid, count in list_keys(args.root):
            print(f"{kid:>12}  {count:>6}")
        return

    key_map = None
    if args.key_map:
        key_map = dict(p.split("=", 1) for p in args.key_map.split(",") if "=" in p)

    stats = run_import(
        args.root, args.log_file, args.roi_state, key_map,
        sample_every=args.sample_every, append=args.append,
    )
    print(f"\nImport summary")
    print(f"  files          {stats.files}")
    print(f"  rows scanned   {stats.rows:,}")
    print(f"  samples kept   {stats.samples:,}")
    print(f"  fsd samples    {stats.fsd_samples:,}")
    print(f"  hard brakes    {stats.hard_brakes}")
    print(f"  rapid accels   {stats.rapid_accels}")
    print(f"  miles driven   {stats.miles:,.1f}")
    print(f"  kWh charged    {stats.kwh:,.2f}  ({stats.charging_sessions} sessions)")
    print(f"\nROI state → {args.roi_state}")
    print(f"Event log  → {args.log_file}")


if __name__ == "__main__":
    main()
