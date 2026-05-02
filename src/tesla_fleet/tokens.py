"""Token storage + refresh."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from tesla_fleet.config import TOKEN_URL, Settings


@dataclass
class Token:
    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "Bearer"

    @classmethod
    def from_response(cls, data: dict) -> "Token":
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=time.time() + int(data.get("expires_in", 28800)) - 60,
            token_type=data.get("token_type", "Bearer"),
        )

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


class TokenStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> Token | None:
        if not self.path.exists():
            return None
        return Token(**json.loads(self.path.read_text()))

    def save(self, token: Token) -> None:
        self.path.write_text(json.dumps(asdict(token), indent=2))
        self.path.chmod(0o600)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


async def refresh(token: Token, settings: Settings) -> Token:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": settings.client_id,
                "refresh_token": token.refresh_token,
                "scope": settings.scopes,
            },
        )
        resp.raise_for_status()
        return Token.from_response(resp.json())


async def exchange_code(code: str, settings: Settings) -> Token:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "code": code,
                "audience": settings.audience,
                "redirect_uri": settings.redirect_uri,
            },
        )
        resp.raise_for_status()
        return Token.from_response(resp.json())
