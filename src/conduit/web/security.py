"""Access control for the dashboard.

Four modes, chosen with ``CONDUIT_AUTH_MODE``:

``lan``    private address ranges only (the sensible default on a home network)
``token``  a shared secret in ``X-Conduit-Token``, ``Authorization: Bearer`` or ``?token=``
``both``   must satisfy both
``none``   wide open -- only for a trusted reverse proxy in front

The reference project hard-coded a prefix check on ``192.168.``/``10.``/``172.``
which quietly let through ``172.99.x`` (public space) and rejected IPv6
loopback entirely. This does the range arithmetic properly.
"""

from __future__ import annotations

import ipaddress
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..config import Settings
from ..logs import get_logger

log = get_logger("security")

OPEN_PATHS = ("/api/health", "/favicon.ico")


def _token_matches(presented: str, expected: str) -> bool:
    # Bytes, not str: compare_digest raises TypeError on non-ASCII text, which
    # turned a garbage header into a 500 instead of a 403.
    return bool(expected) and secrets.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    )


def is_private_address(host: str | None) -> bool:
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost", "testclient")
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
    )


def _presented_token(request: Request) -> str:
    header = request.headers.get("x-conduit-token")
    if header:
        return header.strip()
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.query_params.get("token", "").strip()


class AccessMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.mode = settings.conduit_auth_mode
        self.token = settings.conduit_api_token

    async def dispatch(self, request: Request, call_next):
        if self.mode == "none" or request.url.path in OPEN_PATHS:
            return await call_next(request)

        client_host = request.client.host if request.client else None
        lan_ok = is_private_address(client_host)
        token_ok = _token_matches(_presented_token(request), self.token)

        allowed = {
            "lan": lan_ok,
            "token": token_ok,
            "both": lan_ok and token_ok,
        }.get(self.mode, lan_ok)

        if not allowed:
            log.warning(
                "request rejected",
                extra={"client": client_host, "path": request.url.path, "mode": self.mode},
            )
            return JSONResponse(
                {"detail": "Access denied. rás only accepts local-network requests."},
                status_code=403,
            )
        return await call_next(request)


def websocket_allowed(settings: Settings, host: str | None, token: str) -> bool:
    """WebSockets bypass HTTP middleware in Starlette, so they check here."""
    if settings.conduit_auth_mode == "none":
        return True
    lan_ok = is_private_address(host)
    token_ok = _token_matches(token, settings.conduit_api_token)
    return {
        "lan": lan_ok,
        "token": token_ok,
        "both": lan_ok and token_ok,
    }.get(settings.conduit_auth_mode, lan_ok)
