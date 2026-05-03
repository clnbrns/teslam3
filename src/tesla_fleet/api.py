"""FastAPI service exposing OAuth callback + vehicle endpoints + dashboard."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from tesla_fleet import db
from tesla_fleet.auth import build_authorize_url
from tesla_fleet.client import TeslaFleetClient
from tesla_fleet.config import Settings
from tesla_fleet.gas_prices import average_over, price_for
from tesla_fleet.monitor import RoiState
from tesla_fleet.tokens import TokenStore, exchange_code

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings()
    app.state.store = TokenStore(app.state.settings.token_store_path)
    db.init()
    yield


app = FastAPI(title="tesla-fleet", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def get_client() -> TeslaFleetClient:
    token = app.state.store.load()
    if token is None:
        raise HTTPException(401, "Not authenticated; visit /login")
    return TeslaFleetClient(app.state.settings, token, app.state.store)


@app.get("/", response_class=HTMLResponse)
def dashboard() -> FileResponse:
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/report", response_class=HTMLResponse)
def report_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "report.html")


@app.get("/roi", response_class=HTMLResponse)
def roi_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "roi.html")


@app.get("/events", response_class=HTMLResponse)
def events_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "events.html")


@app.get("/login")
def login() -> dict:
    url, state = build_authorize_url(app.state.settings)
    return {"authorize_url": url, "state": state}


@app.get("/callback")
async def callback(code: str = Query(...), state: str | None = None) -> dict:
    token = await exchange_code(code, app.state.settings)
    app.state.store.save(token)
    return {"ok": True, "state": state}


@app.get("/vehicles")
async def vehicles(client: TeslaFleetClient = Depends(get_client)) -> list[dict]:
    async with client:
        return await client.list_vehicles()


@app.get("/vehicles/{vin}")
async def vehicle_data(vin: str, client: TeslaFleetClient = Depends(get_client)) -> dict:
    async with client:
        return await client.vehicle_data(vin)


@app.post("/vehicles/{vin}/wake")
async def wake(vin: str, client: TeslaFleetClient = Depends(get_client)) -> dict:
    async with client:
        return await client.wake_up(vin)


@app.post("/vehicles/{vin}/command/{name}")
async def cmd(vin: str, name: str, payload: dict | None = None,
              client: TeslaFleetClient = Depends(get_client)) -> dict:
    async with client:
        return await client.command(vin, name, payload)


# --- Dashboard data endpoints ---

MPG_BASELINE = 28.0
ELEC_PRICE = 0.14


@app.get("/api/roi")
def api_roi() -> dict:
    """Lifetime ROI computed per-charging-session against the DFW gas price
    in effect at the time of that session. Falls back to the simple
    odometer/kWh totals when no charging history is loaded."""
    from tesla_fleet.gas_prices import DFW_WEEKLY

    with db.connect() as conn:
        sessions = db.all_charging_sessions(conn)
        sample_window = db.event_time_window(conn, "driver_sample")
        totals = db.get_roi(conn)

    sample_miles = totals["total_miles"]
    sample_kwh = totals["total_kwh"]
    # mi/kWh derived from the windowed sample (vehicle-data export period).
    mi_per_kwh = (sample_miles / sample_kwh) if sample_kwh > 0 else 3.5

    lifetime_kwh = sum(s["energy_kwh"] for s in sessions)
    lifetime_miles_est = lifetime_kwh * mi_per_kwh
    elec_cost = lifetime_kwh * ELEC_PRICE

    # Per-session gas-equivalent at historical DFW price.
    gas_cost = 0.0
    series: list[dict] = []
    for s in sessions:
        ts = s["end_ts"]
        kwh = s["energy_kwh"]
        gas_price = price_for(ts)
        miles = kwh * mi_per_kwh
        gas_for_session = (miles / MPG_BASELINE) * gas_price
        gas_cost += gas_for_session
        series.append({
            "ts": ts,
            "kwh": round(kwh, 2),
            "gas_price": gas_price,
            "elec_cost": round(kwh * ELEC_PRICE, 2),
            "gas_equiv_cost": round(gas_for_session, 2),
            "location": s.get("location"),
        })

    if sample_window[0] and sample_window[1]:
        sample_label = (f"sample window {datetime.fromtimestamp(sample_window[0]).date()}"
                        f" → {datetime.fromtimestamp(sample_window[1]).date()}")
    else:
        sample_label = "no sample window"

    return {
        "total_miles": round(lifetime_miles_est, 2),
        "total_kwh": round(lifetime_kwh, 3),
        "gas_equivalent_cost_usd": round(gas_cost, 2),
        "electric_cost_usd": round(elec_cost, 2),
        "savings_usd": round(gas_cost - elec_cost, 2),
        "sessions": len(sessions),
        "assumptions": {
            "gas_price_per_gal": round(gas_cost / max(lifetime_miles_est / MPG_BASELINE, 1e-9), 3),
            "gas_price_source": "DFW weekly avg, per-session historical",
            "mpg_baseline": MPG_BASELINE,
            "elec_price_per_kwh": ELEC_PRICE,
            "mi_per_kwh": round(mi_per_kwh, 3),
            "mi_per_kwh_source": sample_label,
        },
        "gas_price_history": [{"date": d.isoformat(), "price": p} for d, p in DFW_WEEKLY],
        "sessions_series": series,
    }


@app.get("/api/events")
def api_events(
    limit: int = 200,
    type: str | None = None,
    driver: str | None = None,
) -> list[dict]:
    with db.connect() as conn:
        return db.read_events(conn, limit=limit, type=type, driver=driver)


@app.get("/api/report/{driver}")
def api_report(driver: str) -> dict:
    """Aggregate driver telemetry into a report-card payload."""
    with db.connect() as conn:
        samples = db.driver_samples(conn, driver)
    if not samples:
        return {"driver": driver, "samples": 0, "grade": "—", "score": 0, "stats": {}}

    speeds = [s["speed_mph"] for s in samples if s.get("speed_mph") is not None]
    moving = [s for s in samples if (s.get("speed_mph") or 0) > 1]
    over_limit = [
        s for s in samples
        if s.get("speed_mph") is not None and s.get("speed_limit_mph")
        and s["speed_mph"] > s["speed_limit_mph"]
    ]
    hard_brakes = [s for s in samples if (s.get("event") or {}).get("type") == "hard_brake"]
    rapid_accels = [s for s in samples if (s.get("event") or {}).get("type") == "rapid_accel"]

    timestamps = sorted(s["ts"] for s in samples if "ts" in s)

    # Drive time + miles: integrate over consecutive moving samples; gaps
    # over 5 min between samples are treated as a parked break.
    moving_sorted = sorted(moving, key=lambda s: s["ts"])
    drive_seconds = 0.0
    miles_est = 0.0
    prev = None
    for s in moving_sorted:
        if prev is not None:
            dt = s["ts"] - prev["ts"]
            if 0 < dt < 300:
                drive_seconds += dt
                avg_speed = (s["speed_mph"] + prev["speed_mph"]) / 2
                miles_est += avg_speed * (dt / 3600)
        prev = s

    # 100-point score: start at 100, deduct.
    score = 100
    score -= 4 * len(hard_brakes)
    score -= 3 * len(rapid_accels)
    score -= 2 * len(over_limit)
    score = max(0, min(100, score))

    if score >= 93: grade = "A"
    elif score >= 85: grade = "B"
    elif score >= 75: grade = "C"
    elif score >= 65: grade = "D"
    else: grade = "F"

    return {
        "driver": driver,
        "samples": len(samples),
        "grade": grade,
        "score": score,
        "first_seen": timestamps[0] if timestamps else None,
        "last_seen": timestamps[-1] if timestamps else None,
        "stats": {
            "max_speed_mph": max(speeds) if speeds else 0,
            "avg_speed_mph": round(sum(speeds) / len(speeds), 1) if speeds else 0,
            "hard_brakes": len(hard_brakes),
            "rapid_accels": len(rapid_accels),
            "over_limit_count": len(over_limit),
            "duration_minutes": round(drive_seconds / 60, 1),
            "miles_estimate": round(miles_est, 1),
        },
        "recent_incidents": [
            {
                "ts": s["ts"],
                "type": s["event"]["type"],
                "delta_mph_per_s": s["event"]["delta_mph_per_s"],
                "speed_mph": s.get("speed_mph"),
                "speed_limit_mph": s.get("speed_limit_mph"),
                "maps_url": s.get("maps_url"),
            }
            for s in (hard_brakes + rapid_accels)[-10:]
        ],
    }


@app.get("/api/status/{vin}")
async def api_status(vin: str, client: TeslaFleetClient = Depends(get_client)) -> dict:
    """Compact status payload tailored for the dashboard."""
    async with client:
        try:
            data = await client.vehicle_data(vin)
        except Exception as e:
            return {"online": False, "error": str(e)}
    drive = data.get("drive_state") or {}
    veh = data.get("vehicle_state") or {}
    charge = data.get("charge_state") or {}
    climate = data.get("climate_state") or {}
    return {
        "online": True,
        "display_name": veh.get("vehicle_name") or data.get("display_name"),
        "vin": vin,
        "speed_mph": drive.get("speed"),
        "shift_state": drive.get("shift_state") or "P",
        "lat": drive.get("latitude"),
        "lon": drive.get("longitude"),
        "speed_limit_mph": (drive.get("speed_limit_mode") or {}).get("current_limit_mph"),
        "battery_level": charge.get("battery_level"),
        "battery_range_mi": charge.get("battery_range"),
        "charging_state": charge.get("charging_state"),
        "charge_energy_added": charge.get("charge_energy_added"),
        "odometer": veh.get("odometer"),
        "inside_temp_f": climate.get("inside_temp"),
        "locked": veh.get("locked"),
    }
