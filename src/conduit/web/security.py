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
from urllib.parse import urlsplit

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
    # "::ffff:8.8.8.8" counts as private before Python 3.13; judge the IPv4.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
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


_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localhost")


def _hostname(value: str) -> str:
    return (urlsplit(f"//{value}").hostname or "").lower()


def host_allowed(headers) -> bool:
    """Defeat DNS rebinding and cross-site requests.

    A hostile page can make a LAN browser talk to this server from a
    private address, so the client IP alone proves nothing. The ``Host`` it
    used must be an address or a local name, and a browser ``Origin`` must
    be this very host.
    """
    host_header = headers.get("host", "")
    host = _hostname(host_header)
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if "." in host and not host.endswith(_LOCAL_SUFFIXES):
            return False
    origin = headers.get("origin")
    if origin and origin != "null":
        return urlsplit(origin).netloc.lower() == host_header.lower()
    return True


class AccessMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self.mode = settings.conduit_auth_mode
        self.token = settings.conduit_api_token

    async def dispatch(self, request: Request, call_next):
        if self.mode == "none":
            request.state.authorized = True
            return await call_next(request)

        client_host = request.client.host if request.client else None
        if request.url.path in OPEN_PATHS:
            # Reachable by anyone; the handler shows details only to callers
            # who would have passed the normal check.
            request.state.authorized = self._allowed(request, client_host)
            return await call_next(request)
        if self.mode == "lan" and not host_allowed(request.headers):
            log.warning("request rejected: foreign host or origin",
                        extra={"host": request.headers.get("host"),
                               "origin": request.headers.get("origin")})
            return JSONResponse({"detail": "Access denied."}, status_code=403)
        if not self._allowed(request, client_host):
            log.warning(
                "request rejected",
                extra={"client": client_host, "path": request.url.path, "mode": self.mode},
            )
            return JSONResponse(
                {"detail": "Access denied. rás only accepts local-network requests."},
                status_code=403,
            )
        request.state.authorized = True
        return await call_next(request)

    def _allowed(self, request: Request, client_host: str | None) -> bool:
        if self.mode == "lan" and not host_allowed(request.headers):
            return False
        lan_ok = is_private_address(client_host)
        token_ok = _token_matches(_presented_token(request), self.token)
        return {
            "lan": lan_ok,
            "token": token_ok,
            "both": lan_ok and token_ok,
        }.get(self.mode, lan_ok)


def websocket_allowed(
    settings: Settings, host: str | None, token: str, headers=None
) -> bool:
    """WebSockets bypass HTTP middleware in Starlette, so they check here."""
    if settings.conduit_auth_mode == "none":
        return True
    if (settings.conduit_auth_mode == "lan" and headers is not None
            and not host_allowed(headers)):
        return False
    lan_ok = is_private_address(host)
    token_ok = _token_matches(token, settings.conduit_api_token)
    return {
        "lan": lan_ok,
        "token": token_ok,
        "both": lan_ok and token_ok,
    }.get(settings.conduit_auth_mode, lan_ok)
