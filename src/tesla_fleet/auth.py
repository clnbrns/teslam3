"""Authorization-code helper for partner OAuth."""
from __future__ import annotations

import secrets
import urllib.parse

from tesla_fleet.config import AUTHORIZE_URL, Settings


def build_authorize_url(settings: Settings, state: str | None = None) -> tuple[str, str]:
    state = state or secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": settings.redirect_uri,
        "scope": settings.scopes,
        "state": state,
        "audience": settings.audience,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}", state
