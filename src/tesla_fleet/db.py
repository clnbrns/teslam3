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

CREATE TABLE IF NOT EXISTS roi_state (
    vin TEXT PRIMARY KEY,
    total_miles REAL NOT NULL DEFAULT 0,
    total_kwh REAL NOT NULL DEFAULT 0,
    last_odometer REAL,
    last_charge_energy_added REAL
);
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


def driver_samples(conn: sqlite3.Connection, driver: str) -> list[dict]:
    """All driver_sample rows for a driver (used by the report endpoint)."""
    rows = conn.execute(
        "SELECT payload FROM events WHERE type = 'driver_sample'"
        " AND LOWER(driver) = LOWER(?) ORDER BY ts ASC", (driver,)
    ).fetchall()
    return [json.loads(r["payload"]) for r in rows]
