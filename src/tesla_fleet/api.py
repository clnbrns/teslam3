"""FastAPI service exposing OAuth callback + vehicle endpoints + dashboard."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import asyncio
import base64
import os
import secrets as secrets_mod
import time

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from tesla_fleet import db, osm, telemetry
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
    # Active-driver override: set via POST /api/active-driver, used by the
    # poller when writing samples. Persists in /data so it survives restarts.
    driver_path = Path(app.state.settings.token_store_path).parent / ".active_driver"
    app.state.driver_path = driver_path
    try:
        app.state.active_driver = driver_path.read_text().strip() or app.state.settings.default_driver
    except FileNotFoundError:
        app.state.active_driver = app.state.settings.default_driver

    # Background poller — captures live drives into SQLite.
    app.state.poll_task = None
    vin = os.environ.get("TESLA_VIN", "").strip()
    interval = float(os.environ.get("TESLA_POLL_INTERVAL_SECONDS", "30") or 30)
    if vin:
        logger.info("Starting telemetry poller for %s every %.0fs", vin, interval)
        app.state.poll_task = asyncio.create_task(_poll_loop(vin, interval))
    else:
        logger.info("TESLA_VIN not set; background poller disabled")

    try:
        yield
    finally:
        if app.state.poll_task:
            app.state.poll_task.cancel()
            try:
                await app.state.poll_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Goblin M3P", lifespan=lifespan)


# --- HTTP Basic Auth on the dashboard ---
# SITE_USER / SITE_PASSWORD env vars enable the gate.
# Endpoints Tesla calls (callback, well-known key, telemetry ingest) are
# never gated since Tesla can't send Basic Auth credentials.
PUBLIC_PATH_PREFIXES = (
    "/callback",
    "/.well-known/",
    "/telemetry",         # Tesla telemetry push
    "/static/",           # CSS / JS for the gated pages still need to load
    "/healthz",           # Railway healthcheck
)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        user = os.environ.get("SITE_USER", "")
        pw = os.environ.get("SITE_PASSWORD", "")
        if not pw:  # gate disabled when no password set
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:]).decode("utf-8")
                got_user, _, got_pw = decoded.partition(":")
                if (
                    secrets_mod.compare_digest(got_user, user or got_user)
                    and secrets_mod.compare_digest(got_pw, pw)
                ):
                    return await call_next(request)
            except Exception:
                pass

        return Response(
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Goblin M3P"'},
            content="Authentication required",
        )


MOBILE_UA_HINT = ("iphone", "android", "mobile", "ipod")
NON_MOBILE_PATHS = ("/static/", "/api/", "/healthz", "/callback",
                    "/.well-known/", "/telemetry", "/login", "/login.json")


class MobileURLMiddleware(BaseHTTPMiddleware):
    """UA-based redirect to /m/* paths and internal rewrite back to / for static serving.

    - Mobile UA hitting `/foo` → 302 to `/m/foo` (sticky via "layout=mobile" cookie)
    - Anything under `/m/...` → request.scope.path rewritten to strip `/m`
      so the existing route handlers serve the right file, plus we set a
      `layout=mobile` cookie that the front-end JS reads to flip body class.
    - `?desktop=1` query (or "layout=desktop" cookie) opts out of the redirect.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        ua = (request.headers.get("user-agent") or "").lower()
        is_mobile_ua = any(t in ua for t in MOBILE_UA_HINT)
        layout_cookie = request.cookies.get("layout", "")
        wants_desktop = (
            request.query_params.get("desktop") == "1"
            or layout_cookie == "desktop"
        )

        # Stash desktop opt-out into a cookie that wins on subsequent loads.
        set_desktop_cookie = request.query_params.get("desktop") == "1"

        # Internal rewrite for /m/* → /* so existing handlers fire.
        rewrote_to_mobile = False
        if path == "/m" or path.startswith("/m/"):
            new_path = path[2:] or "/"
            request.scope["path"] = new_path
            request.scope["raw_path"] = new_path.encode()
            rewrote_to_mobile = True

        # Redirect bare desktop URL to /m/* for mobile users (one time per session).
        elif (
            is_mobile_ua
            and not wants_desktop
            and not any(path.startswith(p) for p in NON_MOBILE_PATHS)
        ):
            target = f"/m{path}" if path != "/" else "/m/"
            return RedirectResponse(target, status_code=302)

        response = await call_next(request)
        # Don't pin a layout cookie any more; the front-end decides layout
        # from viewport width, which means the same browser can switch between
        # mobile / desktop UI just by resizing.
        if set_desktop_cookie:
            response.set_cookie("layout", "desktop", max_age=86400, samesite="lax")
        return response


app.add_middleware(MobileURLMiddleware)
app.add_middleware(BasicAuthMiddleware)


class StaticCacheControl(BaseHTTPMiddleware):
    """Short edge cache for static assets so Cloudflare doesn't pin stale
    JS/CSS for hours after a deploy. Versioned ?v= URLs let it cache aggressively."""
    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/static/"):
            if request.url.query:
                # Versioned URL — safe to cache long
                resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                # Unversioned — short cache, must revalidate
                resp.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
        return resp


app.add_middleware(StaticCacheControl)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def get_client() -> TeslaFleetClient:
    token = app.state.store.load()
    if token is None:
        raise HTTPException(401, "Not authenticated; visit /login")
    return TeslaFleetClient(app.state.settings, token, app.state.store)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/active-driver")
def get_active_driver() -> dict:
    return {"driver": app.state.active_driver,
            "options": ["Colin", "Lindsey", "Carson"]}


@app.post("/api/poll-now")
async def api_poll_now() -> dict:
    """Manually trigger one immediate poll. Counts against API budget but
    bypasses the background poller's long sleep — useful when you want
    live state without waiting for the next scheduled tick."""
    vin = os.environ.get("TESLA_VIN", "").strip()
    if not vin:
        raise HTTPException(503, "TESLA_VIN not set")
    token = app.state.store.load()
    if token is None:
        raise HTTPException(401, "No OAuth token; visit /login")
    client = TeslaFleetClient(app.state.settings, token, app.state.store)
    async with client:
        try:
            data = await client.vehicle_data(vin)
        except Exception as e:
            return {"ok": False, "error": str(e)}
    drive = data.get("drive_state") or {}
    veh = data.get("vehicle_state") or {}
    charge = data.get("charge_state") or {}
    now = time.time()
    record = {
        "type": "manual_refresh",
        "ts": now,
        "vin": vin,
        "driver": getattr(app.state, "active_driver", None),
        "speed_mph": drive.get("speed"),
        "shift_state": drive.get("shift_state"),
        "battery_level": charge.get("battery_level"),
        "battery_range_mi": charge.get("battery_range"),
        "charging_state": charge.get("charging_state"),
        "odometer": veh.get("odometer"),
        "gps": ({"lat": drive["latitude"], "lon": drive["longitude"]}
                if drive.get("latitude") is not None else None),
    }
    with db.connect() as conn:
        db.record_event(conn, record)
        odo = veh.get("odometer")
        kwh = charge.get("charge_energy_added")
        if odo is not None or kwh is not None:
            db.update_roi(conn, vin, odometer_mi=odo, charge_energy_added_kwh=kwh)
    return {"ok": True, **{k: v for k, v in record.items() if k != "gps"}}


@app.post("/api/active-driver")
async def set_active_driver(req: Request) -> dict:
    body = await req.json()
    driver = (body.get("driver") or "").strip()
    if driver:
        app.state.active_driver = driver
        try:
            app.state.driver_path.write_text(driver)
        except Exception:
            logger.exception("failed to persist active driver")
    return {"driver": app.state.active_driver}


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    # Trips is the landing page. /map and /dashboard are still reachable.
    return FileResponse(STATIC_DIR / "trips.html")


@app.get("/dashboard", response_class=HTMLResponse)
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


@app.get("/fsd", response_class=HTMLResponse)
def fsd_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "fsd.html")


@app.get("/attention", response_class=HTMLResponse)
def attention_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "attention.html")


@app.get("/charging", response_class=HTMLResponse)
def charging_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "charging.html")


@app.get("/trips", response_class=HTMLResponse)
def trips_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "trips.html")


@app.get("/map", response_class=HTMLResponse)
def map_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "map.html")


@app.get("/login")
def login() -> RedirectResponse:
    url, _ = build_authorize_url(app.state.settings)
    return RedirectResponse(url, status_code=302)


@app.get("/login.json")
def login_json() -> dict:
    url, state = build_authorize_url(app.state.settings)
    return {"authorize_url": url, "state": state}


@app.get("/callback", response_class=HTMLResponse)
async def callback(code: str = Query(...), state: str | None = None) -> str:
    token = await exchange_code(code, app.state.settings)
    app.state.store.save(token)
    return """
<!doctype html><meta charset="utf-8">
<title>Connected · Goblin M3P</title>
<style>
  body { background:#0a0a0a; color:#e8ebe9; font:16px -apple-system,system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
  .card { background:#141414; border:1px solid #3d6b54; border-left:3px solid #c8232c;
          border-radius:8px; padding:32px 40px; max-width:480px; }
  h1 { margin:0 0 8px; font-weight:600; letter-spacing:-0.02em; }
  .ok { color:#3d6b54; font-size:11px; letter-spacing:.2em; text-transform:uppercase; }
  a { display:inline-block; margin-top:18px; padding:8px 16px; background:#2f5240;
      color:#fff; text-decoration:none; border-radius:4px; font-size:13px;
      letter-spacing:.12em; text-transform:uppercase; }
</style>
<div class="card">
  <div class="ok">✓ Connected</div>
  <h1>Tesla account linked</h1>
  <p>Token saved. The dashboard can now read live vehicle data.</p>
  <a href="/">Open dashboard</a>
</div>
"""


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


# --- Tesla Fleet Telemetry: registration + push ingest ---

@app.post("/api/telemetry/register/{vin}")
async def telemetry_register(
    vin: str,
    hostname: str | None = None,
    client: TeslaFleetClient = Depends(get_client),
) -> dict:
    """Register the streaming telemetry config for a VIN against `hostname`
    (defaults to settings.public_hostname)."""
    host = hostname or app.state.settings.public_hostname
    if not host:
        raise HTTPException(400, "Set TESLA_PUBLIC_HOSTNAME or pass ?hostname=")
    async with client:
        return await telemetry.register(client, host, vin)


@app.delete("/api/telemetry/register/{vin}")
async def telemetry_unregister(
    vin: str, client: TeslaFleetClient = Depends(get_client)
) -> dict:
    async with client:
        return await telemetry.unregister(client, vin)


@app.post("/telemetry")
async def telemetry_ingest(req: Request) -> dict:
    """Receive a telemetry payload pushed by Tesla via our Fleet Telemetry
    Server (FTS). The FTS POSTs decoded protobuf records here, one batch
    per car connection event. Idempotent on (vin, ts).

    Auth: requires X-Telemetry-Token header matching the TELEMETRY_TOKEN
    env var. FTS adds this header automatically via its dispatcher config.
    """
    expected = os.environ.get("TELEMETRY_TOKEN", "")
    if expected:
        got = req.headers.get("x-telemetry-token", "")
        if not secrets_mod.compare_digest(got, expected):
            raise HTTPException(401, "bad telemetry token")
    payload = await req.json()
    # Two payload shapes are accepted:
    #   1. FTS HTTP dispatcher: {"vin": "...", "data": [{...}], "createdAt": ...}
    #      (legacy/pre-2026 shape — already handled by telemetry.ingest_payload)
    #   2. FTS 2026+ HTTP sink: array of records, each with vehicle_data fields.
    if isinstance(payload, list):
        written = 0
        for record in payload:
            written += telemetry.ingest_payload(
                record, default_driver=getattr(app.state, "active_driver", None),
            )
        return {"ok": True, "written": written, "batch_size": len(payload)}
    written = telemetry.ingest_payload(
        payload, default_driver=getattr(app.state, "active_driver", None),
    )
    return {"ok": True, "written": written}


@app.get("/.well-known/appspecific/com.tesla.3p.public-key.pem",
         response_class=PlainTextResponse)
def public_key() -> str:
    """Serve the partner public key Tesla validates for command signing."""
    p = STATIC_DIR / "well-known" / "com.tesla.3p.public-key.pem"
    if not p.exists():
        raise HTTPException(404, "public key not yet provisioned")
    return p.read_text()


# --- Dashboard data endpoints ---

MPG_BASELINE = 28.0
ELEC_PRICE = 0.134  # blended home rate, Fort Worth

# --- Per-mile operating cost model (M3 Performance) ---
# Electricity: 3.0 mi/kWh real-world avg → ~$0.045/mi
# Tires: $1,293.96 (Continental DWS06+ set, Jan + Feb 2026) over 50K mi life
# = $0.0259/mi
M3P_MI_PER_KWH = 3.0
TIRE_SET_COST = 591.98 + 701.98     # $1,293.96
TIRE_LIFE_MILES = 50_000
TIRE_PER_MI = TIRE_SET_COST / TIRE_LIFE_MILES         # $0.0259
ELEC_PER_MI = ELEC_PRICE / M3P_MI_PER_KWH             # $0.0447
COST_PER_MI = ELEC_PER_MI + TIRE_PER_MI               # ≈ $0.0706


def _trip_cost(miles: float, battery_used_pct: float | None = None) -> dict:
    """Cost of one trip. Uses real battery delta when available, else miles × rate."""
    if battery_used_pct and battery_used_pct > 0:
        kwh = battery_used_pct / 100 * 75.0      # M3P pack approx
        elec = kwh * ELEC_PRICE
    else:
        elec = miles * ELEC_PER_MI
    tire = miles * TIRE_PER_MI
    return {
        "electricity_usd": round(elec, 2),
        "tire_wear_usd": round(tire, 2),
        "total_usd": round(elec + tire, 2),
    }

# One-off costs tied to owning + operating the Model 3 that aren't captured
# in the per-mile maintenance rate. Add new entries here as receipts accrue.
TESLA_EXPENSES = [
    {"date": "2025-08-15", "amount": 950.00, "category": "infrastructure",
     "label": "Wall Connector + electrician install"},
    {"date": "2026-01-09", "amount": 591.98, "category": "tires",
     "label": "Continental ExtremeContact DWS06 Plus 275/30ZR20XL (front pair)"},
    {"date": "2026-02-12", "amount": 701.98, "category": "tires",
     "label": "Continental ExtremeContact DWS06 Plus (rear pair)"},
]


@app.get("/api/roi")
def api_roi() -> dict:
    """Lifetime ROI computed per-charging-session against the DFW gas price
    in effect at the time of that session. Compares fuel + maintenance
    against multiple ICE vehicles."""
    from tesla_fleet.comparison_vehicles import (
        COMPARISONS, TESLA_MAINT_PER_MI, fuel_price,
    )
    from tesla_fleet.gas_prices import DFW_WEEKLY

    with db.connect() as conn:
        sessions = db.all_charging_sessions(conn)
        sample_window = db.event_time_window(conn, "driver_sample")
        totals = db.get_roi(conn)

    sample_miles = totals["total_miles"]
    sample_kwh = totals["total_kwh"]
    mi_per_kwh = (sample_miles / sample_kwh) if sample_kwh > 0 else 3.5

    lifetime_kwh = sum(s["energy_kwh"] for s in sessions)
    lifetime_miles_est = lifetime_kwh * mi_per_kwh
    elec_cost = lifetime_kwh * ELEC_PRICE
    tesla_maint_cost = lifetime_miles_est * TESLA_MAINT_PER_MI
    tesla_one_offs = sum(e["amount"] for e in TESLA_EXPENSES)
    tesla_total_cost = elec_cost + tesla_maint_cost + tesla_one_offs

    # Per-vehicle running totals (fuel + maintenance, both accumulated
    # per-session so the cumulative chart stays smooth).
    comp_fuel: dict[str, float] = {v.key: 0.0 for v in COMPARISONS}
    comp_maint: dict[str, float] = {v.key: 0.0 for v in COMPARISONS}
    cumulative_savings: dict[str, float] = {v.key: 0.0 for v in COMPARISONS}
    cumulative_tesla = 0.0
    series: list[dict] = []

    for s in sessions:
        ts = s["end_ts"]
        kwh = s["energy_kwh"]
        regular_price = price_for(ts)
        miles = kwh * mi_per_kwh
        sess_elec = kwh * ELEC_PRICE
        sess_tesla_maint = miles * TESLA_MAINT_PER_MI
        sess_tesla_total = sess_elec + sess_tesla_maint
        cumulative_tesla += sess_tesla_total

        per_comp = {}
        for v in COMPARISONS:
            if v.fuel == "electric":
                # Electric peer: same kWh price, but the peer's efficiency
                # determines kWh used to cover the same miles.
                kwh_for_peer = miles / v.mi_per_kwh if v.mi_per_kwh else kwh
                sess_fuel = kwh_for_peer * ELEC_PRICE
                gp_or_kwh = ELEC_PRICE
            else:
                gp_or_kwh = fuel_price(ts, v.fuel, regular_price)
                sess_fuel = (miles / v.mpg) * gp_or_kwh
            sess_maint = miles * v.maint_per_mi
            sess_total = sess_fuel + sess_maint
            comp_fuel[v.key] += sess_fuel
            comp_maint[v.key] += sess_maint
            cumulative_savings[v.key] += sess_total - sess_tesla_total
            per_comp[v.key] = {
                "gas_price": gp_or_kwh,
                "gas_cost": round(sess_fuel, 2),
                "maint_cost": round(sess_maint, 2),
                "ice_total": round(sess_total, 2),
                "savings": round(sess_total - sess_tesla_total, 2),
                "cumulative_savings": round(cumulative_savings[v.key], 2),
            }

        series.append({
            "ts": ts,
            "date": datetime.fromtimestamp(ts).date().isoformat(),
            "kwh": round(kwh, 2),
            "miles_est": round(miles, 2),
            "regular_price": regular_price,
            "elec_cost": round(sess_elec, 2),
            "tesla_maint": round(sess_tesla_maint, 2),
            "cumulative_tesla": round(cumulative_tesla, 2),
            "location": s.get("location"),
            "comparisons": per_comp,
        })

    if sample_window[0] and sample_window[1]:
        sample_label = (f"sample window {datetime.fromtimestamp(sample_window[0]).date()}"
                        f" → {datetime.fromtimestamp(sample_window[1]).date()}")
    else:
        sample_label = "no sample window"

    comparisons = [
        {
            "key": v.key,
            "name": v.name,
            "mpg": v.mpg,
            "fuel": v.fuel,
            "mi_per_kwh": v.mi_per_kwh,
            "maint_per_mi": v.maint_per_mi,
            "note": v.note,
            "gas_cost_usd": round(comp_fuel[v.key], 2),
            "maint_cost_usd": round(comp_maint[v.key], 2),
            "ice_total_usd": round(comp_fuel[v.key] + comp_maint[v.key], 2),
            "savings_usd": round(comp_fuel[v.key] + comp_maint[v.key] - tesla_total_cost, 2),
        }
        for v in COMPARISONS
    ]

    # Default "headline" comparison = first one in the list (generic sedan).
    headline = comparisons[0]

    first_ts = sessions[0]["end_ts"] if sessions else None
    last_ts = sessions[-1]["end_ts"] if sessions else None
    return {
        "first_session": first_ts,
        "last_session": last_ts,
        "total_miles": round(lifetime_miles_est, 2),
        "total_kwh": round(lifetime_kwh, 3),
        "gas_equivalent_cost_usd": headline["gas_cost_usd"],
        "electric_cost_usd": round(elec_cost, 2),
        "tesla_maint_cost_usd": round(tesla_maint_cost, 2),
        "tesla_one_off_cost_usd": round(tesla_one_offs, 2),
        "tesla_one_off_expenses": TESLA_EXPENSES,
        "tesla_total_cost_usd": round(tesla_total_cost, 2),
        "savings_usd": headline["savings_usd"],
        "sessions": len(sessions),
        "assumptions": {
            "gas_price_per_gal": round(
                headline["gas_cost_usd"] / max(lifetime_miles_est / max(headline["mpg"], 1), 1e-9), 3
            ) if headline.get("mpg") else 0,
            "gas_price_source": "DFW weekly avg, per-session historical",
            "mpg_baseline": headline["mpg"],
            "elec_price_per_kwh": ELEC_PRICE,
            "mi_per_kwh": round(mi_per_kwh, 3),
            "mi_per_kwh_source": sample_label,
            "tesla_maint_per_mi": TESLA_MAINT_PER_MI,
            "ice_maint_per_mi": headline["maint_per_mi"],
            "maint_source": "Edmunds True Cost to Own / AAA Driving Costs 2024-25",
        },
        "comparisons": comparisons,
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


@app.get("/api/fsd")
def api_fsd() -> dict:
    """Aggregate FSD/Autopilot usage and profile preferences per driver."""
    with db.connect() as conn:
        return db.fsd_summary(conn)


@app.get("/api/attention")
def api_attention() -> dict:
    """Cabin-camera attentiveness summary (Tesla SW 2026.8+)."""
    with db.connect() as conn:
        return db.attention_summary(conn)


CHARGER_LABEL_MAP = {
    # Tesla's export uses internal taxonomy that doesn't always match real
    # geography or current product names. Normalize to user-facing labels.
    "Europe Supercharger": "Supercharger (V3+)",
    "Supercharger":         "Supercharger (V2)",
    "Tesla Wall Connector": "Wall Connector (home)",
    "Gen 2 Mobile Connector": "Mobile Connector",
    "General - AC power":   "Generic AC outlet",
}


def _norm_charger(raw: str | None) -> str:
    if not raw:
        return "Unknown"
    return CHARGER_LABEL_MAP.get(raw, raw)


@app.get("/api/charging")
def api_charging() -> dict:
    """Sessions, location/charger split, weekly cost trend, avg charging speed."""
    from collections import defaultdict
    with db.connect() as conn:
        sessions = db.all_charging_sessions(conn)

    if not sessions:
        return {"sessions": 0}

    # Location split (Home / Away / Supercharger / Other).
    by_location: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "kwh": 0.0, "cost": 0.0})
    by_charger: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "kwh": 0.0})
    weekly: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "kwh": 0.0, "cost": 0.0})
    speeds: list[dict] = []

    for s in sessions:
        kwh = s["energy_kwh"] or 0
        cost = kwh * ELEC_PRICE
        loc = s.get("location") or "Unknown"
        ct = _norm_charger(s.get("charger_type"))
        by_location[loc]["sessions"] += 1
        by_location[loc]["kwh"] += kwh
        by_location[loc]["cost"] += cost
        by_charger[ct]["sessions"] += 1
        by_charger[ct]["kwh"] += kwh

        d = datetime.fromtimestamp(s["end_ts"]).date()
        # ISO week key (YYYY-WW)
        wk = f"{d.year}-W{d.isocalendar()[1]:02d}"
        weekly[wk]["sessions"] += 1
        weekly[wk]["kwh"] += kwh
        weekly[wk]["cost"] += cost

        dur_h = (s.get("duration_s") or 0) / 3600
        if dur_h > 0.05 and kwh > 0.1:
            avg_kw = kwh / dur_h
            # Cap implausibly high "speeds" — these come from sessions where
            # the duration field underreported (car kept the connector latched
            # but charging stopped). Anything > 280 kW is impossible on this car.
            if avg_kw <= 280:
                speeds.append({"kw": round(avg_kw, 1), "kwh": round(kwh, 2),
                               "duration_h": round(dur_h, 2), "charger": ct})

    return {
        "sessions": len(sessions),
        "by_location": [{"key": k, **v, "kwh": round(v["kwh"], 1),
                          "cost": round(v["cost"], 2)}
                         for k, v in sorted(by_location.items(), key=lambda kv: -kv[1]["kwh"])],
        "by_charger": [{"key": k, **v, "kwh": round(v["kwh"], 1)}
                        for k, v in sorted(by_charger.items(), key=lambda kv: -kv[1]["kwh"])],
        "weekly": [{"week": w, **v, "kwh": round(v["kwh"], 2),
                     "cost": round(v["cost"], 2)}
                    for w, v in sorted(weekly.items())],
        "speed_distribution": _bucket_speeds(speeds),
        "total_kwh": round(sum(s["energy_kwh"] for s in sessions), 1),
        "total_cost": round(sum(s["energy_kwh"] * ELEC_PRICE for s in sessions), 2),
    }


def _bucket_speeds(rows: list[dict]) -> list[dict]:
    """Histogram of avg session kW across speed buckets (Level 1/2/DC)."""
    buckets = [
        ("L1 (≤2 kW)", 0, 2),
        ("L2 slow (2-7)", 2, 7),
        ("L2 fast (7-15)", 7, 15),
        ("L2 max (15-22)", 15, 22),
        ("DC mid (22-100)", 22, 100),
        ("DC fast (100+)", 100, 1000),
    ]
    out = []
    for label, lo, hi in buckets:
        in_b = [r for r in rows if lo <= r["kw"] < hi]
        out.append({
            "bucket": label,
            "sessions": len(in_b),
            "kwh": round(sum(r["kwh"] for r in in_b), 1),
            "avg_kw": round(sum(r["kw"] for r in in_b) / len(in_b), 1) if in_b else 0,
        })
    return out


@app.get("/api/efficiency")
def api_efficiency() -> dict:
    """mi/kWh by week, derived from charging sessions + sample-window odometer."""
    with db.connect() as conn:
        sessions = db.all_charging_sessions(conn)
        sample_window = db.event_time_window(conn, "driver_sample")
        totals = db.get_roi(conn)

    sample_miles = totals["total_miles"]
    sample_kwh = totals["total_kwh"]
    if sample_kwh <= 0:
        return {"weekly": [], "lifetime_mi_per_kwh": 0}

    # Lifetime efficiency anchored to sample window.
    mi_per_kwh = sample_miles / sample_kwh

    # Weekly aggregation of charging energy → multiply by mi_per_kwh for est miles.
    from collections import defaultdict
    weekly: dict[str, dict] = defaultdict(lambda: {"kwh": 0.0, "sessions": 0})
    for s in sessions:
        d = datetime.fromtimestamp(s["end_ts"]).date()
        wk = f"{d.year}-W{d.isocalendar()[1]:02d}"
        weekly[wk]["kwh"] += s["energy_kwh"] or 0
        weekly[wk]["sessions"] += 1

    out = []
    for wk in sorted(weekly):
        kwh = weekly[wk]["kwh"]
        miles = kwh * mi_per_kwh
        out.append({
            "week": wk,
            "kwh": round(kwh, 2),
            "miles_est": round(miles, 1),
            "sessions": weekly[wk]["sessions"],
            # mi/kWh is constant in this estimate since we don't have weekly
            # odometer deltas. Real seasonal variation will land once telemetry
            # streams Odometer + ACChargingEnergyIn fields week by week.
            "mi_per_kwh": round(mi_per_kwh, 3),
        })
    return {
        "weekly": out,
        "lifetime_mi_per_kwh": round(mi_per_kwh, 3),
        "note": (
            "Per-week mi/kWh is constant in this view because the data export "
            "lacks weekly odometer snapshots. Live telemetry will resolve this."
        ),
    }


@app.get("/api/per-driver-cost")
def api_per_driver_cost() -> dict:
    """Allocate the lifetime electric bill by per-driver miles."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT driver, COUNT(*) AS samples FROM events"
            " WHERE type = 'driver_sample' AND driver IS NOT NULL"
            " GROUP BY driver"
        ).fetchall()
        totals = db.get_roi(conn)
        sessions = db.all_charging_sessions(conn)

    if not rows:
        return {"drivers": [], "total_cost": 0}

    # Use sample share as a proxy for miles share — same assumption used in
    # the per-driver report, since sample density tracks drive time.
    total_samples = sum(r["samples"] for r in rows)
    lifetime_kwh = sum(s["energy_kwh"] for s in sessions)
    lifetime_cost = lifetime_kwh * ELEC_PRICE
    sample_miles = totals["total_miles"]
    sample_kwh = totals["total_kwh"]
    mi_per_kwh = (sample_miles / sample_kwh) if sample_kwh > 0 else 3.0
    lifetime_miles = lifetime_kwh * mi_per_kwh

    out = []
    for r in rows:
        share = r["samples"] / total_samples
        miles = lifetime_miles * share
        cost = lifetime_cost * share
        out.append({
            "driver": r["driver"],
            "share_pct": round(share * 100, 1),
            "miles": round(miles, 1),
            "kwh": round(lifetime_kwh * share, 1),
            "cost_usd": round(cost, 2),
        })
    out.sort(key=lambda x: -x["cost_usd"])
    return {
        "drivers": out,
        "total_cost": round(lifetime_cost, 2),
        "total_miles": round(lifetime_miles, 1),
        "method": "sample-weighted (drive time per driver from telemetry)",
    }


def _derive_odometer_segments(conn, cost_per_mi: float, mi_per_kwh: float) -> list[dict]:
    """Synthesize drive segments from polled events.

    Primary signal: odometer delta (most accurate).
    Fallback signal: battery-level drop while not charging — used to reconstruct
    drives where heartbeats lack odometer (legacy schema).
    Estimated miles for fallback = battery_delta_pct * battery_kwh * mi/kWh.
    Approximates 75 kWh usable battery for the M3 Performance.
    """
    BATTERY_KWH = 75.0  # M3P long-range pack approximation

    rows = conn.execute(
        "SELECT ts, payload FROM events"
        " WHERE type IN ('heartbeat', 'driver_sample', 'manual_refresh')"
        " ORDER BY ts ASC"
    ).fetchall()

    parsed = []
    for r in rows:
        try:
            p = json.loads(r["payload"])
            parsed.append((r["ts"], p))
        except Exception:
            continue

    segments: list[dict] = []
    cur: dict | None = None
    last = None  # (ts, payload)

    for ts, p in parsed:
        odo = p.get("odometer")
        bat = p.get("battery_level")
        charging = (p.get("charging_state") or "").lower() == "charging"

        if last is not None:
            last_ts, last_p = last
            last_odo = last_p.get("odometer")
            last_bat = last_p.get("battery_level")
            last_charging = (last_p.get("charging_state") or "").lower() == "charging"

            d_mi = None
            source = None
            if odo is not None and last_odo is not None:
                delta = odo - last_odo
                if delta >= 0.05:
                    d_mi = delta
                    source = "odometer"
            elif (bat is not None and last_bat is not None and not charging
                  and not last_charging):
                d_pct = last_bat - bat
                if d_pct >= 1:
                    d_mi = (d_pct / 100.0) * BATTERY_KWH * mi_per_kwh
                    source = "battery"

            # Gap between samples implies the drive ended and a (possibly
            # different) one started. The trip detector polls every 90s while
            # driving; anything > 20 min must include a parked period.
            gap = ts - last_ts
            prev_parked = (last_p.get("shift_state") in (None, "P"))
            should_split = cur is not None and (
                gap > 20 * 60
                or (prev_parked and gap > 5 * 60)
            )
            if should_split:
                segments.append(cur)
                cur = None

            if d_mi:
                if cur is None:
                    cur = {
                        "start_ts": ts, "end_ts": ts,  # start = first moving sample
                        "start_odo": last_odo, "end_odo": odo,
                        "start_battery": last_bat, "end_battery": bat,
                        "miles_est": d_mi, "source": source,
                    }
                else:
                    cur["end_ts"] = ts
                    cur["end_odo"] = odo
                    cur["end_battery"] = bat
                    cur["miles_est"] += d_mi
                    if cur["source"] != source:
                        cur["source"] = "mixed"
            else:
                if cur:
                    segments.append(cur)
                    cur = None
        last = (ts, p)
    if cur:
        segments.append(cur)

    out: list[dict] = []
    for s in segments:
        if s["miles_est"] < 0.3:  # filter out micro-blips
            continue
        dur_min = (s["end_ts"] - s["start_ts"]) / 60
        bat_used = (s["start_battery"] - s["end_battery"]) if (
            s.get("start_battery") and s.get("end_battery")
        ) else None
        avg_speed = (s["miles_est"] / (dur_min / 60)) if dur_min > 0 else 0
        cost = _trip_cost(s["miles_est"], bat_used)
        out.append({
            "kind": "segment",
            "driver": "—",
            "start_ts": s["start_ts"],
            "end_ts": s["end_ts"],
            "duration_minutes": round(dur_min, 1),
            "miles_est": round(s["miles_est"], 1),
            "max_speed_mph": None,
            "avg_speed_mph": round(avg_speed, 1),
            "hard_brakes": 0,
            "rapid_accels": 0,
            "battery_used_pct": bat_used,
            "source": s["source"],
            "cost_usd": cost["total_usd"],
            "cost_breakdown": cost,
        })
    return out


@app.get("/trip", response_class=HTMLResponse)
def trip_detail_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "trip_detail.html")


@app.get("/api/trip/{start_ts:int}")
def api_trip_detail(start_ts: int) -> dict:
    """Detailed view of a single trip: all polled events between start_ts and
    the next 'idle' boundary, plus speed timeline + incidents + GPS path."""
    with db.connect() as conn:
        # Pull a window of events. Trips end after 10+ minutes of idle, so we
        # cap the lookahead to 6 hours which is far more than any realistic trip.
        rows = conn.execute(
            "SELECT ts, type, payload FROM events"
            " WHERE type IN ('heartbeat', 'driver_sample')"
            " AND ts >= ? AND ts <= ? ORDER BY ts ASC",
            (start_ts, start_ts + 6 * 3600),
        ).fetchall()

    events: list[dict] = []
    for r in rows:
        try:
            p = json.loads(r["payload"])
            p["ts"] = r["ts"]
            p["type"] = r["type"]
            events.append(p)
        except Exception:
            continue
    if not events:
        return {"start_ts": start_ts, "samples": 0, "events": [], "incidents": [], "gps": []}

    # Trip end = last driver_sample (movement) we saw before any 10-min gap.
    # Heartbeats while parked don't count — they fire every 5 min and would
    # otherwise extend "trip duration" through a charging session.
    end_idx = -1
    last_drive_idx = -1
    for i, e in enumerate(events):
        if i > 0 and e["ts"] - events[i - 1]["ts"] > 600:
            break
        if e["type"] == "driver_sample":
            last_drive_idx = i
        end_idx = i
    if last_drive_idx >= 0:
        end_idx = last_drive_idx
    trip_events = events[: end_idx + 1]

    incidents = [
        {"ts": e["ts"], "type": e["event"]["type"],
         "delta_mph_per_s": e["event"].get("delta_mph_per_s"),
         "speed_mph": e.get("speed_mph"),
         "maps_url": e.get("maps_url")}
        for e in trip_events
        if isinstance(e.get("event"), dict)
    ]
    speeds = [
        {"ts": e["ts"], "mph": e["speed_mph"]}
        for e in trip_events if e.get("speed_mph") is not None
    ]
    gps = [
        {"ts": e["ts"], "lat": (e.get("gps") or {}).get("lat"),
         "lon": (e.get("gps") or {}).get("lon")}
        for e in trip_events if e.get("gps")
    ]

    odo_start = next((e.get("odometer") for e in trip_events if e.get("odometer") is not None), None)
    odo_end = next((e.get("odometer") for e in reversed(trip_events) if e.get("odometer") is not None), None)
    bat_start = next((e.get("battery_level") for e in trip_events if e.get("battery_level") is not None), None)
    bat_end = next((e.get("battery_level") for e in reversed(trip_events) if e.get("battery_level") is not None), None)

    miles = round(odo_end - odo_start, 1) if (odo_start and odo_end) else None
    duration_min = round((trip_events[-1]["ts"] - trip_events[0]["ts"]) / 60, 1)

    drivers = [e.get("driver") for e in trip_events if e.get("driver")]
    driver = max(set(drivers), key=drivers.count) if drivers else None

    bat_used = (bat_start - bat_end) if (bat_start and bat_end) else None
    cost = _trip_cost(miles or 0, bat_used) if miles else None
    return {
        "start_ts": trip_events[0]["ts"],
        "end_ts": trip_events[-1]["ts"],
        "duration_minutes": duration_min,
        "miles": miles,
        "battery_used_pct": bat_used,
        "battery_start": bat_start,
        "battery_end": bat_end,
        "samples": len(trip_events),
        "driver": driver,
        "max_speed_mph": max((s["mph"] for s in speeds), default=None),
        "avg_speed_mph": (sum(s["mph"] for s in speeds) / len(speeds)) if speeds else None,
        "speeds": speeds,
        "incidents": incidents,
        "gps": gps,
        "cost": cost,
    }


def _gps_bbox_from_db(conn) -> "osm.BBox":
    """Compute a bounding box around all stored GPS samples."""
    rows = conn.execute(
        "SELECT json_extract(payload, '$.gps.lat') AS lat,"
        " json_extract(payload, '$.gps.lon') AS lon"
        " FROM events"
        " WHERE json_extract(payload, '$.gps.lat') IS NOT NULL"
    ).fetchall()
    pts = [(r["lat"], r["lon"]) for r in rows]
    return osm.BBox.from_points(pts)


@app.get("/api/enforcement")
def api_enforcement() -> dict:
    """Speed cameras, school zones, traffic-calming features near our drive area."""
    with db.connect() as conn:
        bbox = _gps_bbox_from_db(conn)
    try:
        raw = osm.fetch_enforcement(bbox)
        features = osm.normalize_enforcement(raw)
    except Exception as e:
        logger.exception("enforcement fetch failed")
        return {"error": str(e), "features": []}
    return {
        "bbox": [bbox.south, bbox.west, bbox.north, bbox.east],
        "count": len(features),
        "by_kind": {
            "camera":  sum(1 for f in features if f["kind"] == "camera"),
            "school":  sum(1 for f in features if f["kind"] == "school"),
            "calming": sum(1 for f in features if f["kind"] == "calming"),
        },
        "features": features,
    }


@app.get("/api/violations")
def api_violations(driver: str | None = None, over_mph: int = 5) -> dict:
    """Cross-reference every GPS sample with the nearest OSM road's posted limit.

    A 'violation' = sample speed exceeded posted limit by `over_mph` or more.
    Aggregates by driver, road, and trip. Heavy on first call (Overpass query),
    cheap thereafter (cache).
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT ts, payload FROM events"
            " WHERE type = 'driver_sample'"
            " AND json_extract(payload, '$.gps.lat') IS NOT NULL"
            " AND json_extract(payload, '$.speed_mph') IS NOT NULL"
            " ORDER BY ts ASC LIMIT 50000"
        ).fetchall()
        bbox = _gps_bbox_from_db(conn)

    samples = []
    for r in rows:
        try:
            p = json.loads(r["payload"])
        except Exception:
            continue
        gps = p.get("gps") or {}
        if gps.get("lat") is None or gps.get("lon") is None:
            continue
        if (p.get("speed_mph") or 0) < 5:
            continue
        if driver and (p.get("driver") or "").lower() != driver.lower():
            continue
        samples.append({
            "ts": r["ts"], "lat": gps["lat"], "lon": gps["lon"],
            "speed_mph": p["speed_mph"], "driver": p.get("driver"),
        })

    if not samples:
        return {"samples": 0, "violations": [], "by_driver": [], "by_road": []}

    # Fetch road network once and snap each sample.
    try:
        roads = osm.fetch_road_speeds(bbox).get("elements", [])
    except Exception as e:
        logger.exception("road fetch failed")
        return {"error": str(e), "samples": len(samples), "violations": []}

    road_idx = _build_road_index(roads)
    violations: list[dict] = []
    for s in samples:
        snap = _snap_to_road(s["lat"], s["lon"], road_idx)
        if not snap or not snap.get("limit_mph"):
            continue
        over = s["speed_mph"] - snap["limit_mph"]
        if over >= over_mph:
            violations.append({
                **s,
                "posted_mph": snap["limit_mph"],
                "over_mph": round(over, 1),
                "road": snap.get("name"),
            })

    by_driver: dict[str, dict] = {}
    by_road: dict[str, dict] = {}
    for v in violations:
        d = v.get("driver") or "—"
        by_driver.setdefault(d, {"driver": d, "count": 0, "max_over": 0, "miles_logged": 0})
        by_driver[d]["count"] += 1
        by_driver[d]["max_over"] = max(by_driver[d]["max_over"], v["over_mph"])
        rd = v.get("road") or "Unknown"
        by_road.setdefault(rd, {"road": rd, "count": 0, "max_over": 0})
        by_road[rd]["count"] += 1
        by_road[rd]["max_over"] = max(by_road[rd]["max_over"], v["over_mph"])

    return {
        "samples": len(samples),
        "violations_total": len(violations),
        "violations": violations[-50:],  # last 50 for the UI list
        "by_driver": sorted(by_driver.values(), key=lambda x: -x["count"]),
        "by_road": sorted(by_road.values(), key=lambda x: -x["count"])[:20],
    }


def _build_road_index(ways: list[dict]) -> list[dict]:
    """Pre-process Overpass ways into a flat list of segments with maxspeed."""
    out = []
    for w in ways:
        tags = w.get("tags") or {}
        limit = osm._parse_maxspeed(tags.get("maxspeed"))
        if not limit:
            continue
        geom = w.get("geometry") or []
        if len(geom) < 2:
            continue
        out.append({
            "name": tags.get("name") or tags.get("ref"),
            "limit_mph": limit,
            "highway": tags.get("highway"),
            "geom": [(g["lat"], g["lon"]) for g in geom],
        })
    return out


def _snap_to_road(lat: float, lon: float, road_idx: list[dict],
                  max_dist_m: float = 30) -> dict | None:
    """Return the nearest road segment within max_dist_m, or None."""
    best = None
    best_d2 = None
    # ~111_000 m per degree latitude; per degree lon scaled by cos(lat)
    import math
    cos_lat = math.cos(math.radians(lat))
    for road in road_idx:
        for i in range(len(road["geom"]) - 1):
            (lat1, lon1) = road["geom"][i]
            (lat2, lon2) = road["geom"][i + 1]
            d2 = _point_segment_distance_sq(lat, lon, lat1, lon1, lat2, lon2, cos_lat)
            if best_d2 is None or d2 < best_d2:
                best_d2 = d2
                best = road
    if best is None:
        return None
    # Convert squared-degree distance to meters
    if best_d2 ** 0.5 * 111_000 > max_dist_m:
        return None
    return best


def _point_segment_distance_sq(plat, plon, lat1, lon1, lat2, lon2, cos_lat):
    """Squared distance from point (plat, plon) to segment, in degrees².
    Uses cos(lat) to keep east-west distances proportionally accurate."""
    # Convert to local "flat" coords scaled by cos_lat for longitude
    px, py = plon * cos_lat, plat
    x1, y1 = lon1 * cos_lat, lat1
    x2, y2 = lon2 * cos_lat, lat2
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return (px - x1) ** 2 + (py - y1) ** 2
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    cx, cy = x1 + t * dx, y1 + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2


@app.get("/api/gps/all")
def api_gps_all(limit: int = 5000, driver: str | None = None) -> dict:
    """Return all GPS points across the database for the Map page.
    Filters at SQL level to GPS-bearing rows; the historical import has 135K
    rows without GPS, so a naive ORDER BY ASC LIMIT misses live data entirely.
    Optional ``driver`` query filters to one driver's samples only.
    """
    with db.connect() as conn:
        sql = (
            "SELECT ts, type, payload FROM events"
            " WHERE type IN ('driver_sample', 'heartbeat')"
            " AND json_extract(payload, '$.gps.lat') IS NOT NULL"
        )
        params: list = []
        if driver:
            sql += " AND LOWER(driver) = LOWER(?)"
            params.append(driver)
        sql += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    points: list[dict] = []
    for r in rows:
        try:
            p = json.loads(r["payload"])
        except Exception:
            continue
        gps = p.get("gps")
        if not gps or gps.get("lat") is None or gps.get("lon") is None:
            continue
        points.append({
            "ts": r["ts"],
            "lat": gps["lat"],
            "lon": gps["lon"],
            "speed_mph": p.get("speed_mph"),
            "driver": p.get("driver"),
        })
    return {"count": len(points), "points": points}


@app.get("/api/trips")
def api_trips(limit: int = 50, driver: str | None = None) -> dict:
    """Detect 'trips' as continuous driver_sample runs separated by ≥10 min gaps,
    plus odometer-derived segments to backfill drives we couldn't see live."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT ts, driver, payload FROM events"
            " WHERE type = 'driver_sample' ORDER BY ts ASC"
        ).fetchall()
        totals = db.get_roi(conn)
        sessions_total = sum(s["energy_kwh"] for s in db.all_charging_sessions(conn))

    if not rows:
        return {"trips": [], "count": 0}

    # All-in operating cost per mile (electricity + tire wear).
    cost_per_mi = COST_PER_MI

    trips: list[dict] = []
    cur: dict | None = None
    GAP = 600  # 10 min gap = new trip
    for r in rows:
        ts = r["ts"]
        d = r["driver"]
        if d is None:
            continue
        try:
            payload = json.loads(r["payload"])
        except Exception:
            payload = {}
        speed = payload.get("speed_mph")
        ev = payload.get("event") or {}

        if cur is None or (ts - cur["last_ts"]) > GAP or cur["driver"] != d:
            if cur and cur["samples"] > 5:
                trips.append(_finalize_trip(cur, cost_per_mi))
            cur = {
                "driver": d, "first_ts": ts, "last_ts": ts,
                "samples": 0, "max_speed": 0, "speed_sum": 0, "speed_n": 0,
                "brakes": 0, "accels": 0,
            }
        cur["last_ts"] = ts
        cur["samples"] += 1
        if speed is not None:
            cur["max_speed"] = max(cur["max_speed"], speed)
            cur["speed_sum"] += speed
            cur["speed_n"] += 1
        if ev.get("type") == "hard_brake":  cur["brakes"] += 1
        if ev.get("type") == "rapid_accel": cur["accels"] += 1
    if cur and cur["samples"] > 5:
        trips.append(_finalize_trip(cur, cost_per_mi))

    # Tag per-sample trips so the UI knows they came from real telemetry.
    for t in trips:
        t["kind"] = "telemetry"

    # Add odometer-derived (or battery-fallback) segments for drives we
    # couldn't capture live. Filter out segments overlapping a telemetry trip.
    with db.connect() as conn:
        # Same mi/kWh used in the ROI page, derived from sample-window totals.
        roi_totals = db.get_roi(conn)
        mi_per_kwh = (
            roi_totals["total_miles"] / roi_totals["total_kwh"]
            if roi_totals.get("total_kwh") else 3.0
        )
        segments = _derive_odometer_segments(conn, cost_per_mi, mi_per_kwh)
    if segments and trips:
        cover = [(t["start_ts"], t["end_ts"]) for t in trips]
        segments = [
            s for s in segments
            if not any(a <= s["start_ts"] <= b or a <= s["end_ts"] <= b for a, b in cover)
        ]
    trips.extend(segments)

    if driver:
        trips = [t for t in trips if (t.get("driver") or "").lower() == driver.lower()]
    trips.sort(key=lambda t: -t["start_ts"])  # newest first
    return {"trips": trips[:limit], "count": len(trips), "cost_per_mile": round(cost_per_mi, 4)}


def _finalize_trip(cur: dict, cost_per_mi: float) -> dict:
    duration_min = (cur["last_ts"] - cur["first_ts"]) / 60
    avg_speed = (cur["speed_sum"] / cur["speed_n"]) if cur["speed_n"] else 0
    miles_est = avg_speed * (duration_min / 60)
    cost = _trip_cost(miles_est)
    return {
        "driver": cur["driver"],
        "start_ts": cur["first_ts"],
        "end_ts": cur["last_ts"],
        "duration_minutes": round(duration_min, 1),
        "samples": cur["samples"],
        "miles_est": round(miles_est, 1),
        "max_speed_mph": round(cur["max_speed"], 1),
        "avg_speed_mph": round(avg_speed, 1),
        "hard_brakes": cur["brakes"],
        "rapid_accels": cur["accels"],
        "cost_usd": cost["total_usd"],
        "cost_breakdown": cost,
    }


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


# ============================================================
#  Background poller — fetches vehicle_data, derives events, persists to SQLite
# ============================================================

# Maps phone-key IDs to driver names (matches the importer mapping).
KEY_TO_DRIVER = {
    "1038929084": "Colin",
    "4136030175": "Carson",
    "2851572947": "Lindsey",
}
HARD_BRAKE_MPHS = -7.0
RAPID_ACCEL_MPHS = 7.0


def _maybe_heartbeat(
    vin: str, driver: str | None, summary: dict,
    last_heartbeat: float, every: float,
) -> float:
    """Write a cheap 'asleep' heartbeat from the /vehicle summary endpoint
    so we have *some* row indicating the poller is alive, but only every
    HEARTBEAT_EVERY seconds. Returns the new last_heartbeat ts."""
    now = time.time()
    if now - last_heartbeat < every:
        return last_heartbeat
    record = {
        "type": "heartbeat",
        "ts": now,
        "vin": vin,
        "driver": driver,
        "speed_mph": None,
        "shift_state": None,
        "battery_level": (summary.get("charge_state") or {}).get("battery_level"),
        "state": summary.get("state"),
        "gps": None,
    }
    try:
        with db.connect() as conn:
            db.record_event(conn, record)
    except Exception:
        logger.exception("[poll] heartbeat write failed")
    return now


async def _poll_loop(vin: str, interval: float) -> None:
    """Adaptive forever-loop. Burns API quota only when something is happening.

    Cadence:
      - Driving (shift D/R or speed > 0):   30s   (the configured interval)
      - Parked + online:                    5 min
      - Asleep:                             30 min, cheap state-only check
                                            (NEVER wake the car just to peek)
      - Overnight (23:00–06:00 local):      cap at 1 hr unless we know it's
                                            actively driving
    """
    last_speed = None
    last_ts = None
    current_driver: str | None = None
    consecutive_errors = 0
    last_heartbeat = 0.0
    HEARTBEAT_EVERY = 600  # parked status row every 10 min

    # Tuned to hit ~$5/month Tesla Fleet API budget (was ~$15/mo before).
    # Each call costs ~$0.002, so monthly budget = ~2,500 calls = 83/day.
    DRIVING_INTERVAL = max(interval, 90)   # was 30s — accept lower granularity
    PARKED_INTERVAL = 45 * 60              # was 5 min
    ASLEEP_INTERVAL = 2 * 3600             # was 30 min
    OVERNIGHT_INTERVAL = 12 * 3600         # was 1 hr — skip the whole window
    EXTENDED_IDLE_AFTER = 4 * 3600         # 4 hr of no movement → extended-idle mode
    EXTENDED_IDLE_INTERVAL = 24 * 3600     # once-daily check-in during long parks
    # Tracks last time the car was observed moving (speed > 0 or odometer changed).
    # When (now - last_movement) > EXTENDED_IDLE_AFTER, we ALSO back off to
    # EXTENDED_IDLE_INTERVAL even if Tesla reports state=online, so that our
    # polls stop preventing the car from entering its deep-sleep state.
    last_movement_ts = time.time()
    last_observed_odo: float | None = None

    def overnight() -> bool:
        h = datetime.now().hour
        return h >= 22 or h < 6

    while True:
        try:
            token = app.state.store.load()
            if token is None:
                logger.info("[poll] no OAuth token yet; sleeping")
                await asyncio.sleep(PARKED_INTERVAL)
                continue

            # Overnight kill-switch — skip polling entirely 22:00–06:00.
            # Saves ~8 hours × 60min/PARKED_INTERVAL = 11 calls/night = ~$0.66/mo.
            if overnight():
                await asyncio.sleep(OVERNIGHT_INTERVAL)
                continue

            client = TeslaFleetClient(app.state.settings, token, app.state.store)
            async with client:
                # Skip the cheap-probe step — /api/1/vehicles/{vin} bills
                # the same as /vehicle_data, so it was just a wasted call.
                # Call vehicle_data directly; rely on the 404/408 error path
                # to detect asleep state (and back off longer in that case).
                try:
                    data = await client.vehicle_data(vin)
                except Exception as e:
                    msg = str(e)
                    if "404" in msg or "408" in msg or "asleep" in msg.lower():
                        # Car went to sleep between probe and read — back off,
                        # don't wake it.
                        sleep_for = OVERNIGHT_INTERVAL if overnight() else ASLEEP_INTERVAL
                        await asyncio.sleep(sleep_for)
                        continue
                    raise

            now = time.time()
            drive = data.get("drive_state") or {}
            veh = data.get("vehicle_state") or {}
            charge = data.get("charge_state") or {}

            speed = drive.get("speed")  # mph or None when parked
            shift = drive.get("shift_state")
            lat = drive.get("latitude")
            lon = drive.get("longitude")
            speed_limit = (drive.get("speed_limit_mode") or {}).get("current_limit_mph")
            odometer_mi = veh.get("odometer")
            energy_added = charge.get("charge_energy_added")

            # Carry-forward driver from active key.
            key_id = veh.get("active_route_destination") or None  # placeholder fallback
            ap_key = veh.get("driver_temp_setting") or None        # placeholder fallback
            # Real Tesla field for active key device id varies; the export uses
            # "Identity of the Active Key Device" which isn't present in the
            # vehicle_data REST response. As a pragmatic substitute, we keep
            # whatever the importer set last and note it in the payload's
            # active_driver_profile field, falling back to the configured default.
            # Active-driver hierarchy:
            #   1. Tesla-reported active_driver_profile (rare in vehicle_data)
            #   2. UI-set override (POST /api/active-driver, persisted to /data)
            #   3. Configured default_driver
            profile = (veh.get("active_driver_profile")
                       or data.get("active_driver_profile")
                       or getattr(app.state, "active_driver", None)
                       or app.state.settings.default_driver)
            if profile:
                current_driver = profile

            # Detect brake/accel from speed delta.
            event = None
            if speed is not None and last_speed is not None and last_ts is not None:
                dt = now - last_ts
                if dt > 0:
                    d_mphs = (speed - last_speed) / dt
                    if d_mphs <= HARD_BRAKE_MPHS:
                        event = {"type": "hard_brake", "delta_mph_per_s": round(d_mphs, 2)}
                    elif d_mphs >= RAPID_ACCEL_MPHS:
                        event = {"type": "rapid_accel", "delta_mph_per_s": round(d_mphs, 2)}
            last_speed = speed
            last_ts = now

            # Persist a sample when something interesting happened, OR a
            # heartbeat every HEARTBEAT_EVERY seconds while parked so we can
            # see the poller is alive on the dashboard.
            is_drive_sample = (event or (speed and speed > 0) or shift in {"D", "R"})
            is_heartbeat = (now - last_heartbeat) > HEARTBEAT_EVERY
            if is_drive_sample or is_heartbeat:
                record = {
                    "type": "driver_sample" if is_drive_sample else "heartbeat",
                    "ts": now,
                    "vin": vin,
                    "driver": current_driver,
                    "speed_mph": speed,
                    "speed_limit_mph": speed_limit,
                    "shift_state": shift,
                    "battery_level": charge.get("battery_level"),
                    "battery_range_mi": charge.get("battery_range"),
                    "charging_state": charge.get("charging_state"),
                    "odometer": odometer_mi,
                    "gps": {"lat": lat, "lon": lon} if lat is not None else None,
                    "maps_url": (f"https://www.google.com/maps?q={lat},{lon}"
                                  if lat is not None else None),
                    "event": event,
                }
                with db.connect() as conn:
                    db.record_event(conn, record)
                if is_heartbeat:
                    last_heartbeat = now

            # Update odometer + charging totals.
            if odometer_mi is not None or energy_added is not None:
                with db.connect() as conn:
                    db.update_roi(
                        conn, vin,
                        odometer_mi=odometer_mi,
                        charge_energy_added_kwh=energy_added,
                    )

            consecutive_errors = 0

            # Track when the car was last observed moving. Each successful
            # poll that shows movement resets the timer; if the gap exceeds
            # EXTENDED_IDLE_AFTER, we throttle hard so we don't keep the car
            # from entering its native deep-sleep state (vampire drain).
            if is_drive_sample:
                last_movement_ts = now
            elif (
                odometer_mi is not None
                and last_observed_odo is not None
                and odometer_mi > last_observed_odo + 0.01
            ):
                last_movement_ts = now
            if odometer_mi is not None:
                last_observed_odo = odometer_mi

            extended_idle = (now - last_movement_ts) > EXTENDED_IDLE_AFTER

            # Pick next-tick cadence based on what we just saw.
            if is_drive_sample:
                next_sleep = DRIVING_INTERVAL
            elif extended_idle:
                logger.info(
                    "[poll] extended idle (%.1f hr since last movement); "
                    "backing off %d min to let car sleep",
                    (now - last_movement_ts) / 3600,
                    EXTENDED_IDLE_INTERVAL // 60,
                )
                next_sleep = EXTENDED_IDLE_INTERVAL
            elif overnight():
                next_sleep = OVERNIGHT_INTERVAL
            else:
                next_sleep = PARKED_INTERVAL
            await asyncio.sleep(next_sleep)

        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_errors += 1
            backoff = min(interval * (2 ** consecutive_errors), 600)
            logger.exception("[poll] unhandled error; backing off %.0fs", backoff)
            await asyncio.sleep(backoff)
