"""Push alerts via ntfy — turns the dashboard into an actual monitor.

Setup (one env var):
    NTFY_TOPIC=goblin-m3p-<random-suffix>      # pick something unguessable
    NTFY_URL=https://ntfy.sh                   # optional, self-hosted override

Subscribe on the ntfy iOS/Android app with the same topic. No account needed.
Empty NTFY_TOPIC disables everything (all calls become no-ops).

Alert types, each with an independent cooldown so a bad drive doesn't spam:
- hard_brake / rapid_accel  (10 min)
- speeding over SPEED_ALERT_MPH, default 85  (15 min)
- driving during curfew hours, default 23:00–05:00 local  (30 min)

Sends happen on daemon threads — never blocks the poll loop, and a dead
ntfy endpoint degrades to log lines, not errors.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

SPEED_ALERT_MPH = float(os.environ.get("SPEED_ALERT_MPH", "85"))
CURFEW_START_HOUR = int(os.environ.get("CURFEW_START_HOUR", "23"))
CURFEW_END_HOUR = int(os.environ.get("CURFEW_END_HOUR", "5"))
LOCAL_TZ = ZoneInfo(os.environ.get("FLEET_LOCAL_TZ", "America/Chicago"))

_COOLDOWNS_S = {
    "hard_brake": 600,
    "rapid_accel": 600,
    "speeding": 900,
    "curfew": 1800,
}
_last_sent: dict[str, float] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return bool(NTFY_TOPIC)


def send(title: str, body: str, priority: str = "default",
         tags: str = "car") -> None:
    """Fire-and-forget push. Safe to call from sync or async code."""
    if not enabled():
        logger.info("[alert suppressed — NTFY_TOPIC unset] %s: %s", title, body)
        return

    def _post() -> None:
        try:
            httpx.post(
                f"{NTFY_URL}/{NTFY_TOPIC}",
                content=body.encode(),
                headers={"Title": title, "Priority": priority, "Tags": tags},
                timeout=10,
            )
        except httpx.HTTPError:
            logger.warning("ntfy send failed: %s", title)

    threading.Thread(target=_post, daemon=True).start()


def _cooled_down(key: str) -> bool:
    with _lock:
        now = time.time()
        if now - _last_sent.get(key, 0) < _COOLDOWNS_S.get(key, 600):
            return False
        _last_sent[key] = now
        return True


def _in_curfew(ts: float) -> bool:
    h = datetime.fromtimestamp(ts, LOCAL_TZ).hour
    if CURFEW_START_HOUR <= CURFEW_END_HOUR:
        return CURFEW_START_HOUR <= h < CURFEW_END_HOUR
    return h >= CURFEW_START_HOUR or h < CURFEW_END_HOUR


def maybe_alert_sample(record: dict) -> None:
    """Inspect one driver_sample record and push whatever alerts apply.

    Called from both the poller and the telemetry ingest path, so alert
    behavior is identical regardless of data source.
    """
    driver = record.get("driver") or "Unknown driver"
    speed = record.get("speed_mph")
    ts = record.get("ts") or time.time()
    maps_url = record.get("maps_url") or ""

    ev = record.get("event") or {}
    ev_type = ev.get("type")
    if ev_type in ("hard_brake", "rapid_accel") and _cooled_down(ev_type):
        label = "Hard brake" if ev_type == "hard_brake" else "Rapid acceleration"
        send(
            f"{label} — {driver}",
            f"Δ {ev.get('delta_mph_per_s')} mph/s at {speed or '?'} mph\n{maps_url}",
            priority="high", tags="warning,car",
        )

    if speed is not None and speed >= SPEED_ALERT_MPH and _cooled_down("speeding"):
        send(
            f"Speeding — {driver}",
            f"{speed:.0f} mph (alert threshold {SPEED_ALERT_MPH:.0f})\n{maps_url}",
            priority="high", tags="rotating_light,car",
        )

    moving = (speed or 0) > 0 or record.get("shift_state") in ("D", "R")
    if moving and _in_curfew(ts) and _cooled_down("curfew"):
        when = datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%-I:%M %p")
        send(
            f"Car in motion during curfew — {driver}",
            f"Driving at {when} (curfew {CURFEW_START_HOUR}:00–{CURFEW_END_HOUR:02d}:00)\n{maps_url}",
            priority="high", tags="night_with_stars,car",
        )
