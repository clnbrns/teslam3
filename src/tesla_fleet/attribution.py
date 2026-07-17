"""Driver attribution when Tesla won't tell us who's driving.

The Fleet API doesn't return the active driver profile, so samples used to
inherit whatever the UI toggle was last set to — sticky forever, default
"Colin". This module resolves the driver per sample with a priority chain:

1. **Fresh manual override** — set via POST /api/active-driver; expires after
   OVERRIDE_TTL (default 12 h) instead of sticking forever.
2. **Schedule rules** — explicit windows like "weekdays 07:00–08:30 →
   Carson" from ``/data/driver_schedule.json`` (or DRIVER_SCHEDULE_JSON env).
3. **Learned prior** — hour-of-week driver histogram built from historical
   driver_sample rows (the data-export import carries real key-based
   attribution, and manual trip reassignments feed back in). Only wins a
   bucket with ≥ MIN_BUCKET_SAMPLES and ≥ MIN_BUCKET_SHARE dominance.
4. **Default driver** from settings.

Every resolution reports its source so the UI can show *why* a driver was
picked and the user knows when to correct it.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

LOCAL_TZ = ZoneInfo(os.environ.get("FLEET_LOCAL_TZ", "America/Chicago"))
OVERRIDE_TTL_S = float(os.environ.get("DRIVER_OVERRIDE_TTL_S", str(12 * 3600)))

MIN_BUCKET_SAMPLES = 20
MIN_BUCKET_SHARE = 0.6
_PRIOR_REFRESH_S = 6 * 3600

_prior: dict[int, str] | None = None
_prior_built_at = 0.0


# ---------------------------------------------------------------- overrides

def load_override(path: Path) -> dict:
    """Read the persisted override. Handles the legacy plain-text format
    (pre-attribution files held just a name — treat as already expired so
    rules/prior take over, rather than pinning that driver forever)."""
    try:
        raw = path.read_text().strip()
    except FileNotFoundError:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("driver"):
            return {"driver": data["driver"], "set_at": float(data.get("set_at") or 0)}
    except json.JSONDecodeError:
        pass
    return {"driver": raw, "set_at": 0.0}  # legacy text file


def save_override(path: Path, driver: str) -> dict:
    data = {"driver": driver, "set_at": time.time()}
    path.write_text(json.dumps(data))
    return data


def _override_fresh(override: dict, now: float) -> bool:
    return bool(override.get("driver")) and (now - override.get("set_at", 0)) < OVERRIDE_TTL_S


# ----------------------------------------------------------------- schedule

def _load_schedule() -> list[dict]:
    """Rules: [{"days": [0-6, Mon=0], "start": "07:00", "end": "08:30",
    "driver": "Carson"}, ...]. File wins over env."""
    candidates = []
    sched_path = os.environ.get("DRIVER_SCHEDULE_PATH", "/data/driver_schedule.json")
    try:
        candidates.append(Path(sched_path).read_text())
    except OSError:
        pass
    env = os.environ.get("DRIVER_SCHEDULE_JSON", "").strip()
    if env:
        candidates.append(env)
    for raw in candidates:
        try:
            rules = json.loads(raw)
            if isinstance(rules, list):
                return rules
        except json.JSONDecodeError:
            logger.warning("invalid driver schedule JSON; ignoring")
    return []


def _match_schedule(ts: float) -> str | None:
    rules = _load_schedule()
    if not rules:
        return None
    local = datetime.fromtimestamp(ts, LOCAL_TZ)
    hm = local.strftime("%H:%M")
    for r in rules:
        try:
            days = r.get("days") or list(range(7))
            if local.weekday() in days and r["start"] <= hm < r["end"]:
                return r.get("driver")
        except (KeyError, TypeError):
            continue
    return None


# ------------------------------------------------------------- learned prior

def _bucket(ts: float) -> int:
    local = datetime.fromtimestamp(ts, LOCAL_TZ)
    return local.weekday() * 24 + local.hour


def _build_prior(conn) -> dict[int, str]:
    """Hour-of-week → dominant driver, from all historical driver samples."""
    rows = conn.execute(
        "SELECT ts, driver FROM events WHERE type = 'driver_sample'"
        " AND driver IS NOT NULL"
    ).fetchall()
    buckets: dict[int, dict[str, int]] = {}
    for r in rows:
        b = _bucket(r["ts"])
        buckets.setdefault(b, {})
        buckets[b][r["driver"]] = buckets[b].get(r["driver"], 0) + 1
    prior: dict[int, str] = {}
    for b, counts in buckets.items():
        total = sum(counts.values())
        top_driver, top_n = max(counts.items(), key=lambda kv: kv[1])
        if total >= MIN_BUCKET_SAMPLES and top_n / total >= MIN_BUCKET_SHARE:
            prior[b] = top_driver
    return prior


def _learned(conn, ts: float) -> str | None:
    global _prior, _prior_built_at
    now = time.time()
    if _prior is None or now - _prior_built_at > _PRIOR_REFRESH_S:
        try:
            _prior = _build_prior(conn)
            _prior_built_at = now
            logger.info("[attribution] learned prior covers %d/168 hour buckets",
                        len(_prior))
        except Exception:
            logger.exception("[attribution] prior build failed")
            _prior = _prior or {}
    return _prior.get(_bucket(ts))


def invalidate_prior() -> None:
    """Call after manual trip reassignments so corrections feed back in."""
    global _prior
    _prior = None


# ------------------------------------------------------------------ resolve

def resolve(conn, ts: float, override: dict, default: str | None) -> tuple[str | None, str]:
    """Return (driver, source) for a sample at ``ts``."""
    if _override_fresh(override, time.time()):
        return override["driver"], "manual"
    d = _match_schedule(ts)
    if d:
        return d, "schedule"
    d = _learned(conn, ts)
    if d:
        return d, "learned"
    return default, "default"
