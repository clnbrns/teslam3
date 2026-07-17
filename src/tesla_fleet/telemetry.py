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
from datetime import datetime
from typing import Any

import httpx

from tesla_fleet import alerts, charging, db
from tesla_fleet.client import TeslaFleetClient

logger = logging.getLogger(__name__)

# Fields we ask Tesla to stream. Frequency in seconds between samples.
# Reference: https://developer.tesla.com/docs/fleet-api/telemetry/available-data
#
# ONLY confirmed field names go in TELEMETRY_FIELDS — Tesla rejects a
# fleet_telemetry_config containing unknown fields, which would silently
# leave the car streaming nothing. Unverified names live in
# SPECULATIVE_FIELDS and are opt-in via register(..., include_speculative=True)
# once each name is confirmed against the current telemetry spec.
TELEMETRY_FIELDS: dict[str, dict[str, Any]] = {
    # 1 Hz speed is the backbone: trips, hard-brake/rapid-accel, max speed.
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
}

# NOT registered by default. Verify each name against Tesla's field list
# (developer.tesla.com → Fleet Telemetry → available-data) before enabling:
# a single unknown field fails the whole config registration.
SPECULATIVE_FIELDS: dict[str, dict[str, Any]] = {
    # Driving dynamics — nicer brake/accel signal than speed deltas.
    "AcceleratorPedalPosition":  {"interval_seconds": 1},
    "BrakePedalPosition":        {"interval_seconds": 1},
    # Tesla SW 2026.8+ cabin-camera fields (names are best-effort).
    "CabinCameraDriverProfile":  {"interval_seconds": 30},   # face-verified driver
    "DriverGazeAway":            {"interval_seconds": 1, "minimum_delta": 1},
    "DriverPhoneUse":            {"interval_seconds": 1, "minimum_delta": 1},
    "DriverDrowsy":              {"interval_seconds": 5},
    "DriverInattentive":         {"interval_seconds": 1},
}

HARD_BRAKE_MPHS = -7.0
RAPID_ACCEL_MPHS = 7.0


def _parse_iso_ts(s: str | None) -> float | None:
    """Parse Tesla's ISO-8601 createdAt (with or without trailing Z)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def build_config(hostname: str, vin: str, port: int = 443,
                 include_speculative: bool = False) -> dict:
    """Construct the body for POST /api/1/vehicles/{vin}/fleet_telemetry_config."""
    fields = dict(TELEMETRY_FIELDS)
    if include_speculative:
        fields.update(SPECULATIVE_FIELDS)
    return {
        "vins": [vin],
        "config": {
            "hostname": hostname,
            "port": port,
            "ca": "",  # Tesla-trusted CA chain; empty falls back to public CAs
            "exp": int(time.time()) + 30 * 24 * 3600,  # 30 days; refresh periodically
            "fields": fields,
        },
    }


async def register(client: TeslaFleetClient, hostname: str, vin: str,
                   proxy_url: str | None = None,
                   include_speculative: bool = False) -> dict:
    """Push the telemetry config to Tesla. Idempotent — overwrites prior config.

    Tesla requires this call to go through the Vehicle Command Proxy, which
    signs requests with the partner ECDSA key. If `proxy_url` is provided
    (e.g. https://vcp.burnsbuilt.co), route through it; otherwise hit Tesla
    directly (will fail with 400 "must be called through proxy").
    """
    body = build_config(hostname, vin, include_speculative=include_speculative)
    path = "/api/1/vehicles/fleet_telemetry_config_create"

    if proxy_url:
        # Proxy expects an Authorization header it forwards upstream.
        # TLS is verified — the VCP sits behind Railway's Let's Encrypt cert,
        # and this channel carries the OAuth bearer token.
        async with httpx.AsyncClient(base_url=proxy_url, timeout=30) as proxy:
            await client._ensure_fresh()
            resp = await proxy.post(
                path, json=body,
                headers={"Authorization": f"Bearer {client.token.access_token}"},
            )
            resp.raise_for_status()
            return resp.json()
    return await client._request("POST", path, json=body)


async def unregister(client: TeslaFleetClient, vin: str,
                     proxy_url: str | None = None) -> dict:
    """Disable streaming for a VIN (back to polling-only)."""
    path = f"/api/1/vehicles/{vin}/fleet_telemetry_config"
    if proxy_url:
        async with httpx.AsyncClient(base_url=proxy_url, timeout=30) as proxy:
            await client._ensure_fresh()
            resp = await proxy.delete(
                path,
                headers={"Authorization": f"Bearer {client.token.access_token}"},
            )
            resp.raise_for_status()
            return resp.json()
    return await client._request("DELETE", path)


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
            sample_ts = _parse_iso_ts(r.get("createdAt"))

    if not sample_ts:
        sample_ts = time.time()

    speed_mph = by_key.get("VehicleSpeed")  # already in mph per Tesla spec
    location = by_key.get("Location") or {}
    lat = (location or {}).get("latitude") if isinstance(location, dict) else None
    lon = (location or {}).get("longitude") if isinstance(location, dict) else None
    gear = by_key.get("Gear")  # "P" / "R" / "N" / "D"

    # SW 2026.8 cabin-camera signals.
    verified_driver = by_key.get("CabinCameraDriverProfile")
    attention_kinds = []
    if by_key.get("DriverGazeAway"):    attention_kinds.append(("gaze_away",  by_key.get("DriverGazeAway")))
    if by_key.get("DriverPhoneUse"):    attention_kinds.append(("phone_use",  by_key.get("DriverPhoneUse")))
    if by_key.get("DriverDrowsy"):      attention_kinds.append(("drowsy",     by_key.get("DriverDrowsy")))
    if by_key.get("DriverInattentive"): attention_kinds.append(("inattentive",by_key.get("DriverInattentive")))

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
    alerts.maybe_alert_sample(record)
    with db.connect() as conn:
        if db.record_event(conn, record):
            written += 1
        # SW 2026.8: persist face-verified driver + any inattentiveness flags.
        for kind, raw in attention_kinds:
            attn = {
                "vin": vin, "ts": sample_ts,
                "driver": default_driver,
                "verified_driver": verified_driver or default_driver,
                "kind": kind,
                "duration_s": (raw or {}).get("duration_s") if isinstance(raw, dict) else None,
                "severity":   (raw or {}).get("severity")   if isinstance(raw, dict) else None,
                "speed_mph": speed_mph,
                "payload": raw,
            }
            if db.record_attention(conn, attn):
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
        # Live charge-session boundary detection (same tracker as the poller).
        if by_key.get("ChargeState") is not None or charge_kwh is not None:
            charging.observe(
                conn, vin,
                ts=sample_ts,
                charging_state=by_key.get("ChargeState"),
                charge_energy_added=charge_kwh,
                fast_charger_present=bool(by_key.get("DCChargingEnergyIn")),
                lat=lat, lon=lon,
            )
    return written
