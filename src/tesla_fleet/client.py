"""Async Tesla Fleet API client."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from tesla_fleet.config import Settings
from tesla_fleet.tokens import Token, TokenStore, refresh

logger = logging.getLogger(__name__)


class TeslaFleetClient:
    def __init__(self, settings: Settings, token: Token, store: TokenStore | None = None):
        self.settings = settings
        self.token = token
        self.store = store
        self._http = httpx.AsyncClient(timeout=30, base_url=settings.api_base)

    async def __aenter__(self) -> "TeslaFleetClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._http.aclose()

    async def _ensure_fresh(self) -> None:
        if self.token.expired:
            logger.info("Refreshing token")
            self.token = await refresh(self.token, self.settings)
            if self.store:
                self.store.save(self.token)

    async def _request(self, method: str, path: str, **kw: Any) -> dict:
        await self._ensure_fresh()
        headers = kw.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token.access_token}"
        resp = await self._http.request(method, path, headers=headers, **kw)
        resp.raise_for_status()
        return resp.json()

    # --- Vehicles ---
    async def list_vehicles(self) -> list[dict]:
        data = await self._request("GET", "/api/1/vehicles")
        return data.get("response", [])

    async def vehicle(self, vin: str) -> dict:
        data = await self._request("GET", f"/api/1/vehicles/{vin}")
        return data.get("response", {})

    async def vehicle_data(self, vin: str) -> dict:
        data = await self._request("GET", f"/api/1/vehicles/{vin}/vehicle_data")
        return data.get("response", {})

    async def wake_up(self, vin: str) -> dict:
        return await self._request("POST", f"/api/1/vehicles/{vin}/wake_up")

    # --- Commands ---
    async def command(self, vin: str, name: str, payload: dict | None = None) -> dict:
        url = f"/api/1/vehicles/{vin}/command/{name}"
        if self.settings.vehicle_command_proxy_url:
            # Route through signed-command proxy when configured.
            async with httpx.AsyncClient(
                base_url=self.settings.vehicle_command_proxy_url, timeout=30
            ) as proxy:
                await self._ensure_fresh()
                resp = await proxy.post(
                    url,
                    json=payload or {},
                    headers={"Authorization": f"Bearer {self.token.access_token}"},
                )
                resp.raise_for_status()
                return resp.json()
        return await self._request("POST", url, json=payload or {})

    async def lock(self, vin: str) -> dict:
        return await self.command(vin, "door_lock")

    async def unlock(self, vin: str) -> dict:
        return await self.command(vin, "door_unlock")

    async def climate_start(self, vin: str) -> dict:
        return await self.command(vin, "auto_conditioning_start")

    async def climate_stop(self, vin: str) -> dict:
        return await self.command(vin, "auto_conditioning_stop")

    async def charge_start(self, vin: str) -> dict:
        return await self.command(vin, "charge_start")

    async def charge_stop(self, vin: str) -> dict:
        return await self.command(vin, "charge_stop")

    async def set_charge_limit(self, vin: str, percent: int) -> dict:
        return await self.command(vin, "set_charge_limit", {"percent": percent})
