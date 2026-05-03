"""Fleet Telemetry — push-based ingestion (~18× cheaper than polling).

Two responsibilities:

1. **Register a telemetry config** with Tesla so the car streams the
   fields we care about to our HTTPS endpoint at the configured frequency.
   Tesla calls back with `POST <hostname>/telemetry` whenever new data is
   available (typically batched at 1–10 Hz while moving).

2. **Ingest** the streamed payloads into SQLite and derive driver events
   (hard brake, rapid acceleration) using the same logic the importer
   and poller already use.

This module does NOT yet sign requests with the partner ECDSA key —
needed once Tesla rolls signature enforcement out for telemetry config
calls; the field is left as a hook on TelemetryClient.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from tesla_fleet import db
from tesla_fleet.client import TeslaFleetClient

logger = logging.getLogger(__name__)

# Fields we ask Tesla to stream. Frequency in milliseconds between samples.
# Reference: https://developer.tesla.com/docs/fleet-api/telemetry/available-data
TELEMETRY_FIELDS: dict[str, dict[str, Any]] = {
    "VehicleSpeed":              {"interval_seconds": 1, "minimum_delta": 0.5},
    "Location":                  {"interval_seconds": 5},
    "Gear":                      {"interval_seconds": 1},
    "BatteryLevel":              {"interval_seconds": 60, "minimum_delta": 1},
    "ChargeState":               {"interval_seconds": 30},
    "ChargeAmps":                {"interval_seconds": 30},
    "Odometer":                  {"interval_seconds": 60},
    "OutsideTemp":               {"interval_seconds": 60},
    "InsideTemp":                {"interval_seconds": 60},
    "Soc":                       {"interval_seconds": 60},
    "ACChargingEnergyIn":        {"interval_seconds": 60},
    "DCChargingEnergyIn":        {"interval_seconds": 60},
    "EstBatteryRange":           {"interval_seconds": 60},
    "RatedRange":                {"interval_seconds": 300},
    "VehicleName":               {"interval_seconds": 3600},
    "Locked":                    {"interval_seconds": 60},
    "ChargeLimitSoc":            {"interval_seconds": 300},
    # Driving dynamics — used for brake/accel detection on the server side.
    "AcceleratorPedalPosition":  {"interval_seconds": 1},
    "BrakePedalPosition":        {"interval_seconds": 1},
}

HARD_BRAKE_MPHS = -7.0
RAPID_ACCEL_MPHS = 7.0


def build_config(hostname: str, vin: str, port: int = 443) -> dict:
    """Construct the body for POST /api/1/vehicles/{vin}/fleet_telemetry_config."""
    return {
        "vins": [vin],
        "config": {
            "hostname": hostname,
            "port": port,
            "ca": "",  # Tesla-trusted CA chain; empty falls back to public CAs
            "exp": int(time.time()) + 30 * 24 * 3600,  # 30 days; refresh periodically
            "fields": TELEMETRY_FIELDS,
        },
    }


async def register(client: TeslaFleetClient, hostname: str, vin: str) -> dict:
    """Push the telemetry config to Tesla. Idempotent — overwrites prior config."""
    body = build_config(hostname, vin)
    return await client._request(
        "POST", f"/api/1/vehicles/{vin}/fleet_telemetry_config", json=body
    )


async def unregister(client: TeslaFleetClient, vin: str) -> dict:
    """Disable streaming for a VIN (back to polling-only)."""
    return await client._request("DELETE", f"/api/1/vehicles/{vin}/fleet_telemetry_config")


# -----------------------------------------------------------------------
# Ingestion side — called by the FastAPI /telemetry endpoint.
# -----------------------------------------------------------------------

class _SpeedTracker:
    """Per-VIN running speed/time delta for brake/accel detection."""

    def __init__(self) -> None:
        self.last_mph: float | None = None
        self.last_ts: float | None = None

    def detect(self, mph: float | None, ts: float) -> dict | None:
        if mph is None:
            return None
        ev: dict | None = None
        if self.last_mph is not None and self.last_ts is not None:
            dt = ts - self.last_ts
            if dt > 0:
                d_mphs = (mph - self.last_mph) / dt
                if d_mphs <= HARD_BRAKE_MPHS:
                    ev = {"type": "hard_brake", "delta_mph_per_s": round(d_mphs, 2)}
                elif d_mphs >= RAPID_ACCEL_MPHS:
                    ev = {"type": "rapid_accel", "delta_mph_per_s": round(d_mphs, 2)}
        self.last_mph = mph
        self.last_ts = ts
        return ev


_TRACKERS: dict[str, _SpeedTracker] = {}


def _tracker(vin: str) -> _SpeedTracker:
    if vin not in _TRACKERS:
        _TRACKERS[vin] = _SpeedTracker()
    return _TRACKERS[vin]


def ingest_payload(payload: dict, *, default_driver: str | None = None) -> int:
    """Persist a Tesla telemetry payload to SQLite. Returns rows written.

    Tesla's payload shape (current spec):
        {
          "vin": "...",
          "createdAt": "ISO-8601",
          "data": [
            {"key": "VehicleSpeed", "value": {"doubleValue": 45.0}, "createdAt": "..."},
            ...
          ]
        }
    """
    vin = payload.get("vin") or ""
    if not vin:
        return 0
    rows = payload.get("data") or []
    by_key: dict[str, Any] = {}
    sample_ts: float | None = None

    for r in rows:
        k = r.get("key")
        v = r.get("value") or {}
        # Tesla wraps scalars in typed envelopes; pull the first value.
        for typed in ("doubleValue", "stringValue", "intValue", "floatValue", "boolValue"):
            if typed in v:
                by_key[k] = v[typed]
                break
        else:
            by_key[k] = v
        if not sample_ts:
            try:
                sample_ts = httpx._parse_date(r.get("createdAt") or "").timestamp()
            except Exception:
                sample_ts = None

    if not sample_ts:
        sample_ts = time.time()

    speed_mph = by_key.get("VehicleSpeed")  # already in mph per Tesla spec
    location = by_key.get("Location") or {}
    lat = (location or {}).get("latitude") if isinstance(location, dict) else None
    lon = (location or {}).get("longitude") if isinstance(location, dict) else None
    gear = by_key.get("Gear")  # "P" / "R" / "N" / "D"

    event = _tracker(vin).detect(speed_mph, sample_ts)
    record = {
        "type": "driver_sample",
        "ts": sample_ts,
        "vin": vin,
        "driver": default_driver,
        "speed_mph": speed_mph,
        "speed_limit_mph": None,
        "shift_state": gear,
        "gps": {"lat": lat, "lon": lon} if lat is not None else None,
        "maps_url": (f"https://www.google.com/maps?q={lat},{lon}" if lat is not None else None),
        "event": event,
        "raw": by_key,
    }

    written = 0
    with db.connect() as conn:
        if db.record_event(conn, record):
            written += 1
        # Update ROI from odometer + charge-energy keys when present.
        odo_mi = by_key.get("Odometer")
        charge_kwh = by_key.get("ACChargingEnergyIn") or by_key.get("DCChargingEnergyIn")
        if odo_mi is not None or charge_kwh is not None:
            db.update_roi(
                conn, vin,
                odometer_mi=odo_mi,
                charge_energy_added_kwh=charge_kwh,
            )
    return written
