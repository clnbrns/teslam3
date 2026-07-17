"""Reverse geocoding for trip start/end labels, via Nominatim (OSM).

Design constraints:
- Nominatim's public instance allows ~1 req/s with a real User-Agent.
  We enforce that with a module-level throttle AND a per-call budget so a
  cold /api/trips load never stalls for long — unresolved cells simply
  resolve on later refreshes.
- Results cache permanently in SQLite (``geocode_cache``) keyed by a
  ~100 m lat/lon cell, so steady-state label lookups cost zero requests.
- "Home" comes from the TESLA_HOME_LAT/LON geofence, never from Nominatim,
  so the family address never renders as a street name.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import httpx

from tesla_fleet import db
from tesla_fleet.charging import _is_home

logger = logging.getLogger(__name__)

NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org")
USER_AGENT = "tesla-fleet-dashboard/0.1 (personal, single-vehicle)"
CACHE_TTL_S = 180 * 24 * 3600  # places don't move; re-resolve twice a year
MIN_INTERVAL_S = 1.1           # Nominatim usage policy

_throttle_lock = threading.Lock()
_last_call = 0.0


def cell_key(lat: float, lon: float) -> str:
    """~100 m grid cell — close-together endpoints share one lookup."""
    return f"{lat:.3f},{lon:.3f}"


def _short_label(data: dict) -> str | None:
    """Collapse a Nominatim reverse result to a compact human label."""
    name = data.get("name")
    addr = data.get("address") or {}
    locality = (addr.get("suburb") or addr.get("neighbourhood")
                or addr.get("city") or addr.get("town") or addr.get("village"))
    if name:
        return name
    road = addr.get("road")
    if road and locality:
        return f"{road}, {locality}"
    return road or locality or None


def _fetch(lat: float, lon: float) -> str | None:
    global _last_call
    with _throttle_lock:
        wait = MIN_INTERVAL_S - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(
                f"{NOMINATIM_URL}/reverse",
                params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 17},
                headers={"User-Agent": USER_AGENT},
            )
            r.raise_for_status()
            return _short_label(r.json())
    except httpx.HTTPError:
        logger.warning("nominatim reverse failed for %.4f,%.4f", lat, lon)
        return None


def resolve(conn, lat: float | None, lon: float | None,
            budget: "Budget | None" = None) -> str | None:
    """Place label for a point: Home geofence → cache → (budgeted) Nominatim.

    Returns None when unknown and the budget didn't allow a fetch; the next
    call will retry. Caches empty results briefly-ish via the normal TTL so
    unresolvable water-tower coordinates don't refetch forever.
    """
    if lat is None or lon is None:
        return None
    if _is_home(lat, lon):
        return "Home"
    key = cell_key(lat, lon)
    cached = db.geocode_get(conn, key, CACHE_TTL_S)
    if cached is not None:
        return cached or None
    if budget is not None and not budget.take():
        return None
    name = _fetch(lat, lon)
    db.geocode_put(conn, key, name)
    return name


class Budget:
    """Caps new Nominatim fetches per API request."""

    def __init__(self, n: int):
        self.remaining = n

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True
