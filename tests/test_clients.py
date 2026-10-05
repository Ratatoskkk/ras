"""Upstream response contracts that must not become false empty results."""

from __future__ import annotations

import pytest

from conduit.clients.indexers.base import SearchQuery
from conduit.clients.indexers.unit3d import Unit3dIndexer
from conduit.clients.plex import PlexClient
from conduit.config import IndexerConfig
from conduit.util.resilience import TransientError


@pytest.mark.parametrize("media, expected", [
    ([{"Part": [{"exists": False, "accessible": False}]}], False),
    ([{"Part": [{"exists": "0"}]}], False),
    ([{"Part": [{"exists": True, "accessible": False}]}], True),
    ([{"Part": [{"exists": False}]}, {"Part": [{"exists": True}]}], True),
    ([{"Part": [{"exists": True}, {"exists": False}]}], False),
    ([{"Part": [{"file": "/remote/movie.mkv"}]}], True),
    ([], True),
])
async def test_movie_ownership_uses_plex_file_checks(monkeypatch, media, expected) -> None:
    plex = PlexClient("http://plex.test:32400", "token")

    async def get_json(path, *, params, allow_404):
        assert path == "/library/metadata/6887"
        assert params["checkFiles"] == 1
        assert allow_404 is True
        return {"MediaContainer": {"Metadata": [{"Media": media}]}}

    monkeypatch.setattr(plex.server, "get_json", get_json)
    try:
        assert await plex.movie_file_exists("6887") is expected
    finally:
        await plex.aclose()


async def test_a_removed_plex_movie_is_missing(monkeypatch) -> None:
    plex = PlexClient("http://plex.test:32400", "token")

    async def get_json(*args, **kwargs):
        return None

    monkeypatch.setattr(plex.server, "get_json", get_json)
    try:
        assert await plex.movie_file_exists("6887") is False
    finally:
        await plex.aclose()


@pytest.mark.parametrize("payload", [{}, {"MediaContainer": {"Metadata": []}}])
async def test_an_incomplete_file_check_is_not_proof_a_movie_is_missing(
    monkeypatch, payload
) -> None:
    plex = PlexClient("http://plex.test:32400", "token")

    async def get_json(*args, **kwargs):
        return payload

    monkeypatch.setattr(plex.server, "get_json", get_json)
    try:
        with pytest.raises(TransientError, match="metadata"):
            await plex.movie_file_exists("6887")
    finally:
        await plex.aclose()


@pytest.mark.parametrize("payload", [
    None,
    {},
    {"error": "maintenance"},
    {"data": {"error": "maintenance"}},
    {"data": ["not a torrent"]},
])
async def test_malformed_tracker_results_are_failures_not_empty_searches(payload) -> None:
    indexer = Unit3dIndexer(
        IndexerConfig(
            name="Test", base_url="https://tracker.test", api_key_env="TRACKER_KEY"
        ),
        "test-key",
    )

    async def get_json(*args, **kwargs):
        return payload

    indexer.http.get_json = get_json
    try:
        with pytest.raises(TransientError, match="unexpected search payload"):
            await indexer.search(SearchQuery(media_type="movie", tmdb_id="27205"))
    finally:
        await indexer.aclose()


async def test_valid_empty_tracker_result_stays_an_empty_search() -> None:
    indexer = Unit3dIndexer(
        IndexerConfig(
            name="Test", base_url="https://tracker.test", api_key_env="TRACKER_KEY"
        ),
        "test-key",
    )

    async def get_json(*args, **kwargs):
        return {"data": []}

    indexer.http.get_json = get_json
    try:
        assert await indexer.search(SearchQuery(media_type="movie", tmdb_id="27205")) == []
    finally:
        await indexer.aclose()


async def test_tracker_search_returns_releases_from_every_page() -> None:
    indexer = Unit3dIndexer(
        IndexerConfig(
            name="Test", base_url="https://tracker.test", api_key_env="TRACKER_KEY"
        ),
        "test-key",
    )

    async def get_json(path, *, params):
        page = params.get("page", 1)
        return {
            "data": [{"id": page, "attributes": {
                "name": f"Movie {page}", "size": 1000, "seeders": 5,
            }}],
            "links": {"next": "https://tracker.test/api/torrents/filter?page=2"
                      if page == 1 else None},
            "meta": {"current_page": page},
        }

    indexer.http.get_json = get_json
    try:
        releases = await indexer.search(SearchQuery(media_type="movie", tmdb_id="27205"))
        assert [release.indexer_id for release in releases] == ["1", "2"]
    finally:
        await indexer.aclose()


async def test_failed_later_tracker_page_is_not_treated_as_complete_results() -> None:
    indexer = Unit3dIndexer(
        IndexerConfig(
            name="Test", base_url="https://tracker.test", api_key_env="TRACKER_KEY"
        ),
        "test-key",
    )

    async def get_json(path, *, params):
        if params["page"] == 1:
            return {
                "data": [{"id": 1, "attributes": {"name": "Movie", "size": 1000}}],
                "links": {"next": "https://tracker.test/api/torrents/filter?page=2"},
            }
        return {"error": "maintenance"}

    indexer.http.get_json = get_json
    try:
        with pytest.raises(TransientError, match="unexpected search payload"):
            await indexer.search(SearchQuery(media_type="movie", tmdb_id="27205"))
    finally:
        await indexer.aclose()
