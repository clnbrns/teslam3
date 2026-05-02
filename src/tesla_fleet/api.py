"""FastAPI service exposing OAuth callback + vehicle endpoints."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query

from tesla_fleet.auth import build_authorize_url
from tesla_fleet.client import TeslaFleetClient
from tesla_fleet.config import Settings
from tesla_fleet.tokens import TokenStore, exchange_code

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = Settings()
    app.state.store = TokenStore(app.state.settings.token_store_path)
    yield


app = FastAPI(title="tesla-fleet", lifespan=lifespan)


def get_client() -> TeslaFleetClient:
    token = app.state.store.load()
    if token is None:
        raise HTTPException(401, "Not authenticated; visit /login")
    return TeslaFleetClient(app.state.settings, token, app.state.store)


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
