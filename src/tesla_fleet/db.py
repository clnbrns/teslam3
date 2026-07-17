"""SQLite storage layer for telemetry, events, and ROI state.

Single file with WAL mode so the dashboard can read while the monitor /
telemetry receiver writes. All inserts are idempotent on (vin, ts) so
re-imports and stream replays are safe.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(os.environ.get("TESLA_DB_PATH", "tesla.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    vin TEXT NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    driver TEXT,
    payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS events_vin_ts_type
    ON events(vin, ts, type);
CREATE INDEX IF NOT EXISTS events_driver_ts
    ON events(driver, ts);
CREATE INDEX IF NOT EXISTS events_type_ts
    ON events(type, ts);

CREATE TABLE IF NOT EXISTS charging_sessions (
    vin TEXT NOT NULL,
    end_ts REAL NOT NULL,
    energy_kwh REAL NOT NULL,
    duration_s INTEGER,
    charger_type TEXT,
    location TEXT,
    PRIMARY KEY (vin, end_ts)
);

-- In-flight charging session (one per VIN); survives poller restarts.
-- Finalized rows move to charging_sessions and this row is cleared.
CREATE TABLE IF NOT EXISTS charge_session_state (
    vin TEXT PRIMARY KEY,
    start_ts REAL NOT NULL,
    last_ts REAL NOT NULL,
    energy_kwh REAL NOT NULL DEFAULT 0,
    charger_type TEXT,
    location TEXT,
    lat REAL,
    lon REAL
);

-- Reverse-geocode cache: one row per ~100m lat/lon cell.
CREATE TABLE IF NOT EXISTS geocode_cache (
    cell TEXT PRIMARY KEY,
    name TEXT,
    fetched_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS roi_state (
    vin TEXT PRIMARY KEY,
    total_miles REAL NOT NULL DEFAULT 0,
    total_kwh REAL NOT NULL DEFAULT 0,
    last_odometer REAL,
    last_charge_energy_added REAL
);

CREATE TABLE IF NOT EXISTS fsd_samples (
    vin TEXT NOT NULL,
    ts REAL NOT NULL,
    driver TEXT,
    ap_state TEXT,                 -- e.g. UNAVAILABLE / AVAILABLE / ACTIVE_FSD / ACTIVE_AP
    accel_mode TEXT,               -- CHILL / STANDARD / SPORT / PERFORMANCE / PLAID
    steering_mode TEXT,            -- COMFORT / STANDARD / SPORT
    stopping_mode TEXT,            -- ROLL / CREEP / HOLD
    nav_on_ap INTEGER,             -- 0/1 if Navigate-on-AP toggled
    PRIMARY KEY (vin, ts)
);
CREATE INDEX IF NOT EXISTS fsd_driver_ts ON fsd_samples(driver, ts);
CREATE INDEX IF NOT EXISTS fsd_ap_state  ON fsd_samples(ap_state);

-- Cabin-camera-derived attentiveness (Tesla SW 2026.8+).
-- One row per detection event; the 'kind' column distinguishes signals so we
-- can score a single driver across multiple inattentiveness types.
CREATE TABLE IF NOT EXISTS attention_events (
    vin TEXT NOT NULL,
    ts REAL NOT NULL,
    driver TEXT,                   -- Tesla profile name from key OR cabin-cam ID
    verified_driver TEXT,          -- cabin-cam facial verification (when available)
    kind TEXT NOT NULL,            -- gaze_away | phone_use | driver_swap | drowsy | etc.
    duration_s REAL,               -- length of the detection window, if reported
    severity TEXT,                 -- low | medium | high (when reported)
    speed_mph REAL,                -- vehicle speed at time of event (context)
    payload TEXT,                  -- raw json blob from the API for forensics
    PRIMARY KEY (vin, ts, kind)
);
CREATE INDEX IF NOT EXISTS attn_driver_ts ON attention_events(driver, ts);
CREATE INDEX IF NOT EXISTS attn_kind_ts   ON attention_events(kind, ts);
"""

