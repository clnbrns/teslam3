"""OpenStreetMap Overpass API helpers — speed cameras, school zones, road
maxspeed lookups, with disk caching so we don't hammer the public Overpass
endpoint on every dashboard refresh.

Data is queried by bounding box; the dashboard builds the bbox automatically
from the spread of stored GPS samples.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

OVERPASS_URL = os.environ.get(
    "OVERPASS_URL", "https://overpass-api.de/api/interpreter"
)
CACHE_DIR = Path(os.environ.get("OSM_CACHE_DIR", "/data/osm_cache"))
CACHE_TTL_SECONDS = int(os.environ.get("OSM_CACHE_TTL", str(7 * 24 * 3600)))


@dataclass(frozen=True)
class BBox:
    south: float
    west: float
    north: float
    east: float

    @classmethod
    def from_points(cls, points: list[tuple[float, float]],
                    pad_deg: float = 0.02) -> "BBox":
        if not points:
            # Sane default centered on DFW
            return cls(32.55, -97.55, 32.95, -96.65)
        lats = [p[0] for p in points]
        lons = [p[1] for p in points]
        return cls(
            min(lats) - pad_deg, min(lons) - pad_deg,
            max(lats) + pad_deg, max(lons) + pad_deg,
        )

    def overpass_str(self) -> str:
        return f"{self.south},{self.west},{self.north},{self.east}"


def _cache_path(key: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{key}.json"


def _read_cache(key: str) -> dict | None:
    p = _cache_path(key)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    if time.time() - data.get("_fetched_at", 0) > CACHE_TTL_SECONDS:
        return None
    return data


def _write_cache(key: str, payload: dict) -> None:
    payload["_fetched_at"] = time.time()
    _cache_path(key).write_text(json.dumps(payload))


def _query_overpass(query: str, key: str) -> dict:
    cached = _read_cache(key)
    if cached:
        return cached
    logger.info("Overpass query → %s", key)
    with httpx.Client(timeout=60) as client:
        r = client.post(OVERPASS_URL, data={"data": query})
        r.raise_for_status()
        payload = r.json()
    _write_cache(key, payload)
    return payload


def fetch_enforcement(bbox: BBox) -> dict:
    """Speed cameras, school zones, traffic-calming features in the bbox."""
    key = f"enforcement_{bbox.south:.3f}_{bbox.west:.3f}_{bbox.north:.3f}_{bbox.east:.3f}"
    bb = bbox.overpass_str()
    query = f"""
    [out:json][timeout:55];
    (
      node["highway"="speed_camera"]({bb});
      node["enforcement"="maxspeed"]({bb});
      way["amenity"="school"]({bb});
      node["amenity"="school"]({bb});
      node["traffic_calming"]({bb});
      way["traffic_calming"]({bb});
    );
    out center tags;
    """
    return _query_overpass(query, key)


def fetch_road_speeds(bbox: BBox) -> dict:
    """All highways with a known maxspeed tag in the bbox.

    Returns Overpass `out geom` payload — each way includes its full geometry
    so we can snap GPS samples to the nearest road segment locally.
    """
    key = f"roads_{bbox.south:.3f}_{bbox.west:.3f}_{bbox.north:.3f}_{bbox.east:.3f}"
    bb = bbox.overpass_str()
    query = f"""
    [out:json][timeout:90];
    (
      way["highway"]["maxspeed"]({bb});
    );
    out geom tags;
    """
    return _query_overpass(query, key)


def normalize_enforcement(payload: dict) -> list[dict]:
    """Flatten Overpass response to a simple list of features for the UI."""
    out = []
    for el in payload.get("elements", []):
        if el.get("type") == "node":
            lat, lon = el.get("lat"), el.get("lon")
        else:  # way → use the computed center
            c = el.get("center") or {}
            lat, lon = c.get("lat"), c.get("lon")
        if lat is None or lon is None:
            continue
        tags = el.get("tags") or {}
        kind = (
            "camera" if tags.get("highway") == "speed_camera" or tags.get("enforcement") == "maxspeed"
            else "school" if tags.get("amenity") == "school"
            else "calming" if tags.get("traffic_calming")
            else "other"
        )
        out.append({
            "kind": kind,
            "lat": lat, "lon": lon,
            "name": tags.get("name") or tags.get("operator"),
            "limit_mph": _parse_maxspeed(tags.get("maxspeed")),
            "tags": tags,
        })
    return out


_MPH_PER_KMH = 0.621371


def _parse_maxspeed(val: str | None) -> int | None:
    """Parse OSM maxspeed values (e.g. '35 mph', '50', '60 km/h')."""
    if not val:
        return None
    s = val.strip().lower()
    try:
        if "mph" in s:
            return int(float(s.replace("mph", "").strip()))
        if "km/h" in s or "kmh" in s:
            return int(float(s.replace("km/h", "").replace("kmh", "").strip()) * _MPH_PER_KMH)
        # Plain number — OSM convention defaults to km/h, but US is mph.
        n = float(s)
        # Heuristic: US speed limits in mph rarely exceed 85; if over, assume km/h.
        return int(n if n <= 85 else n * _MPH_PER_KMH)
    except ValueError:
        return None
