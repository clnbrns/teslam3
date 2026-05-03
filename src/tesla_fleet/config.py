"""Configuration loaded from environment / .env."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REGIONS = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
}

AUTH_BASE = "https://auth.tesla.com"
TOKEN_URL = f"{AUTH_BASE}/oauth2/v3/token"
AUTHORIZE_URL = f"{AUTH_BASE}/oauth2/v3/authorize"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TESLA_", extra="ignore")

    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = ""
    audience: str = REGIONS["na"]
    region: str = "na"
    scopes: str = "openid offline_access user_data vehicle_device_data vehicle_cmds vehicle_charging_cmds"
    token_store_path: str = Field(default=".tokens.json", alias="TOKEN_STORE_PATH")
    vehicle_command_proxy_url: str = Field(default="", alias="VEHICLE_COMMAND_PROXY_URL")
    public_hostname: str = Field(default="", alias="TESLA_PUBLIC_HOSTNAME")
    default_driver: str = Field(default="Colin", alias="TESLA_DEFAULT_DRIVER")

    @property
    def api_base(self) -> str:
        return REGIONS.get(self.region, self.audience)