_lock = threading.Lock()


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a sqlite3 connection in WAL mode. Thread-safe via a process lock."""
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()


def init(db_path: Path | None = None) -> None:
    """Create tables / indexes if they don't exist. Safe to call repeatedly."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def record_event(conn: sqlite3.Connection, event: dict) -> bool:
    """Insert one event row. Returns True if inserted, False if duplicate."""
    vin = event.get("vin") or ""
    ts = event.get("ts")
    typ = event.get("type")
    if ts is None or not typ:
        return False
    driver = event.get("driver")
    try:
        conn.execute(
            "INSERT INTO events(vin, ts, type, driver, payload) VALUES (?, ?, ?, ?, ?)",
            (vin, float(ts), typ, driver, json.dumps(event)),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def record_charging(conn: sqlite3.Connection, session: dict) -> bool:
    vin = session.get("vin") or ""
    end_ts = session.get("ts") or session.get("end_ts")
    if not end_ts:
        return False
    try:
        conn.execute(
            "INSERT INTO charging_sessions(vin, end_ts, energy_kwh, duration_s, charger_type, location)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (vin, float(end_ts), float(session.get("energy_kwh") or 0),
             int(session.get("duration_s") or 0),
             session.get("charger_type"), session.get("location")),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def get_charge_state(conn: sqlite3.Connection, vin: str) -> dict | None:
    """In-flight charging session for a VIN, or None."""
    row = conn.execute(
        "SELECT start_ts, last_ts, energy_kwh, charger_type, location, lat, lon"
        " FROM charge_session_state WHERE vin = ?", (vin,),
    ).fetchone()
    return dict(row) if row else None


def save_charge_state(conn: sqlite3.Connection, vin: str, state: dict) -> None:
    conn.execute(
        "INSERT INTO charge_session_state(vin, start_ts, last_ts, energy_kwh,"
        " charger_type, location, lat, lon) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(vin) DO UPDATE SET last_ts=excluded.last_ts,"
        " energy_kwh=excluded.energy_kwh, charger_type=excluded.charger_type,"
        " location=excluded.location, lat=excluded.lat, lon=excluded.lon",
        (vin, state["start_ts"], state["last_ts"], state.get("energy_kwh") or 0,
         state.get("charger_type"), state.get("location"),
         state.get("lat"), state.get("lon")),
    )


def clear_charge_state(conn: sqlite3.Connection, vin: str) -> None:
    conn.execute("DELETE FROM charge_session_state WHERE vin = ?", (vin,))


def geocode_get(conn: sqlite3.Connection, cell: str, max_age_s: float) -> str | None:
    """Cached place name for a lat/lon cell; empty string = known-unresolvable."""
    row = conn.execute(
        "SELECT name, fetched_at FROM geocode_cache WHERE cell = ?", (cell,)
    ).fetchone()
    if row is None:
        return None
    if time.time() - row["fetched_at"] > max_age_s:
        return None
    return row["name"] if row["name"] is not None else ""


def geocode_put(conn: sqlite3.Connection, cell: str, name: str | None) -> None:
    conn.execute(
        "INSERT INTO geocode_cache(cell, name, fetched_at) VALUES (?, ?, ?)"
        " ON CONFLICT(cell) DO UPDATE SET name=excluded.name,"
        " fetched_at=excluded.fetched_at",
        (cell, name, time.time()),
    )


def get_roi(conn: sqlite3.Connection, vin: str = "") -> dict:
    """Return ROI totals for a vin (or aggregated across all vins when empty)."""
    if vin:
        row = conn.execute(
            "SELECT total_miles, total_kwh FROM roi_state WHERE vin = ?", (vin,)
        ).fetchone()
        return {"total_miles": row["total_miles"] if row else 0.0,
                "total_kwh": row["total_kwh"] if row else 0.0}
    row = conn.execute(
        "SELECT COALESCE(SUM(total_miles), 0) AS m, COALESCE(SUM(total_kwh), 0) AS k FROM roi_state"
    ).fetchone()
    return {"total_miles": row["m"] or 0.0, "total_kwh": row["k"] or 0.0}


def update_roi(conn: sqlite3.Connection, vin: str, *,
               odometer_mi: float | None = None,
               charge_energy_added_kwh: float | None = None) -> None:
    """Apply odometer and charge-energy deltas to the persisted ROI row."""
    row = conn.execute(
        "SELECT total_miles, total_kwh, last_odometer, last_charge_energy_added"
        " FROM roi_state WHERE vin = ?", (vin,),
    ).fetchone()
    miles = (row["total_miles"] if row else 0.0) or 0.0
    kwh = (row["total_kwh"] if row else 0.0) or 0.0
    last_odo = row["last_odometer"] if row else None
    last_charge = row["last_charge_energy_added"] if row else None

    if odometer_mi is not None:
        if last_odo is not None and odometer_mi >= last_odo:
            miles += odometer_mi - last_odo
        last_odo = odometer_mi
    if charge_energy_added_kwh is not None:
        if last_charge is not None and charge_energy_added_kwh >= last_charge:
            kwh += charge_energy_added_kwh - last_charge
        last_charge = charge_energy_added_kwh

    conn.execute(
        "INSERT INTO roi_state(vin, total_miles, total_kwh, last_odometer, last_charge_energy_added)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(vin) DO UPDATE SET total_miles=excluded.total_miles,"
        " total_kwh=excluded.total_kwh, last_odometer=excluded.last_odometer,"
        " last_charge_energy_added=excluded.last_charge_energy_added",
        (vin, miles, kwh, last_odo, last_charge),
    )


def set_roi_totals(conn: sqlite3.Connection, vin: str,
                   total_miles: float, total_kwh: float) -> None:
    """Backfill helper: overwrite totals for a vin (used by importer)."""
    conn.execute(
        "INSERT INTO roi_state(vin, total_miles, total_kwh) VALUES (?, ?, ?)"
        " ON CONFLICT(vin) DO UPDATE SET total_miles=excluded.total_miles,"
        " total_kwh=excluded.total_kwh",
        (vin, total_miles, total_kwh),
    )


def read_events(conn: sqlite3.Connection, *, limit: int = 200,
                type: str | None = None, driver: str | None = None) -> list[dict]:
    """Return events ordered newest-first, with the JSON payload restored."""
    where = []
    params: list[Any] = []
    if type:
        where.append("type = ?")
        params.append(type)
    if driver:
        where.append("LOWER(driver) = LOWER(?)")
        params.append(driver)
    sql = "SELECT payload FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    return [json.loads(r["payload"]) for r in conn.execute(sql, params)]


def event_time_window(conn: sqlite3.Connection, type: str = "driver_sample"
                      ) -> tuple[float | None, float | None]:
    row = conn.execute(
        "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM events WHERE type = ?", (type,)
    ).fetchone()
    return (row["lo"], row["hi"]) if row else (None, None)


def all_charging_sessions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT vin, end_ts, energy_kwh, duration_s, charger_type, location"
        " FROM charging_sessions ORDER BY end_ts ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def record_fsd(conn: sqlite3.Connection, sample: dict) -> bool:
    vin = sample.get("vin") or ""
    ts = sample.get("ts")
    if ts is None:
        return False
    try:
        conn.execute(
            "INSERT INTO fsd_samples(vin, ts, driver, ap_state, accel_mode,"
            " steering_mode, stopping_mode, nav_on_ap)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (vin, float(ts), sample.get("driver"), sample.get("ap_state"),
             sample.get("accel_mode"), sample.get("steering_mode"),
             sample.get("stopping_mode"), sample.get("nav_on_ap")),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def fsd_summary(conn: sqlite3.Connection) -> dict:
    """Aggregate FSD usage + profile preferences per driver."""
    rows = conn.execute(
        "SELECT driver, ap_state, accel_mode, steering_mode, stopping_mode,"
        " COUNT(*) AS n FROM fsd_samples"
        " WHERE driver IS NOT NULL"
        " GROUP BY driver, ap_state, accel_mode, steering_mode, stopping_mode"
    ).fetchall()

    # Build per-driver aggregates.
    drivers: dict[str, dict] = {}
    for r in rows:
        d = drivers.setdefault(r["driver"], {
            "driver": r["driver"],
            "samples": 0,
            "ap_breakdown": {},      # AVAILABLE / UNAVAILABLE / ACTIVE_FSD / etc.
            "accel_modes": {},
            "steering_modes": {},
            "stopping_modes": {},
        })
        n = r["n"]
        d["samples"] += n
        if r["ap_state"]:
            d["ap_breakdown"][r["ap_state"]] = d["ap_breakdown"].get(r["ap_state"], 0) + n
        if r["accel_mode"]:
            d["accel_modes"][r["accel_mode"]] = d["accel_modes"].get(r["accel_mode"], 0) + n
        if r["steering_mode"]:
            d["steering_modes"][r["steering_mode"]] = d["steering_modes"].get(r["steering_mode"], 0) + n
        if r["stopping_mode"]:
            d["stopping_modes"][r["stopping_mode"]] = d["stopping_modes"].get(r["stopping_mode"], 0) + n

    # Compute derived metrics: FSD %, available %, dominant profile.
    out = []
    for name, d in drivers.items():
        ap = d["ap_breakdown"]
        active_fsd = ap.get("ACTIVE_FSD", 0)
        active_ap = ap.get("ACTIVE_AP", 0)  # plain Autopilot when distinguishable
        available = ap.get("AVAILABLE", 0)
        unavailable = ap.get("UNAVAILABLE", 0)
        engaged_total = active_fsd + active_ap
        # "Engagement %" = engaged samples / (engaged + available samples).
        # Excludes UNAVAILABLE so we measure choice when FSD was an option.
        opportunity = engaged_total + available
        engagement_pct = (engaged_total / opportunity * 100) if opportunity else 0
        fsd_share = (active_fsd / engaged_total * 100) if engaged_total else 0

        def top(d_: dict, n: int = 3) -> list[dict]:
            return [
                {"key": k, "n": v, "pct": round(v / max(sum(d_.values()), 1) * 100, 1)}
                for k, v in sorted(d_.items(), key=lambda kv: -kv[1])[:n]
            ]

        out.append({
            "driver": name,
            "samples": d["samples"],
            "engaged_samples": engaged_total,
            "fsd_samples": active_fsd,
            "ap_samples": active_ap,
            "available_samples": available,
            "unavailable_samples": unavailable,
            "engagement_pct": round(engagement_pct, 1),
            "fsd_share_of_engaged": round(fsd_share, 1),
            "ap_breakdown": ap,
            "accel_top": top(d["accel_modes"]),
            "steering_top": top(d["steering_modes"]),
            "stopping_top": top(d["stopping_modes"]),
        })
    out.sort(key=lambda x: -x["samples"])
    return {"drivers": out}


def record_attention(conn: sqlite3.Connection, ev: dict) -> bool:
    vin = ev.get("vin") or ""
    ts = ev.get("ts")
    kind = ev.get("kind")
    if ts is None or not kind:
        return False
    try:
        conn.execute(
            "INSERT INTO attention_events(vin, ts, driver, verified_driver, kind,"
            " duration_s, severity, speed_mph, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vin, float(ts), ev.get("driver"), ev.get("verified_driver"), kind,
             ev.get("duration_s"), ev.get("severity"), ev.get("speed_mph"),
             json.dumps(ev.get("payload") or {})),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def _driver_seconds_in_car(conn: sqlite3.Connection) -> dict[str, float]:
    """Estimate seconds-in-the-car per driver by summing the gaps between
    consecutive driver_sample events (ignoring gaps > 5 min, which mark a
    new session). Only samples with the driver field populated count."""
    rows = conn.execute(
        "SELECT ts, driver FROM events WHERE type = 'driver_sample'"
        " AND driver IS NOT NULL ORDER BY driver, ts ASC"
    ).fetchall()
    totals: dict[str, float] = {}
    last_ts: float | None = None
    last_driver: str | None = None
    for r in rows:
        if r["driver"] != last_driver:
            last_driver, last_ts = r["driver"], r["ts"]
            continue
        gap = r["ts"] - (last_ts or r["ts"])
        if 0 < gap <= 300:                          # ≤5 min = same session
            totals[r["driver"]] = totals.get(r["driver"], 0.0) + gap
        last_ts = r["ts"]
    return totals


def attention_summary(conn: sqlite3.Connection) -> dict:
    """Aggregate cabin-camera inattentiveness by driver, with derived score.

    Per-kind metrics are reported BOTH as event counts AND as a percentage
    of time-in-the-car (sum of detection durations / total drive seconds).
    The score deducts proportionally to those percentages, so a driver who
    spends 5% of their drive looking at a phone is worse than one who spends
    1% on it, regardless of total miles.
    """
    rows = conn.execute(
        "SELECT COALESCE(verified_driver, driver) AS d, kind, COUNT(*) AS n,"
        " AVG(duration_s) AS avg_dur, SUM(duration_s) AS total_dur,"
        " MAX(severity) AS max_sev"
        " FROM attention_events"
        " WHERE COALESCE(verified_driver, driver) IS NOT NULL"
        " GROUP BY d, kind"
    ).fetchall()
    mism = conn.execute(
        "SELECT driver AS expected, verified_driver AS actual, COUNT(*) AS n"
        " FROM attention_events"
        " WHERE driver IS NOT NULL AND verified_driver IS NOT NULL"
        " AND driver != verified_driver"
        " GROUP BY driver, verified_driver"
    ).fetchall()

    drive_seconds = _driver_seconds_in_car(conn)

    drivers: dict[str, dict] = {}
    for r in rows:
        d = drivers.setdefault(r["d"], {
            "driver": r["d"], "events": 0,
            "by_kind": {}, "avg_durations": {},
            "total_durations": {},
        })
        d["events"] += r["n"]
        d["by_kind"][r["kind"]] = r["n"]
        if r["avg_dur"] is not None:
            d["avg_durations"][r["kind"]] = round(r["avg_dur"], 2)
        if r["total_dur"] is not None:
            d["total_durations"][r["kind"]] = float(r["total_dur"])

    out = []
    # Pct-of-time-in-car deduction weights: a driver who spends 1% of every
    # drive on their phone loses 25 pts; 1% drowsy loses 40 pts. Tuned so
    # realistic distributions land in the B / C range.
    pct_weights = {"gaze_away": 5, "phone_use": 25, "drowsy": 40, "driver_swap": 0}
    for name, d in drivers.items():
        in_car_s = drive_seconds.get(name, 0.0)
        pct_by_kind: dict[str, float] = {}
        deductions = 0.0
        for kind, total_s in d["total_durations"].items():
            pct = (total_s / in_car_s * 100) if in_car_s > 0 else 0.0
            pct_by_kind[kind] = round(pct, 2)
            deductions += pct * pct_weights.get(kind, 0)
        score = int(max(0, min(100, 100 - deductions)))
        if score >= 90: grade = "A"
        elif score >= 80: grade = "B"
        elif score >= 70: grade = "C"
        elif score >= 60: grade = "D"
        else: grade = "F"
        out.append({
            **d,
            "in_car_seconds": round(in_car_s, 1),
            "in_car_minutes": round(in_car_s / 60, 1),
            "pct_by_kind": pct_by_kind,
            "attention_score": score,
            "grade": grade,
        })
    out.sort(key=lambda x: x["attention_score"], reverse=True)
    return {
        "drivers": out,
        "verification_mismatches": [dict(r) for r in mism],
    }


def driver_samples(conn: sqlite3.Connection, driver: str) -> list[dict]:
    """All driver_sample rows for a driver (used by the report endpoint)."""
    rows = conn.execute(
        "SELECT payload FROM events WHERE type = 'driver_sample'"
        " AND LOWER(driver) = LOWER(?) ORDER BY ts ASC", (driver,)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]
