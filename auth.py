"""OAuth provider registration via Authlib.

A provider is only registered if its client id and secret env vars are set,
so the app gracefully runs without any OAuth configured (guest mode only).
"""
from __future__ import annotations

import os

from authlib.integrations.flask_client import OAuth


GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"


def init_oauth(app) -> OAuth:
    oauth = OAuth(app)
    google_id = os.environ.get("GOOGLE_CLIENT_ID")
    google_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    if google_id and google_secret:
        oauth.register(
            name="google",
            client_id=google_id,
            client_secret=google_secret,
            server_metadata_url=GOOGLE_DISCOVERY,
            client_kwargs={"scope": "openid email profile"},
        )
    return oauth


def enabled_providers() -> list[str]:
    providers = []
    if os.environ.get("GOOGLE_CLIENT_ID"):
        providers.append("google")
    return providers
