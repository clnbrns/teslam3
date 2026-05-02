import json
import time
from pathlib import Path

from tesla_fleet.tokens import Token, TokenStore


def test_token_round_trip(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "t.json")
    tok = Token(access_token="a", refresh_token="r", expires_at=time.time() + 3600)
    store.save(tok)
    loaded = store.load()
    assert loaded is not None
    assert loaded.access_token == "a"
    assert not loaded.expired


def test_token_from_response() -> None:
    tok = Token.from_response({"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    assert tok.access_token == "a"
    assert tok.expires_at > time.time()


def test_token_store_clear(tmp_path: Path) -> None:
    p = tmp_path / "t.json"
    p.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": 0}))
    store = TokenStore(p)
    store.clear()
    assert not p.exists()
