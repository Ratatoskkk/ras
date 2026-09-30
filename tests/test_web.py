"""HTTP surface: routing, security, and the config round trip."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from conduit.config import ConfigStore, Settings
from conduit.domain.models import DownloadState
from conduit.services.bus import QUEUE_LIMIT, EventBus
from conduit.web.app import create_app
from conduit.web.security import is_private_address
from conduit.web.ws import snapshot_carrier, websocket_endpoint


@pytest.fixture
async def client(tmp_path, settings: Settings, config_store: ConfigStore):
    app = create_app(settings=settings, config_store=config_store, run_tasks=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
            http.app = app  # type: ignore[attr-defined]
            yield http


class TestEndpoints:
    @pytest.mark.parametrize(
        "path",
        ["/api/health", "/api/state", "/api/tasks", "/api/config", "/api/drives",
         "/api/events", "/api/logs", "/api/upcoming", "/api/cleanup", "/api/history",
         "/api/media", "/api/blocklist"],
    )
    async def test_read_endpoints_answer(self, client, path) -> None:
        response = await client.get(path)
        assert response.status_code == 200

    async def test_state_has_the_shape_the_dashboard_expects(self, client) -> None:
        payload = (await client.get("/api/state")).json()
        for key in ("summary", "downloads", "pending_groups", "upcoming", "drives",
                    "timestamps", "tasks", "version"):
            assert key in payload

    async def test_unknown_api_route_is_404_not_the_spa(self, client) -> None:
        assert (await client.get("/api/nope")).status_code == 404

    async def test_missing_download_is_404(self, client) -> None:
        assert (await client.get("/api/downloads/999")).status_code == 404

    async def test_static_route_cannot_escape_the_static_directory(self, client) -> None:
        """A traversal falls back to the SPA shell rather than serving a file.

        The containment check compares resolved paths, not string prefixes --
        a sibling directory whose name merely starts the same way is not
        inside the static root.
        """
        for path in ("/../pyproject.toml", "/..%2Fpyproject.toml",
                     "/assets/../../../pyproject.toml"):
            response = await client.get(path)
            assert response.status_code in (200, 404)
            assert "[project]" not in response.text

    async def test_clear_history_reports_what_it_actually_deleted(self, client) -> None:
        ctx = client.app.state.conduit
        media_id = await ctx.repos.media.upsert(media_type="show", tmdb_id="1", title="Silo")
        kept = await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (S01E01)", indexer="A", indexer_id="1",
            state=DownloadState.COMPLETED,
        )
        for index, dead in enumerate(
            (DownloadState.DENIED, DownloadState.FAILED, DownloadState.CANCELLED), start=2
        ):
            await ctx.repos.downloads.create(
                media_id=media_id, display_title=f"Silo (S01E0{index})", indexer="A",
                indexer_id=str(index), state=dead,
            )

        response = await client.post("/api/actions/clear-history")
        # Three deleted, not "the four rows history happened to return".
        assert response.json() == {"ok": True, "removed": 3}
        assert await ctx.repos.downloads.get(kept) is not None


async def test_stalled_websocket_closes_so_browser_can_reconnect(
    monkeypatch, settings: Settings
) -> None:
    bus = EventBus()

    class Socket:
        def __init__(self) -> None:
            self.app = SimpleNamespace(state=SimpleNamespace(
                conduit=SimpleNamespace(settings=settings, bus=bus),
                supervisor=SimpleNamespace(status=lambda: []),
            ))
            self.client = SimpleNamespace(host="127.0.0.1")
            self.query_params = {}
            self.close_code = None

        async def accept(self) -> None:
            pass

        async def send_json(self, message) -> None:
            if message["topic"] == "state":
                for _ in range(QUEUE_LIMIT + 1):
                    bus.publish("download.progress")

        async def receive_text(self) -> str:
            await asyncio.Future()

        async def close(self, code: int, reason: str) -> None:
            self.close_code = code

    async def fake_state(ctx) -> dict:
        return {}

    monkeypatch.setattr("conduit.web.ws.state.build_state", fake_state)
    socket = Socket()
    await websocket_endpoint(socket)

    assert socket.close_code == 1013
    assert bus.listener_count == 0


class TestApprovalFlow:
    async def _pending(self, client) -> int:
        ctx = client.app.state.conduit
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="1", title="Silo"
        )
        return await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (Season 1)", indexer="Aither",
            indexer_id="42", size_bytes=88 * 1024**3, season=1, is_season_pack=True,
        )

    async def test_approve_moves_it_to_the_queue(self, client) -> None:
        download_id = await self._pending(client)
        response = await client.post("/api/downloads/approve", json={"ids": [download_id]})
        assert response.json() == {"ok": True, "approved": 1}
        ctx = client.app.state.conduit
        assert (await ctx.repos.downloads.get(download_id))["state"] == DownloadState.QUEUED

    async def test_deny_also_blocklists_the_release(self, client) -> None:
        download_id = await self._pending(client)
        await client.post("/api/downloads/deny", json={"ids": [download_id]})
        entries = (await client.get("/api/blocklist")).json()
        assert entries[0]["indexer_id"] == "42"

    async def test_stale_deny_does_not_blocklist_an_approved_release(self, client) -> None:
        download_id = await self._pending(client)
        await client.post("/api/downloads/approve", json={"ids": [download_id]})
        response = await client.post("/api/downloads/deny", json={"ids": [download_id]})
        assert response.json() == {"ok": True, "denied": 0}
        assert (await client.get("/api/blocklist")).json() == []

    async def test_retry_refuses_a_download_that_is_not_stuck(self, client) -> None:
        download_id = await self._pending(client)
        assert (await client.post(f"/api/downloads/{download_id}/retry")).status_code == 400

    async def test_remove_reports_unavailable_client_without_archiving(self, client) -> None:
        download_id = await self._pending(client)
        ctx = client.app.state.conduit
        await ctx.repos.downloads.set_state(download_id, DownloadState.DOWNLOADING)
        ctx.qbt = None

        response = await client.delete(f"/api/downloads/{download_id}")

        assert response.status_code == 503
        assert (await ctx.repos.downloads.get(download_id))["archived"] == 0


class TestMediaEndpoints:
    async def test_ignore_and_follow_round_trip(self, client) -> None:
        ctx = client.app.state.conduit
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="1", title="Silo"
        )
        await ctx.repos.wanted.upsert(media_id=media_id, season=1, episode=1)

        await client.patch(f"/api/media/{media_id}", json={"ignored": True})
        assert (await ctx.repos.media.get(media_id))["ignored"] == 1
        assert (await client.get("/api/upcoming")).json() == []

        await client.patch(f"/api/media/{media_id}", json={"ignored": False, "monitored": True})
        assert (await ctx.repos.media.get(media_id))["ignored"] == 0
        assert len((await client.get("/api/upcoming")).json()) == 1

    async def test_marking_episodes_seen_keeps_the_show_followed(self, client) -> None:
        ctx = client.app.state.conduit
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="1", title="Silo"
        )
        first = await ctx.repos.wanted.upsert(media_id=media_id, season=1, episode=1)
        await ctx.repos.wanted.upsert(media_id=media_id, season=2, episode=1)

        response = await client.post("/api/wanted/watched", json={"ids": [first]})
        assert response.json() == {"ok": True, "changed": 1}

        # The show is still followed and its other episode still tracked --
        # this is nothing like blocklisting.
        assert (await ctx.repos.media.get(media_id))["ignored"] == 0
        upcoming = (await client.get("/api/upcoming")).json()
        assert [u["season"] for u in upcoming] == [2]

    async def test_marking_a_whole_title_seen(self, client) -> None:
        ctx = client.app.state.conduit
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="1", title="Silo"
        )
        for episode in (1, 2, 3):
            await ctx.repos.wanted.upsert(media_id=media_id, season=1, episode=episode)

        response = await client.post(f"/api/media/{media_id}/watched", json={})
        assert response.json()["changed"] == 3
        assert (await client.get("/api/upcoming")).json() == []

    async def test_seen_through_a_given_episode(self, client) -> None:
        ctx = client.app.state.conduit
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="1", title="Silo"
        )
        for episode in (1, 2, 3):
            await ctx.repos.wanted.upsert(media_id=media_id, season=1, episode=episode)

        response = await client.post(
            f"/api/media/{media_id}/watched", json={"season": 1, "up_to_episode": 2}
        )
        assert response.json()["changed"] == 2
        assert [u["episode"] for u in (await client.get("/api/upcoming")).json()] == [3]

    async def test_marking_an_unknown_title_seen_is_404(self, client) -> None:
        assert (await client.post("/api/media/999/watched", json={})).status_code == 404

    async def test_accounts_never_hangs_the_dashboard(self, client) -> None:
        """Even with no reachable tracker the panel returns promptly."""
        import time

        started = time.perf_counter()
        response = await client.get("/api/accounts")
        assert response.status_code == 200
        assert time.perf_counter() - started < 10

    async def test_task_trigger_and_pause(self, client) -> None:
        assert (await client.post("/api/tasks/library-index/run")).json()["ok"] is True
        assert (await client.post("/api/tasks/nope/run")).status_code == 404
        response = await client.post("/api/tasks/library-index/enabled", json={"enabled": False})
        assert response.json()["enabled"] is False


class TestConfigRoundTrip:
    async def test_saving_a_valid_config_persists_it(self, client) -> None:
        config = (await client.get("/api/config")).json()["config"]
        config["policy"]["max_active_downloads"] = 9
        response = await client.put("/api/config", json=config)
        assert response.status_code == 200
        assert response.json()["config"]["policy"]["max_active_downloads"] == 9
        assert client.app.state.conduit.config.policy.max_active_downloads == 9

    async def test_an_invalid_config_is_refused_before_it_touches_disk(self, client) -> None:
        config = (await client.get("/api/config")).json()["config"]
        original = client.app.state.conduit.config.policy.max_active_downloads
        config["policy"]["max_active_downloads"] = "not a number"
        assert (await client.put("/api/config", json=config)).status_code == 422
        assert client.app.state.conduit.config.policy.max_active_downloads == original

    async def test_secrets_are_never_returned(self, client) -> None:
        payload = (await client.get("/api/config")).json()
        body = str(payload)
        assert "password" not in body.lower()
        assert settings_token_absent(body)


def settings_token_absent(body: str) -> bool:
    return "plex_token" not in body and "api_key\":" not in body


class TestLiveUpdates:
    """One snapshot per burst, and it must land on a topic the client reads."""

    @pytest.mark.parametrize(
        ("topics", "expected"),
        [
            (["download.created"], 0),
            (["event"], None),
            (["task.start", "task.finish"], None),
            # The snapshot goes on the last state-shaped message, not the last
            # message: the client ignores `state` on `event` and `*.progress`.
            (["download.created", "event"], 0),
            (["event", "download.created"], 1),
            (["download.created", "search.finished", "download.progress"], 1),
        ],
    )
    def test_snapshot_rides_the_last_state_shaped_message(self, topics, expected) -> None:
        assert snapshot_carrier(topics) == expected

    def test_an_empty_burst_needs_no_snapshot(self) -> None:
        assert snapshot_carrier([]) is None


class TestSecurity:
    @pytest.mark.parametrize(
        ("address", "private"),
        [
            ("127.0.0.1", True),
            ("192.168.1.20", True),
            ("10.0.0.5", True),
            ("172.16.4.4", True),
            ("172.31.255.255", True),
            ("::1", True),
            ("172.32.0.1", False),   # the reference project let this through
            ("172.99.1.1", False),
            ("8.8.8.8", False),
        ],
    )
    def test_private_range_detection_is_exact(self, address, private) -> None:
        assert is_private_address(address) is private

    async def test_lan_mode_rejects_a_public_client(self, tmp_path, config_store) -> None:
        settings = Settings(
            plex_token="t", tmdb_api_key="t", qbittorrent_password="p",
            DOWNLOAD_DIRS=str(tmp_path), conduit_auth_mode="lan",
            database_path=tmp_path / "c.db",
        )
        app = create_app(settings=settings, config_store=config_store, run_tasks=False)
        async with app.router.lifespan_context(app):
            # Not 203.0.113.x: Python's ipaddress module classifies the RFC 5737
            # documentation ranges as private, so they would be allowed.
            transport = httpx.ASGITransport(app=app, client=("8.8.8.8", 5000))
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
                assert (await http.get("/api/state")).status_code == 403
                # health stays open so a monitor can still reach it
                assert (await http.get("/api/health")).status_code == 200

    async def test_token_mode_requires_the_header(self, tmp_path, config_store) -> None:
        settings = Settings(
            plex_token="t", tmdb_api_key="t", qbittorrent_password="p",
            DOWNLOAD_DIRS=str(tmp_path), conduit_auth_mode="token",
            conduit_api_token="s3cret", database_path=tmp_path / "c.db",
        )
        app = create_app(settings=settings, config_store=config_store, run_tasks=False)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
                assert (await http.get("/api/state")).status_code == 403
                ok = await http.get("/api/state", headers={"X-Conduit-Token": "s3cret"})
                assert ok.status_code == 200
