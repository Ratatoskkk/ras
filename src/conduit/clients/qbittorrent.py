"""Async qBittorrent WebUI v2 client.

Torrents are added *by file* rather than by URL. That is the whole point: we
already fetched the .torrent to compute its info-hash, so qBittorrent receives
something whose identity we know in advance and every later status lookup is an
exact hash match. The reference project added by URL and then guessed which
torrent was which by comparing words in the name.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx

from ..domain.models import TorrentStatus
from ..logs import get_logger
from ..util.resilience import PermanentError, TransientError
from .http import HttpService

log = get_logger("qbittorrent")

# States that mean a torrent is moving data or waiting its turn to.
BUSY_STATES = frozenset(
    {"downloading", "stalledDL", "metaDL", "queuedDL", "forcedDL", "allocating", "checkingDL"}
)


class QBittorrentClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.http = HttpService(
            "qbittorrent",
            self.base_url,
            headers={"Referer": self.base_url, "Origin": self.base_url},
            timeout=timeout,
        )
        self._login_lock = asyncio.Lock()
        self._authenticated = False
        self._version = ""

    async def aclose(self) -> None:
        await self.http.aclose()

    def health(self) -> dict[str, Any]:
        return {**self.http.health(), "version": self._version,
                "authenticated": self._authenticated}

    # -- auth ---------------------------------------------------------------
    async def login(self, force: bool = False) -> None:
        async with self._login_lock:
            if self._authenticated and not force:
                return
            response = await self.http.request(
                "POST",
                "/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
            )
            body = (response.text if response is not None else "").strip()
            if body != "Ok.":
                raise PermanentError(
                    "qBittorrent rejected the login. Check QBITTORRENT_USERNAME/PASSWORD, "
                    "and that the WebUI allows this host."
                )
            self._authenticated = True
            self._version = await self._fetch_version()
            log.info("connected to qBittorrent", extra={"version": self._version})

    async def _fetch_version(self) -> str:
        try:
            response = await self.http.request("GET", "/api/v2/app/version")
            return (response.text if response is not None else "").strip()
        except Exception:
            return "unknown"

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response | None:
        """Issue a request, transparently re-authenticating on session expiry."""
        if not self._authenticated:
            await self.login()
        try:
            return await self.http.request(method, path, **kwargs)
        except PermanentError as exc:
            if "403" not in str(exc) and "authentication" not in str(exc).lower():
                raise
            self._authenticated = False
            await self.login(force=True)
            return await self.http.request(method, path, **kwargs)

    # -- reads --------------------------------------------------------------
    async def torrents(
        self,
        *,
        status_filter: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        hashes: list[str] | None = None,
    ) -> list[TorrentStatus]:
        params: dict[str, Any] = {}
        if status_filter:
            params["filter"] = status_filter
        if category:
            params["category"] = category
        if tag:
            params["tag"] = tag
        if hashes:
            params["hashes"] = "|".join(h.lower() for h in hashes)
        response = await self._call("GET", "/api/v2/torrents/info", params=params)
        if response is None:
            return []
        try:
            payload = response.json()
        except ValueError as exc:
            raise TransientError("qBittorrent returned a non-JSON torrent list") from exc
        return [_to_status(item) for item in payload if isinstance(item, dict)]

    async def torrents_by_hash(self, hashes: list[str]) -> dict[str, TorrentStatus]:
        if not hashes:
            return {}
        return {t.info_hash: t for t in await self.torrents(hashes=hashes)}

    # -- writes -------------------------------------------------------------
    async def ensure_category(self, name: str, save_path: str = "") -> None:
        if not name:
            return
        # Fails harmlessly when the category already exists, or on builds that
        # predate categories entirely.
        with contextlib.suppress(Exception):
            await self._call(
                "POST",
                "/api/v2/torrents/createCategory",
                data={"category": name, "savePath": save_path},
            )

    async def add_torrent_file(
        self,
        content: bytes,
        *,
        filename: str,
        save_path: str,
        category: str = "",
        tags: str = "",
        paused: bool = False,
        rename: str = "",
    ) -> bool:
        data: dict[str, str] = {
            "savepath": save_path,
            "autoTMM": "false",
            "paused": "true" if paused else "false",
            "stopped": "true" if paused else "false",
        }
        if category:
            data["category"] = category
        if tags:
            data["tags"] = tags
        if rename:
            data["rename"] = rename

        response = await self._call(
            "POST",
            "/api/v2/torrents/add",
            files={"torrents": (filename, content, "application/x-bittorrent")},
            data=data,
        )
        body = (response.text if response is not None else "").strip()
        if body and body.lower() not in ("ok.", ""):
            raise TransientError(f"qBittorrent refused the torrent: {body[:200]}")
        return True

    async def add_torrent_url(
        self, url: str, *, save_path: str, category: str = "", tags: str = "",
        paused: bool = False,
    ) -> bool:
        """Fallback for when the .torrent could not be fetched locally."""
        data = {
            "urls": url,
            "savepath": save_path,
            "autoTMM": "false",
            "paused": "true" if paused else "false",
            "stopped": "true" if paused else "false",
        }
        if category:
            data["category"] = category
        if tags:
            data["tags"] = tags
        response = await self._call("POST", "/api/v2/torrents/add", data=data)
        body = (response.text if response is not None else "").strip()
        if body and body.lower() not in ("ok.", ""):
            raise TransientError(f"qBittorrent refused the torrent URL: {body[:200]}")
        return True

    async def delete(self, hashes: list[str], delete_files: bool = False) -> None:
        if not hashes:
            return
        await self._call(
            "POST",
            "/api/v2/torrents/delete",
            data={
                "hashes": "|".join(h.lower() for h in hashes),
                "deleteFiles": "true" if delete_files else "false",
            },
        )


def _to_status(item: dict[str, Any]) -> TorrentStatus:
    # 8640000 == qBittorrent's sentinel for "unknown/infinite".
    eta = int(item.get("eta") or 0)
    return TorrentStatus(
        info_hash=str(item.get("infohash_v1") or item.get("hash") or "").lower(),
        name=item.get("name", ""),
        state=item.get("state", ""),
        progress=float(item.get("progress") or 0.0),
        eta_seconds=eta if 0 <= eta < 8640000 else -1,
        dlspeed=float(item.get("dlspeed") or 0.0),
        size_bytes=float(item.get("size") or item.get("total_size") or 0.0),
        save_path=item.get("save_path", ""),
        content_path=item.get("content_path", ""),
        tags=[t.strip() for t in (item.get("tags") or "").split(",") if t.strip()],
        category=item.get("category", ""),
        completion_on=int(item.get("completion_on") or 0),
        seeding_time=int(item.get("seeding_time") or 0),
        ratio=float(item.get("ratio") or 0.0),
        uploaded=float(item.get("uploaded") or 0.0),
    )
