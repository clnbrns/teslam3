"""Live charging-session detection + per-charger electricity pricing.

The historical importer fills ``charging_sessions`` from Tesla's data export,
but nothing used to write sessions observed *live* — so every cost page froze
at the last manual import. This module closes that gap:

- ``observe()`` is fed one poll (or telemetry) observation at a time and
  detects session boundaries from ``charging_state`` transitions plus
  ``charge_energy_added`` (which is cumulative within a session and resets
  when a new session starts).
- In-flight session state is persisted in SQLite (``charge_session_state``)
  so a poller restart mid-charge doesn't drop the session.
- ``rate_for()`` prices a session by charger type / location instead of the
  old flat home rate (which understated Supercharger sessions ~3–4×).

Rates are env-overridable; defaults reflect Fort Worth home service and
typical DFW Supercharger pricing.
"""
from __future__ import annotations

import logging
import math
import os

from tesla_fleet import db

logger = logging.getLogger(__name__)

HOME_RATE = float(os.environ.get("ELEC_HOME_RATE", "0.134"))
SUPERCHARGER_RATE = float(os.environ.get("ELEC_SUPERCHARGER_RATE", "0.42"))
AWAY_AC_RATE = float(os.environ.get("ELEC_AWAY_AC_RATE", "0.25"))

# Home geofence for labeling live sessions. Optional — without it, live AC
# sessions are labeled by charger type only.
HOME_LAT = os.environ.get("TESLA_HOME_LAT")
HOME_LON = os.environ.get("TESLA_HOME_LON")
HOME_RADIUS_M = float(os.environ.get("TESLA_HOME_RADIUS_M", "150"))

# Sessions smaller than this are connector blips, not real charges.
MIN_SESSION_KWH = 0.2

_SUPERCHARGER_TYPES = {"supercharger", "europe supercharger", "dc"}
_HOME_TYPES = {"tesla wall connector", "wall connector (home)", "gen 2 mobile connector",
               "mobile connector"}


def _is_home(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None or not HOME_LAT or not HOME_LON:
        return False
    try:
        hlat, hlon = float(HOME_LAT), float(HOME_LON)
    except ValueError:
        return False
    # Equirectangular approximation is plenty at 150 m scale.
    dx = (lon - hlon) * 111_320 * math.cos(math.radians(lat))
    dy = (lat - hlat) * 111_320
    return math.hypot(dx, dy) <= HOME_RADIUS_M


def rate_for(charger_type: str | None, location: str | None) -> float:
    """USD/kWh for a session, from charger type first, then location label."""
    ct = (charger_type or "").strip().lower()
    loc = (location or "").strip().lower()
    if ct in _SUPERCHARGER_TYPES or "supercharger" in ct:
        return SUPERCHARGER_RATE
    if ct in _HOME_TYPES or loc == "home":
        return HOME_RATE
    if ct or loc:
        return AWAY_AC_RATE
    # Unknown provenance — assume home, the overwhelmingly common case.
    return HOME_RATE


def session_cost(session: dict) -> float:
    """Priced cost of one charging_sessions row."""
    kwh = session.get("energy_kwh") or 0.0
    return kwh * rate_for(session.get("charger_type"), session.get("location"))


def observe(
    conn,
    vin: str,
    *,
    ts: float,
    charging_state: str | None,
    charge_energy_added: float | None,
    fast_charger_present: bool | None = None,
    lat: float | None = None,
    lon: float | None = None,
) -> dict | None:
    """Feed one observation; returns the finalized session dict when one closes.

    State machine (persisted per VIN in charge_session_state):
      no session + state=Charging            → open a session
      session    + energy grows              → extend
      session    + energy drops              → the old session ended unseen;
                                               close it, then open a new one
      session    + state!=Charging           → close
    """
    state = (charging_state or "").strip().lower()
    is_charging = state == "charging"
    cur = db.get_charge_state(conn, vin)

    closed: dict | None = None

    if cur:
        energy_dropped = (
            is_charging
            and charge_energy_added is not None
            and charge_energy_added < (cur["energy_kwh"] or 0) - 0.5
        )
        if not is_charging or energy_dropped:
            closed = _finalize(conn, vin, cur)
            cur = None

    if is_charging:
        if cur is None:
            cur = {
                "start_ts": ts,
                "last_ts": ts,
                "energy_kwh": charge_energy_added or 0.0,
                "charger_type": _charger_label(fast_charger_present, lat, lon),
                "location": "Home" if _is_home(lat, lon) else ("Away" if lat is not None else None),
                "lat": lat,
                "lon": lon,
            }
        else:
            cur["last_ts"] = ts
            if charge_energy_added is not None:
                cur["energy_kwh"] = max(cur["energy_kwh"] or 0.0, charge_energy_added)
            # Upgrade a DC label if fast charger shows up mid-session.
            if fast_charger_present:
                cur["charger_type"] = "Supercharger (V3+)"
        db.save_charge_state(conn, vin, cur)

    return closed


def _charger_label(fast_charger_present: bool | None,
                   lat: float | None, lon: float | None) -> str:
    if fast_charger_present:
        return "Supercharger (V3+)"
    if _is_home(lat, lon):
        return "Wall Connector (home)"
    return "AC (away)" if lat is not None else "AC"


def _finalize(conn, vin: str, cur: dict) -> dict | None:
    db.clear_charge_state(conn, vin)
    kwh = cur.get("energy_kwh") or 0.0
    if kwh < MIN_SESSION_KWH:
        return None
    session = {
        "vin": vin,
        "end_ts": cur["last_ts"],
        "energy_kwh": round(kwh, 3),
        "duration_s": int(max(0, cur["last_ts"] - cur["start_ts"])),
        "charger_type": cur.get("charger_type"),
        "location": cur.get("location"),
    }
    if db.record_charging(conn, session):
        logger.info("[charging] closed session: %.2f kWh @ %s (%s)",
                    kwh, session["charger_type"], session["location"] or "?")
        return session
    return None
