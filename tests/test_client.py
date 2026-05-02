import time

import httpx
import pytest
import respx

from tesla_fleet.client import TeslaFleetClient
from tesla_fleet.config import Settings
from tesla_fleet.tokens import Token


@pytest.fixture
def settings() -> Settings:
    return Settings(region="na", client_id="cid", client_secret="sec")


@pytest.fixture
def token() -> Token:
    return Token(access_token="at", refresh_token="rt", expires_at=time.time() + 3600)


@respx.mock
async def test_list_vehicles(settings: Settings, token: Token) -> None:
    respx.get(f"{settings.api_base}/api/1/vehicles").mock(
        return_value=httpx.Response(200, json={"response": [{"vin": "5YJ", "display_name": "T"}]})
    )
    async with TeslaFleetClient(settings, token) as c:
        vs = await c.list_vehicles()
    assert vs[0]["vin"] == "5YJ"


@respx.mock
async def test_set_charge_limit(settings: Settings, token: Token) -> None:
    route = respx.post(
        f"{settings.api_base}/api/1/vehicles/5YJ/command/set_charge_limit"
    ).mock(return_value=httpx.Response(200, json={"response": {"result": True}}))
    async with TeslaFleetClient(settings, token) as c:
        out = await c.set_charge_limit("5YJ", 80)
    assert route.called
    assert out["response"]["result"] is True
