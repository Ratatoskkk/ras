"""End-to-end service behaviour with stubbed upstreams."""

from __future__ import annotations

import asyncio
import time
from datetime import date, timedelta

import pytest

from conduit.clients.indexers.base import IndexerPool
from conduit.clients.plex import PlexClient
from conduit.config import ConfigStore, Settings
from conduit.db.repo import Repos
from conduit.domain.models import (
    DownloadState,
    LibraryItem,
    TorrentStatus,
    WantedState,
    WatchlistEntry,
)
from conduit.services import (
    calendar,
    janitor,
    library,
    monitor,
    queue,
    search,
    state,
    supervisor,
    watchlist,
)
from conduit.services.context import Conduit
from conduit.util.resilience import TransientError
from conftest import make_release
from fakes import FakeIndexer, FakePlex, FakeQbt, FakeTmdb, pool

GIB = 1024**3


@pytest.fixture
async def ctx(tmp_path, settings: Settings, config_store: ConfigStore):
    """A real context with fake edges."""
    downloads = tmp_path / "downloads"
    downloads.mkdir(exist_ok=True)
    settings.download_dirs_raw = str(downloads)

    conduit = Conduit(settings=settings, config_store=config_store)
    # The reserve is a real safety net in production, but it would make these
    # tests pass or fail depending on how full the machine's disk happens to
    # be. It is exercised deliberately in test_storage instead.
    conduit.config.policy.reserve_free_space_gb = 0
    await conduit.db.connect()
    conduit.repos = Repos(conduit.db)
    conduit.plex = FakePlex()
    conduit.tmdb = FakeTmdb()
    conduit.qbt = FakeQbt()
    conduit.indexers, _ = pool([])
    try:
        yield conduit
    finally:
        await conduit.db.close()


SILO_TMDB = {
    "125988": {
        "id": 125988,
        "name": "Silo",
        "status": "Returning Series",
        "poster_path": "/silo.jpg",
        "first_air_date": "2023-05-05",
        "seasons": [{"season_number": 1, "episode_count": 3}],
        "season/1": {
            "episodes": [
                {"episode_number": 1, "name": "Freedom Day", "air_date": "2023-05-05"},
                {"episode_number": 2, "name": "Holston's Pick", "air_date": "2023-05-05"},
                {"episode_number": 3, "name": "Machines", "air_date": "2023-05-12"},
            ]
        },
    }
}


class TestLibraryIndexing:
    async def test_index_stores_a_snapshot(self, ctx) -> None:
        ctx.plex = FakePlex(library=[
            LibraryItem(kind="movie", rating_key="1", tmdb_id="27205", watched=True),
            LibraryItem(kind="episode", rating_key="2", show_tmdb_id="125988",
                        season=1, episode=1, watched=True),
        ])
        result = await library.index_library(ctx)
        assert result["items"] == 2
        assert await ctx.repos.library.watched_episodes("125988") == {(1, 1)}

    async def test_empty_response_does_not_wipe_the_index(self, ctx) -> None:
        """A Plex hiccup must not make Conduit think your library vanished."""
        ctx.plex = FakePlex(library=[
            LibraryItem(kind="episode", rating_key="2", show_tmdb_id="125988",
                        season=1, episode=1),
        ])
        await library.index_library(ctx)
        ctx.plex = FakePlex(library=[])
        await library.index_library(ctx)
        assert await ctx.repos.library.have_episodes("125988") == {(1, 1)}

    async def test_partial_section_failure_keeps_the_previous_mirror(self, ctx, monkeypatch) -> None:
        await ctx.repos.library.replace_all([
            LibraryItem(kind="movie", rating_key="old", tmdb_id="27205", title="Inception"),
        ])
        plex = PlexClient("http://plex.test:32400", "token")

        async def sections():
            return [{"key": "good", "type": "movie"}, {"key": "bad", "type": "movie"}]

        async def page(key, item_type):
            if key == "bad":
                raise RuntimeError("section unavailable")
            return [{"ratingKey": "new", "title": "Other Film", "Guid": [{"id": "tmdb://99"}]}]

        monkeypatch.setattr(plex, "sections", sections)
        monkeypatch.setattr(plex, "_paged", page)
        ctx.plex = plex
        try:
            with pytest.raises(RuntimeError, match="section unavailable"):
                await library.index_library(ctx)
        finally:
            await plex.aclose()

        assert await ctx.repos.library.has_movie("27205")
        assert await ctx.repos.library.has_movie("99") is None

    async def test_short_plex_pages_continue_until_the_reported_total(self, monkeypatch) -> None:
        plex = PlexClient("http://plex.test:32400", "token")
        starts = []

        async def sections():
            return [{"key": "movies", "type": "movie"}]

        async def get_json(path, *, params):
            start = params["X-Plex-Container-Start"]
            starts.append(start)
            batch = ([{"ratingKey": "1"}, {"ratingKey": "2"}] if start == 0
                     else [{"ratingKey": "3"}])
            return {"MediaContainer": {"totalSize": 3, "Metadata": batch}}

        monkeypatch.setattr(plex, "sections", sections)
        monkeypatch.setattr(plex.server, "get_json", get_json)
        try:
            rows = await plex.index_library()
        finally:
            await plex.aclose()
        assert [row.rating_key for row in rows] == ["1", "2", "3"]
        assert starts == [0, 2]

    async def test_incomplete_plex_page_keeps_the_previous_mirror(self, ctx, monkeypatch) -> None:
        await ctx.repos.library.replace_all([
            LibraryItem(kind="movie", rating_key="old", tmdb_id="27205", title="Inception"),
        ])
        plex = PlexClient("http://plex.test:32400", "token")

        async def sections():
            return [{"key": "movies", "type": "movie"}]

        async def get_json(path, *, params):
            start = params["X-Plex-Container-Start"]
            batch = ([{"ratingKey": "new", "title": "Other Film"}] if start == 0
                     else [])
            return {"MediaContainer": {"totalSize": 2, "Metadata": batch}}

        monkeypatch.setattr(plex, "sections", sections)
        monkeypatch.setattr(plex.server, "get_json", get_json)
        ctx.plex = plex
        try:
            with pytest.raises(TransientError, match="incomplete page"):
                await library.index_library(ctx)
        finally:
            await plex.aclose()
        assert await ctx.repos.library.has_movie("27205")

    async def test_unmatched_entries_are_surfaced_not_swallowed(self, ctx) -> None:
        """The one gap de-duplication cannot close, made visible.

        A show Plex has not matched carries no TMDB id, so every "do I own
        this?" lookup misses it and Conduit would pay to fetch files already
        on the disk. Nothing can auto-fix that, so it has to be said out loud.
        """
        ctx.plex = FakePlex(library=[
            LibraryItem(kind="show", rating_key="1", title="Modern Family", tmdb_id="1421"),
            LibraryItem(kind="episode", rating_key="2", title="Modern Family",
                        show_tmdb_id="1421", season=3, episode=1),
            # The split-out folder Plex filed as its own, unmatched, show.
            LibraryItem(kind="show", rating_key="3", title="Modern Family S03"),
            LibraryItem(kind="episode", rating_key="4", title="Modern Family S03",
                        season=3, episode=2),
        ])
        result = await library.index_library(ctx)
        assert result["unmatched"] == 1

        rows = await ctx.repos.library.unmatched()
        assert [(r["title"], r["episodes"]) for r in rows] == [("Modern Family S03", 1)]

        events = await ctx.repos.events.recent(limit=20, category="library")
        assert any("no TMDB match" in e["message"] for e in events)

        snapshot = await state.build_state(ctx)
        assert snapshot["summary"]["unmatched"] == 1
        assert snapshot["unmatched"][0]["title"] == "Modern Family S03"

    async def test_a_matched_library_says_nothing(self, ctx) -> None:
        ctx.plex = FakePlex(library=[
            LibraryItem(kind="show", rating_key="1", title="Silo", tmdb_id="125988"),
            LibraryItem(kind="episode", rating_key="2", title="Silo",
                        show_tmdb_id="125988", season=1, episode=1),
        ])
        assert (await library.index_library(ctx))["unmatched"] == 0
        assert await ctx.repos.library.unmatched() == []
        assert await ctx.repos.events.recent(limit=20, category="library") == []

    async def test_the_warning_is_not_repeated_on_every_pass(self, ctx) -> None:
        """The index runs every half hour. One warning, not forty-eight a day."""
        ctx.plex = FakePlex(library=[
            LibraryItem(kind="show", rating_key="3", title="Modern Family S03"),
        ])
        await library.index_library(ctx)
        await library.index_library(ctx)
        events = await ctx.repos.events.recent(limit=20, category="library")
        assert len(events) == 1

        # ...and it says so when the user fixes it.
        ctx.plex = FakePlex(library=[
            LibraryItem(kind="show", rating_key="3", title="Modern Family", tmdb_id="1421"),
        ])
        await library.index_library(ctx)
        events = await ctx.repos.events.recent(limit=20, category="library")
        assert "matched again" in events[0]["message"]

    async def test_watched_shows_become_followed(self, ctx) -> None:
        ctx.tmdb = FakeTmdb(shows=SILO_TMDB)
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key="1", show_tmdb_id="125988",
                        season=1, episode=1, watched=True),
        ])
        assert await library.track_watched_shows(ctx) == 1
        media = await ctx.repos.media.list_all()
        assert media[0]["title"] == "Silo"

    async def test_ignored_shows_are_not_re_followed(self, ctx) -> None:
        ctx.tmdb = FakeTmdb(shows=SILO_TMDB)
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        await ctx.repos.media.set_flags(media_id, ignored=True)
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key="1", show_tmdb_id="125988",
                        season=1, episode=1, watched=True),
        ])
        assert await library.track_watched_shows(ctx) == 0


class TestWatchlist:
    async def test_adding_something_asks_for_a_search_straight_away(self, ctx) -> None:
        """Otherwise a title you just added sits there until the next search
        pass -- up to half an hour of nothing visible happening."""
        asked: list[str] = []
        ctx.request_run = lambda name: asked.append(name) or True
        ctx.plex = FakePlex(watchlist=[
            WatchlistEntry(rating_key="rk1", guid="plex://movie/1", title="Inception",
                           media_type="movie", year=2010, tmdb_id="27205"),
        ])
        ctx.tmdb = FakeTmdb(movies={"27205": {"title": "Inception",
                                              "digital_release": "2010-12-07"}})
        await watchlist.sync_watchlist(ctx)
        assert asked == ["search-full"]

    async def test_an_empty_watchlist_asks_for_nothing(self, ctx) -> None:
        asked: list[str] = []
        ctx.request_run = lambda name: asked.append(name) or True
        await watchlist.sync_watchlist(ctx)
        assert asked == []

    async def test_a_film_becomes_a_want(self, ctx) -> None:
        ctx.plex = FakePlex(watchlist=[
            WatchlistEntry(rating_key="rk1", guid="plex://movie/1", title="Inception",
                           media_type="movie", year=2010, tmdb_id="27205"),
        ])
        ctx.tmdb = FakeTmdb(movies={"27205": {"title": "Inception",
                                              "digital_release": "2010-12-07"}})
        result = await watchlist.sync_watchlist(ctx)
        assert result["added"] == 1
        media = await ctx.repos.media.list_all()
        assert media[0]["media_type"] == "movie"
        wants = await ctx.repos.wanted.for_media(int(media[0]["id"]))
        assert wants[0]["state"] == WantedState.SEARCHING

    @pytest.mark.parametrize("days_from_now, expected", [
        (-1, WantedState.SEARCHING),
        (1, WantedState.WAITING),
    ])
    async def test_movie_search_starts_when_its_release_date_arrives(
        self, ctx, days_from_now, expected
    ) -> None:
        release_date = date.today() + timedelta(days=days_from_now)

        class MovieTmdb(FakeTmdb):
            async def movie_release_date(self, tmdb_id):
                return release_date, "theatrical" if days_from_now < 0 else "home"

        ctx.tmdb = MovieTmdb()
        ctx.plex = FakePlex(watchlist=[
            WatchlistEntry(rating_key="rk1", guid="g", title="Film",
                           media_type="movie", tmdb_id="42"),
        ])
        await watchlist.sync_watchlist(ctx)
        media_id = (await ctx.repos.media.list_all())[0]["id"]
        assert (await ctx.repos.wanted.for_media(media_id))[0]["state"] == expected

    async def test_unresolved_show_stays_on_the_watchlist(self, ctx) -> None:
        ctx.plex = FakePlex(watchlist=[
            WatchlistEntry(rating_key="rk1", guid="g", title="Unknown Show",
                           media_type="show"),
        ])
        result = await watchlist.sync_watchlist(ctx)
        assert result["failed"] == 1
        assert ctx.plex.removed == []
        media_id = (await ctx.repos.media.list_all())[0]["id"]
        assert await ctx.repos.wanted.for_media(media_id) == []

    async def test_show_with_empty_episode_details_stays_on_watchlist(self, ctx) -> None:
        ctx.tmdb = FakeTmdb(shows={"42": {"season/1": {"episodes": []}}})
        ctx.plex = FakePlex(watchlist=[
            WatchlistEntry(rating_key="rk1", guid="g", title="Pending Show",
                           media_type="show", tmdb_id="42"),
        ])
        result = await watchlist.sync_watchlist(ctx)
        assert result["failed"] == 1
        assert ctx.plex.removed == []
        media_id = (await ctx.repos.media.list_all())[0]["id"]
        assert await ctx.repos.wanted.for_media(media_id) == []

    async def test_item_is_only_removed_after_it_is_recorded(self, ctx) -> None:
        ctx.plex = FakePlex(watchlist=[
            WatchlistEntry(rating_key="rk1", guid="g", title="Inception",
                           media_type="movie", tmdb_id="27205"),
        ])
        await watchlist.sync_watchlist(ctx)
        assert ctx.plex.removed == ["rk1"]
        assert await ctx.repos.media.list_all()

    async def test_removal_can_be_switched_off(self, ctx) -> None:
        ctx.config.policy.auto_remove_from_watchlist = False
        ctx.plex = FakePlex(watchlist=[
            WatchlistEntry(rating_key="rk1", guid="g", title="Inception",
                           media_type="movie", tmdb_id="27205"),
        ])
        await watchlist.sync_watchlist(ctx)
        assert ctx.plex.removed == []

    async def test_a_film_already_owned_is_not_wanted(self, ctx) -> None:
        await ctx.repos.library.replace_all([
            LibraryItem(kind="movie", rating_key="1", tmdb_id="27205", watched=False),
        ])
        ctx.plex = FakePlex(watchlist=[
            WatchlistEntry(rating_key="rk1", guid="g", title="Inception",
                           media_type="movie", tmdb_id="27205"),
        ])
        await watchlist.sync_watchlist(ctx)
        media = await ctx.repos.media.list_all()
        wants = await ctx.repos.wanted.for_media(int(media[0]["id"]))
        assert wants[0]["state"] == WantedState.DOWNLOADED

    async def test_one_bad_item_does_not_abort_the_batch(self, ctx) -> None:
        class Exploding(FakePlex):
            async def remove_from_watchlist(self, rating_key: str) -> bool:
                if rating_key == "bad":
                    raise RuntimeError("boom")
                return await super().remove_from_watchlist(rating_key)

        ctx.plex = Exploding(watchlist=[
            WatchlistEntry(rating_key="bad", guid="g", title="A", media_type="movie",
                           tmdb_id="1"),
            WatchlistEntry(rating_key="ok", guid="g", title="B", media_type="movie",
                           tmdb_id="2"),
        ])
        result = await watchlist.sync_watchlist(ctx)
        assert result["added"] == 2  # both were recorded before removal was attempted


class TestCalendar:
    async def test_expands_a_series_into_episode_wants(self, ctx) -> None:
        ctx.tmdb = FakeTmdb(shows=SILO_TMDB)
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        created = await calendar.refresh_media(ctx, await ctx.repos.media.get(media_id))
        assert created == 3
        wants = await ctx.repos.wanted.for_media(media_id)
        assert {w["episode"] for w in wants} == {1, 2, 3}

    async def test_backfill_assumption_tidies_the_existing_want_list(self, ctx) -> None:
        """Turning the assumption on must clean up wants already recorded."""
        ctx.tmdb = FakeTmdb(shows=SILO_TMDB)
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        await calendar.refresh_media(ctx, await ctx.repos.media.get(media_id))
        assert len(await ctx.repos.wanted.for_media(media_id)) == 3

        # You have now watched S01E02; E01 is assumed seen, E03 still wanted.
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key="2", show_tmdb_id="125988",
                        season=1, episode=2, watched=True),
        ])
        ctx.config.policy.assume_prior_seasons_watched = True
        await calendar.refresh_media(ctx, await ctx.repos.media.get(media_id))

        states = {
            r["episode"]: r["state"] for r in await ctx.repos.wanted.for_media(media_id)
        }
        assert states[1] == WantedState.IGNORED      # assumed watched
        assert states[2] == WantedState.DOWNLOADED   # present in the library
        assert states[3] in (WantedState.WAITING, WantedState.SEARCHING)

    async def test_episodes_already_on_disk_are_skipped(self, ctx) -> None:
        ctx.tmdb = FakeTmdb(shows=SILO_TMDB)
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key="1", show_tmdb_id="125988",
                        season=1, episode=1),
        ])
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        await calendar.refresh_media(ctx, await ctx.repos.media.get(media_id))
        wants = await ctx.repos.wanted.for_media(media_id)
        assert {w["episode"] for w in wants} == {2, 3}

    async def test_a_newly_owned_movie_stops_being_searched(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="movie", tmdb_id="27205", title="Inception"
        )
        wanted_id = await ctx.repos.wanted.upsert(
            media_id=media_id, season=None, episode=None, state=WantedState.SEARCHING
        )
        await ctx.repos.library.replace_all([
            LibraryItem(kind="movie", rating_key="1", tmdb_id="27205", title="Inception"),
        ])
        await calendar.refresh_media(ctx, await ctx.repos.media.get(media_id))
        assert (await ctx.repos.wanted.get(wanted_id))["state"] == WantedState.DOWNLOADED
        assert await ctx.repos.wanted.due_for_search() == []


class TestSupervisorCadence:
    """A cadence change has to be felt, not just saved.

    The task computed its delay once and then slept it out, so shortening the
    library index from 30 minutes meant waiting up to 30 minutes to see it --
    indistinguishable from the setting not working.
    """

    def _spec(self, seconds: int) -> supervisor.TaskSpec:
        async def noop(_ctx) -> None:
            return None
        return supervisor.TaskSpec(
            name="t", description="", run=noop, interval=lambda _ctx: seconds
        )

    async def test_a_shortened_interval_applies_mid_sleep(self, ctx, monkeypatch) -> None:
        monkeypatch.setattr(supervisor, "INTERVAL_RECHECK_SECONDS", 0.01)
        sup = supervisor.Supervisor(ctx)
        sup.register(self._spec(1800))
        state = sup.states["t"]

        # Long on the first look, shortened to nothing on the next -- exactly
        # what saving a new interval in Settings does.
        delays = iter([1800.0])
        monkeypatch.setattr(sup, "_delay_for", lambda _s: next(delays, 0.0))

        started = time.monotonic()
        await asyncio.wait_for(sup._sleep(state, 1800.0), timeout=3)
        assert time.monotonic() - started < 1.0

    async def test_a_lengthened_interval_does_not_extend_the_current_wait(
        self, ctx, monkeypatch
    ) -> None:
        """Only ever brings the deadline forward, so a task cannot be pushed
        further away by editing settings while it waits."""
        monkeypatch.setattr(supervisor, "INTERVAL_RECHECK_SECONDS", 0.01)
        sup = supervisor.Supervisor(ctx)
        sup.register(self._spec(30))
        state = sup.states["t"]
        monkeypatch.setattr(sup, "_delay_for", lambda _s: 9999.0)

        started = time.monotonic()
        await asyncio.wait_for(sup._sleep(state, 0.05), timeout=3)
        assert time.monotonic() - started < 1.0

    async def test_a_trigger_still_wakes_it_immediately(self, ctx, monkeypatch) -> None:
        monkeypatch.setattr(supervisor, "INTERVAL_RECHECK_SECONDS", 5.0)
        sup = supervisor.Supervisor(ctx)
        sup.register(self._spec(3600))
        state = sup.states["t"]

        async def trigger_soon() -> None:
            await asyncio.sleep(0.02)
            sup.trigger("t")

        started = time.monotonic()
        await asyncio.gather(
            asyncio.wait_for(sup._sleep(state, 3600.0), timeout=3), trigger_soon()
        )
        assert time.monotonic() - started < 1.0

    async def test_trigger_during_a_run_starts_another_run(self, ctx) -> None:
        sup = supervisor.Supervisor(ctx)
        first_started = asyncio.Event()
        finish_first = asyncio.Event()
        second_started = asyncio.Event()
        runs = 0

        async def run(_ctx) -> None:
            nonlocal runs
            runs += 1
            if runs == 1:
                first_started.set()
                await finish_first.wait()
            else:
                second_started.set()

        sup.register(supervisor.TaskSpec(
            name="t", description="", run=run, interval=lambda _ctx: 3600,
            jitter=0.0,
        ))
        await sup.start()
        try:
            await asyncio.wait_for(first_started.wait(), timeout=1)
            assert sup.trigger("t") is True
            finish_first.set()
            await asyncio.wait_for(second_started.wait(), timeout=1)
            assert runs == 2
        finally:
            await sup.stop()


async def test_live_indexer_filter_change_rebuilds_client(ctx, monkeypatch) -> None:
    monkeypatch.setattr(Settings, "tracker_api_key", lambda self, env_var: "test-key")
    ctx.rebuild_indexers()
    old = ctx.indexers.indexers[0]
    try:
        ctx.config.indexers[0].only_alive = False
        ctx.rebuild_indexers()
        assert ctx.indexers.indexers[0] is not old
        assert ctx.indexers.indexers[0].config.only_alive is False
    finally:
        await ctx.indexers.aclose()
        await old.aclose()


class TestSearch:
    async def _followed_show(self, ctx) -> int:
        ctx.tmdb = FakeTmdb(shows=SILO_TMDB)
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        await calendar.refresh_media(ctx, await ctx.repos.media.get(media_id))
        return media_id

    async def test_scheduled_search_starts_after_air_date_without_calendar_refresh(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        aired = (date.today() - timedelta(days=1)).isoformat()
        wanted_id = await ctx.repos.wanted.upsert(
            media_id=media_id, season=1, episode=4, air_date=aired,
            state=WantedState.WAITING,
        )
        ctx.indexers, _ = pool([make_release(
            "Silo S01E04 2160p ATVP WEB-DL DV HDR10+ H.265-Kitsune",
            indexer="Fake", tmdb_id="125988", size_bytes=9 * GIB,
        )])

        assert (await search.run_search(ctx))["grabbed"] == 1
        assert (await ctx.repos.wanted.get(wanted_id))["state"] == WantedState.GRABBED

    async def test_scheduled_search_recovers_a_release_after_slowing_down(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        aired = (date.today() - timedelta(days=10)).isoformat()
        wanted_id = await ctx.repos.wanted.upsert(
            media_id=media_id, season=1, episode=4, air_date=aired,
            state=WantedState.SEARCHING,
        )
        await ctx.repos.wanted.record_search_miss(wanted_id, "nothing found")
        await ctx.repos.wanted.set_state(wanted_id, WantedState.UNAVAILABLE)
        await ctx.repos.wanted.db.execute(
            "UPDATE wanted SET search_attempts=100, "
            "first_search_at=datetime('now','-200 days'), "
            "last_search_at=datetime('now','-2 days') WHERE id=?", (wanted_id,)
        )
        ctx.indexers, _ = pool([
            make_release(
                "Silo S01E04 2160p ATVP WEB-DL DV HDR10+ H.265-Kitsune",
                indexer="Fake", tmdb_id="125988", size_bytes=9 * GIB,
            ),
        ])

        assert (await search.run_search(ctx))["grabbed"] == 1
        assert (await ctx.repos.wanted.get(wanted_id))["state"] == WantedState.GRABBED

    async def test_grabs_a_season_pack_and_gates_it_for_approval(self, ctx) -> None:
        media_id = await self._followed_show(ctx)
        ctx.indexers, _ = pool([
            make_release("Silo S01 2160p ATVP WEB-DL DD+ 5.1 Atmos DV HDR10+ H.265-Kitsune",
                         tmdb_id="125988", size_bytes=88 * GIB, seeders=120),
        ])
        result = await search.run_search(ctx)
        assert result["grabbed"] == 1
        rows = await ctx.repos.downloads.dashboard()
        assert rows[0]["state"] == DownloadState.PENDING_APPROVAL
        assert rows[0]["display_title"] == "Silo (Season 1)"
        assert rows[0]["is_season_pack"] == 1
        assert rows[0]["wanted_count"] == 3
        wants = await ctx.repos.wanted.for_media(media_id)
        assert all(w["state"] == WantedState.GRABBED for w in wants)

    async def test_pack_size_uses_whole_season_when_only_some_episodes_are_missing(
        self, ctx
    ) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key=str(episode),
                        show_tmdb_id="125988", season=1, episode=episode)
            for episode in range(1, 8)
        ])
        for episode in range(8, 11):
            await ctx.repos.wanted.upsert(
                media_id=media_id, season=1, episode=episode,
                state=WantedState.SEARCHING,
            )
        ctx.indexers, _ = pool([
            make_release("Silo S01 2160p Remux DV HDR10 H.265-GRP",
                         tmdb_id="125988", size_bytes=900 * GIB),
        ])

        preview = await search.preview_media(ctx, media_id)
        assert preview["candidates"][0]["accepted"] is True
        result = await search.run_search(ctx)

        assert result["grabbed"] == 1
        assert (await ctx.repos.downloads.dashboard())[0]["wanted_count"] == 3

    async def test_one_missing_episode_does_not_grab_a_whole_season_pack(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key=str(number),
                        show_tmdb_id="125988", season=1, episode=number)
            for number in (1, 2)
        ])
        await ctx.repos.wanted.upsert(
            media_id=media_id, season=1, episode=3, state=WantedState.SEARCHING
        )
        ctx.indexers, _ = pool([
            make_release("Silo S01 2160p ATVP WEB-DL DV HDR10 H.265-GRP",
                         tmdb_id="125988", size_bytes=70 * GIB),
        ])

        assert (await search.run_search(ctx))["grabbed"] == 0
        assert await ctx.repos.downloads.dashboard() == []
        preview = await search.preview_media(ctx, media_id)
        assert any("pack" in reason for reason in preview["candidates"][0]["rejections"])

    async def test_search_limit_does_not_split_one_season_pack(self, ctx) -> None:
        media_id = await self._followed_show(ctx)
        ctx.indexers, _ = pool([
            make_release("Silo S01 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         tmdb_id="125988", size_bytes=88 * GIB),
        ])

        result = await search.run_search(ctx, limit=2)

        assert result["grabbed"] == 1
        assert result["searched"] == 3
        wants = await ctx.repos.wanted.for_media(media_id)
        assert all(w["state"] == WantedState.GRABBED for w in wants)
        assert (await ctx.repos.downloads.dashboard())[0]["wanted_count"] == 3

    async def test_falls_back_to_single_episodes_when_no_pack_exists(self, ctx) -> None:
        await self._followed_show(ctx)
        ctx.indexers, indexer = pool([
            make_release("Silo S01E01 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         tmdb_id="125988", size_bytes=9 * GIB),
            make_release("Silo S01E02 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         tmdb_id="125988", size_bytes=9 * GIB),
        ])
        result = await search.run_search(ctx)
        assert result["grabbed"] == 2
        titles = {r["display_title"] for r in await ctx.repos.downloads.dashboard()}
        assert titles == {"Silo (S01E01)", "Silo (S01E02)"}
        assert any(q.episode is not None for q in indexer.queries)

    async def test_multi_episode_release_claims_every_episode_it_contains(self, ctx) -> None:
        media_id = await self._followed_show(ctx)
        await ctx.repos.wanted.set_state(
            (await ctx.repos.wanted.for_media(media_id))[2]["id"], WantedState.WATCHED
        )
        ctx.indexers, _ = pool([
            make_release("Silo S01E01-E02 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         tmdb_id="125988", size_bytes=18 * GIB),
        ])

        assert (await search.run_search(ctx))["grabbed"] == 1
        wants = await ctx.repos.wanted.for_media(media_id)
        assert [row["state"] for row in wants] == [
            WantedState.GRABBED, WantedState.GRABBED, WantedState.WATCHED
        ]
        assert (await ctx.repos.downloads.dashboard())[0]["wanted_count"] == 2

    async def test_season_range_pack_claims_only_its_seasons(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        for season in (1, 2, 3, 4):
            await ctx.repos.wanted.upsert(
                media_id=media_id, season=season, episode=None,
                state=WantedState.SEARCHING,
            )
        ctx.indexers, _ = pool([
            make_release("Silo S01-S03 2160p ATVP WEB-DL DV HDR10 H.265-GRP",
                         tmdb_id="125988", size_bytes=60 * GIB),
        ])

        assert (await search.run_search(ctx))["grabbed"] == 1
        wants = await ctx.repos.wanted.for_media(media_id)
        assert [row["state"] for row in wants] == [
            WantedState.GRABBED, WantedState.GRABBED,
            WantedState.GRABBED, WantedState.SEARCHING,
        ]
        download = (await ctx.repos.downloads.dashboard())[0]
        assert download["wanted_count"] == 3
        assert download["display_title"] == "Silo (Seasons 1–3)"

    async def test_release_inspector_includes_candidates_after_the_first_fifty(self, ctx) -> None:
        media_id = await self._followed_show(ctx)
        ctx.indexers, _ = pool([
            make_release(f"Silo S01E01 2160p WEB-DL-GRP{number}",
                         indexer_id=str(number), tmdb_id="125988")
            for number in range(51)
        ])

        result = await search.preview_media(ctx, media_id)

        assert result["total"] == 51
        assert len(result["candidates"]) == 51

    async def test_single_episodes_are_not_gated(self, ctx) -> None:
        await self._followed_show(ctx)
        ctx.indexers, _ = pool([
            make_release("Silo S01E01 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         tmdb_id="125988", size_bytes=9 * GIB),
        ])
        await search.run_search(ctx)
        rows = await ctx.repos.downloads.dashboard()
        assert rows[0]["state"] == DownloadState.QUEUED

    async def test_records_why_nothing_qualified(self, ctx) -> None:
        await self._followed_show(ctx)
        ctx.indexers, _ = pool([
            make_release("Silo S01 480p DVDRip XviD-GRP", tmdb_id="125988",
                         size_bytes=2 * GIB, seeders=0),
        ])
        result = await search.run_search(ctx)
        assert result["grabbed"] == 0
        events = await ctx.repos.events.recent(limit=50, category="search")
        assert any("No release for" in e["message"] for e in events)

    async def test_a_release_is_never_grabbed_twice(self, ctx) -> None:
        await self._followed_show(ctx)
        ctx.indexers, _ = pool([
            make_release("Silo S01 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         tmdb_id="125988", size_bytes=88 * GIB),
        ])
        await search.run_search(ctx)
        await ctx.repos.wanted.set_state_for_media(
            (await ctx.repos.media.list_all())[0]["id"], WantedState.SEARCHING
        )
        await search.run_search(ctx)
        assert len(await ctx.repos.downloads.dashboard()) == 1

    async def test_concurrent_searches_do_not_grab_two_releases_for_one_want(self, ctx) -> None:
        media_id = await self._followed_show(ctx)
        releases = [
            make_release(
                f"Silo S01 2160p ATVP WEB-DL DV HDR10 H.265-GRP{number}",
                tmdb_id="125988", indexer_id=str(number), size_bytes=88 * GIB,
            )
            for number in (1, 2)
        ]
        both_searching = asyncio.Event()

        class AlternatingIndexer(FakeIndexer):
            calls = 0

            async def search(self, query):
                self.calls += 1
                call = self.calls
                if call == 2:
                    both_searching.set()
                await both_searching.wait()
                if call > 2:
                    return []
                return [releases[call - 1]]

        ctx.indexers = IndexerPool([AlternatingIndexer()])

        results = await asyncio.gather(
            search.search_media_now(ctx, media_id),
            search.search_media_now(ctx, media_id),
        )

        assert sum(result["grabbed"] for result in results) == 1
        assert len(await ctx.repos.downloads.dashboard()) == 1
        assert {row["state"] for row in await ctx.repos.wanted.for_media(media_id)} == {
            WantedState.GRABBED
        }

    async def test_nothing_you_already_have_is_ever_searched_for(self, ctx) -> None:
        """The money question on a private tracker: no re-downloads.

        With every episode present in Plex, the calendar creates no wants, so
        the tracker is never even queried -- let alone a torrent grabbed.
        """
        ctx.tmdb = FakeTmdb(shows=SILO_TMDB)
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key=str(n), show_tmdb_id="125988",
                        season=1, episode=n)
            for n in (1, 2, 3)
        ])
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        await calendar.refresh_media(ctx, await ctx.repos.media.get(media_id))

        ctx.indexers, indexer = pool([
            make_release("Silo S01 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         tmdb_id="125988", size_bytes=88 * GIB),
        ])
        result = await search.run_search(ctx)
        assert result == {"searched": 0, "grabbed": 0}
        assert indexer.queries == []
        assert await ctx.repos.downloads.dashboard() == []

    async def test_a_torrent_already_in_the_client_is_adopted_not_re_added(self, ctx) -> None:
        """No duplicate download, so no wasted ratio, even after a lost database."""
        from conduit.util import bencode

        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        download_id = await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (S01E01)",
            release_name="Silo S01E01 2160p WEB-DL-GRP", indexer="Fake", indexer_id="1",
            download_url="https://fake.test/1", size_bytes=GIB // 4,
            state=DownloadState.QUEUED,
        )
        ctx.indexers, indexer = pool([])

        # Put the exact same torrent in the client first.
        content = await indexer.fetch_torrent(
            make_release("Silo S01E01 2160p WEB-DL-GRP", size_bytes=GIB // 4,
                         download_url="https://fake.test/1")
        )
        summary = bencode.torrent_summary(content)
        from conduit.domain.models import TorrentStatus

        ctx.qbt.torrents_list.append(
            TorrentStatus(
                info_hash=str(summary["info_hash"]), name=str(summary["name"]),
                state="stalledUP", progress=1.0, eta_seconds=0, dlspeed=0,
                size_bytes=float(summary["size_bytes"]), save_path="X:\\", content_path="",
            )
        )

        await queue.dispatch_queue(ctx)
        assert ctx.qbt.added == []  # nothing was sent a second time
        assert (await ctx.repos.downloads.get(download_id))["state"] == DownloadState.DOWNLOADING

    async def test_a_failing_tracker_does_not_break_the_pass(self, ctx) -> None:
        await self._followed_show(ctx)
        ctx.indexers, indexer = pool([])
        indexer.fail = True
        assert (await search.run_search(ctx))["grabbed"] == 0
        wants = await ctx.repos.wanted.for_media((await ctx.repos.media.list_all())[0]["id"])
        assert all(w["search_attempts"] == 0 for w in wants)

    async def test_manual_search_does_not_report_a_known_release_as_new(self, ctx) -> None:
        media_id = await self._followed_show(ctx)
        ctx.indexers, _ = pool([
            make_release("Silo S01 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         tmdb_id="125988", size_bytes=88 * GIB),
        ])
        assert (await search.run_search(ctx))["grabbed"] == 1
        await ctx.repos.wanted.set_state_for_media(media_id, WantedState.SEARCHING)
        result = await search.search_media_now(ctx, media_id)
        assert result["grabbed"] == 0
        assert len(await ctx.repos.downloads.dashboard()) == 1

    async def test_pack_fallback_survives_a_season_level_want(self, ctx) -> None:
        """A title can hold both a season-level want and episode-level ones.

        The pack-to-singles fallback used to sort those keys together, and
        ``(1, None) < (1, 2)`` raises -- taking the whole title's search down
        with it. Trackers only ever offer packs for some seasons, so this path
        is the common one, not the exotic one.
        """
        media_id = await self._followed_show(ctx)
        # A watchlisted season with no TMDB episode list leaves exactly this:
        # one want for the whole season alongside the per-episode rows.
        await ctx.repos.wanted.upsert(
            media_id=media_id, season=1, episode=None, title="Season 1",
            state=WantedState.SEARCHING,
        )
        ctx.indexers, _ = pool([
            make_release("Silo S01E02 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         tmdb_id="125988", size_bytes=9 * GIB),
        ])
        result = await search.run_search(ctx)
        assert result["grabbed"] == 1
        titles = {r["display_title"] for r in await ctx.repos.downloads.dashboard()}
        assert titles == {"Silo (S01E02)"}

    async def test_two_trackers_sharing_a_torrent_id_are_kept_apart(self, ctx) -> None:
        """Torrent ids are unique per tracker, not globally.

        Both results below are torrent "1". Keyed on the id alone, the second
        tracker's parse was applied to the first tracker's release -- so a
        single episode inherited "this is a season pack", matched a pack
        search it does not satisfy, and outscored the real pack.
        """
        from conduit.clients.indexers.base import IndexerPool
        from fakes import FakeIndexer

        await self._followed_show(ctx)
        # Plenty of tracker rows carry no TMDB id, which is exactly when the
        # parsed title is what decides whether a release is the right show.
        alpha = FakeIndexer([
            make_release("Completely Different Show S01 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         indexer="Alpha", indexer_id="1", size_bytes=60 * GIB),
        ])
        alpha.name = "Alpha"
        beta = FakeIndexer([
            make_release("Silo S01 1080p ATVP WEB-DL H.264-GRP",
                         indexer="Beta", indexer_id="1", size_bytes=30 * GIB),
        ])
        beta.name = "Beta"
        ctx.indexers = IndexerPool([alpha, beta])

        assert (await search.run_search(ctx))["grabbed"] == 1
        row = (await ctx.repos.downloads.dashboard())[0]
        assert row["indexer"] == "Beta"
        assert "Completely Different Show" not in row["release_name"]

    async def test_preview_ranks_without_grabbing(self, ctx) -> None:
        media_id = await self._followed_show(ctx)
        ctx.indexers, _ = pool([
            make_release("Silo S01 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         tmdb_id="125988", size_bytes=88 * GIB),
            make_release("Silo S01 480p DVDRip XviD-GRP", tmdb_id="125988", seeders=0),
        ])
        result = await search.preview_media(ctx, media_id)
        assert result["total"] == 2
        assert result["candidates"][0]["accepted"] is True
        assert result["candidates"][-1]["rejections"]
        assert await ctx.repos.downloads.dashboard() == []

    async def test_preview_explains_known_and_unwanted_releases(self, ctx) -> None:
        media_id = await self._followed_show(ctx)
        await ctx.repos.wanted.set_state(
            (await ctx.repos.wanted.for_media(media_id))[1]["id"], WantedState.WATCHED
        )
        ctx.indexers, _ = pool([
            make_release("Silo S01E01 2160p ATVP WEB-DL DV HDR10 H.265-GRP",
                         tmdb_id="125988", indexer_id="known"),
            make_release("Silo S01E02 2160p ATVP WEB-DL DV HDR10 H.265-GRP",
                         tmdb_id="125988", indexer_id="seen"),
            make_release("Other Show S01E01 2160p ATVP WEB-DL DV HDR10 H.265-GRP",
                         indexer_id="wrong"),
        ])
        await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (S01E01)",
            indexer="Test", indexer_id="known", state=DownloadState.DENIED,
        )

        result = await search.preview_media(ctx, media_id)
        by_id = {candidate["indexer_id"]: candidate for candidate in result["candidates"]}

        assert any("already selected" in reason for reason in by_id["known"]["rejections"])
        assert any("outstanding" in reason for reason in by_id["seen"]["rejections"])
        assert any("match" in reason for reason in by_id["wrong"]["rejections"])


class TestPendingApprovals:
    """Approvals are a shelf, not an inbox.

    The user keeps the next season of a show sitting in the approval list as a
    deliberate "start it when I am ready" gate. That is a different decision
    from "do I want this new title at all?", so the two are separated -- and
    the separation is derived from the library, not stored.
    """

    async def _pending_season(self, ctx, *, season: int, tmdb_id: str = "1421") -> int:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id=tmdb_id, title="Modern Family"
        )
        return await ctx.repos.downloads.create(
            media_id=media_id, display_title=f"Modern Family (Season {season})",
            release_name=f"Modern Family S0{season} 1080p BluRay REMUX",
            indexer="Fake", indexer_id=str(season), season=season, is_season_pack=True,
            size_bytes=40 * GIB, state=DownloadState.PENDING_APPROVAL,
        )

    async def test_the_next_season_of_a_watched_show_is_its_own_thing(self, ctx) -> None:
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key=f"s2e{n}", title="Modern Family",
                        show_tmdb_id="1421", season=2, episode=n, watched=n <= 22)
            for n in range(1, 25)
        ])
        await self._pending_season(ctx, season=3)

        group = (await state.build_state(ctx))["pending_groups"][0]
        assert group["kind"] == "continuation"
        assert group["target_season"] == 3
        assert group["previous_season"] == {
            "season": 2, "episodes": 24, "watched": 22, "progress": 0.9167,
        }

    async def test_a_title_with_nothing_earlier_on_disk_is_just_new(self, ctx) -> None:
        await self._pending_season(ctx, season=1)
        group = (await state.build_state(ctx))["pending_groups"][0]
        assert group["kind"] == "new"
        assert "previous_season" not in group

    async def test_a_later_season_on_disk_does_not_make_it_a_continuation(self, ctx) -> None:
        """Only *earlier* seasons count. Owning season 5 says nothing about
        whether you are ready to start season 3."""
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key="s5e1", title="Modern Family",
                        show_tmdb_id="1421", season=5, episode=1, watched=True),
        ])
        await self._pending_season(ctx, season=3)
        assert (await state.build_state(ctx))["pending_groups"][0]["kind"] == "new"

    async def test_continuations_are_listed_first(self, ctx) -> None:
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key="s1e1", title="Modern Family",
                        show_tmdb_id="1421", season=1, episode=1, watched=True),
        ])
        await self._pending_season(ctx, season=2)
        # An unrelated new title, alphabetically earlier, with more items.
        other = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="999", title="Andor"
        )
        for episode in (1, 2):
            await ctx.repos.downloads.create(
                media_id=other, display_title=f"Andor (S01E0{episode})", indexer="Fake",
                indexer_id=f"a{episode}", season=1, episode_from=episode,
                size_bytes=GIB, state=DownloadState.PENDING_APPROVAL,
            )

        groups = (await state.build_state(ctx))["pending_groups"]
        assert [g["kind"] for g in groups] == ["continuation", "new"]
        assert groups[0]["title"] == "Modern Family"


class TestQueueDispatch:
    async def _queued(self, ctx, size=GIB // 4) -> int:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        return await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (S01E01)",
            release_name="Silo S01E01 2160p WEB-DL-GRP", indexer="Fake", indexer_id="1",
            download_url="https://fake.test/download/1", size_bytes=size,
            state=DownloadState.QUEUED,
        )

    async def test_sends_the_torrent_and_records_its_hash(self, ctx) -> None:
        download_id = await self._queued(ctx)
        ctx.indexers, _ = pool([])
        result = await queue.dispatch_queue(ctx)
        assert result["sent"] == 1
        row = await ctx.repos.downloads.get(download_id)
        assert row["state"] == DownloadState.DOWNLOADING
        assert row["info_hash"] and len(row["info_hash"]) == 40
        assert ctx.qbt.added[0]["category"] == "conduit"

    async def test_verified_size_crossing_gate_requires_approval_before_add(self, ctx) -> None:
        from conduit.util import bencode

        download_id = await self._queued(ctx)
        ctx.config.policy.approval_size_threshold_gb = 1
        ctx.indexers, indexer = pool([])

        async def actual_torrent(release):
            return bencode.encode({
                b"announce": b"https://fake.test/announce",
                b"info": {
                    b"name": release.name.encode(),
                    b"length": 2 * GIB,
                    b"piece length": 262144,
                },
            })

        indexer.fetch_torrent = actual_torrent
        assert (await queue.dispatch_queue(ctx))["sent"] == 0
        row = await ctx.repos.downloads.get(download_id)
        assert row["state"] == DownloadState.PENDING_APPROVAL
        assert row["size_bytes"] == 2 * GIB
        assert ctx.qbt.added == []

        assert await ctx.repos.downloads.approve(download_id)
        assert (await queue.dispatch_queue(ctx))["sent"] == 1
        assert (await ctx.repos.downloads.get(download_id))["state"] == DownloadState.DOWNLOADING

    async def test_dry_run_sends_nothing(self, ctx) -> None:
        await self._queued(ctx)
        ctx.config.policy.dry_run = True
        result = await queue.dispatch_queue(ctx)
        assert result["sent"] == 0
        assert ctx.qbt.added == []

    async def test_removed_queued_download_is_never_dispatched(self, ctx) -> None:
        download_id = await self._queued(ctx)
        await ctx.repos.downloads.archive(download_id)

        result = await queue.dispatch_queue(ctx)

        assert result["sent"] == 0
        assert ctx.qbt.added == []
        assert (await ctx.repos.downloads.get(download_id))["archived"] == 1

    async def test_manual_and_scheduled_dispatch_do_not_send_twice(self, ctx) -> None:
        await self._queued(ctx)
        ctx.indexers, _ = pool([])

        results = await asyncio.gather(
            queue.dispatch_queue(ctx), queue.dispatch_queue(ctx)
        )

        assert sum(result["sent"] for result in results) == 1
        assert len(ctx.qbt.added) == 1

    async def test_remove_during_torrent_fetch_does_not_leave_a_download(self, ctx) -> None:
        download_id = await self._queued(ctx)
        ctx.indexers, indexer = pool([])
        fetched = asyncio.Event()
        release_fetch = asyncio.Event()
        fetch = indexer.fetch_torrent

        async def delayed_fetch(release):
            fetched.set()
            await release_fetch.wait()
            return await fetch(release)

        indexer.fetch_torrent = delayed_fetch
        dispatch = asyncio.create_task(queue.dispatch_queue(ctx))
        await asyncio.wait_for(fetched.wait(), 2)
        removal = asyncio.create_task(janitor.remove_download(ctx, download_id))
        release_fetch.set()
        await asyncio.wait_for(asyncio.gather(dispatch, removal), 2)

        assert (await ctx.repos.downloads.get(download_id))["archived"] == 1
        assert ctx.qbt.torrents_list == []

    async def test_insufficient_space_is_reported_not_swallowed(self, ctx) -> None:
        download_id = await self._queued(ctx, size=900_000 * GIB)
        ctx.indexers, _ = pool([])
        await queue.dispatch_queue(ctx)
        row = await ctx.repos.downloads.get(download_id)
        assert row["state"] == DownloadState.NO_SPACE
        assert "needs" in (row["error"] or "")

    async def test_tracker_timeout_does_not_bypass_pre_add_hash_and_size_checks(
        self, ctx
    ) -> None:
        """Without the file, its true size and info-hash remain unknown."""
        download_id = await self._queued(ctx)
        class Timeout:
            async def fetch_torrent(self, release):
                raise TimeoutError("ReadTimeout")
        ctx.indexers = Timeout()

        result = await queue.dispatch_queue(ctx)
        assert result["sent"] == 0
        assert ctx.qbt.added == []
        row = await ctx.repos.downloads.get(download_id)
        assert row["state"] == DownloadState.FAILED
        assert "could not fetch .torrent" in (row["error"] or "")

        events = await ctx.repos.events.recent(limit=20, category="queue")
        assert not any("Started" in e["message"] for e in events)

    async def test_malformed_torrent_is_not_sent_to_the_client(self, ctx) -> None:
        download_id = await self._queued(ctx)

        class Malformed:
            async def fetch_torrent(self, release):
                return b"d3:foo"

        ctx.indexers = Malformed()
        assert (await queue.dispatch_queue(ctx))["sent"] == 0
        assert ctx.qbt.added == []
        row = await ctx.repos.downloads.get(download_id)
        assert row["state"] == DownloadState.FAILED
        assert "malformed" in row["error"]

    async def test_an_empty_torrent_fetch_does_not_reach_the_client(self, ctx) -> None:
        download_id = await self._queued(ctx)

        class Timeout:
            async def fetch_torrent(self, release):
                return None
        ctx.indexers = Timeout()

        assert (await queue.dispatch_queue(ctx))["sent"] == 0
        row = await ctx.repos.downloads.get(download_id)
        assert row["state"] == DownloadState.FAILED
        assert ctx.qbt.added == []

    async def test_file_add_accepted_without_appearing_is_reported_as_failed(
        self, ctx, monkeypatch
    ) -> None:
        monkeypatch.setattr(queue, "CONFIRM_ATTEMPTS", 2)
        monkeypatch.setattr(queue, "CONFIRM_DELAY_SECONDS", 0)
        download_id = await self._queued(ctx)
        ctx.indexers, _ = pool([])
        ctx.qbt.file_adds_never_land = True

        assert (await queue.dispatch_queue(ctx))["sent"] == 0
        row = await ctx.repos.downloads.get(download_id)
        assert row["state"] == DownloadState.FAILED
        assert "never appeared" in (row["error"] or "")

    async def test_concurrency_limit_is_respected(self, ctx) -> None:
        ctx.config.policy.max_active_downloads = 1
        ctx.indexers, _ = pool([])
        await self._queued(ctx)
        media_id = (await ctx.repos.media.list_all())[0]["id"]
        await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (S01E02)", indexer="Fake", indexer_id="2",
            download_url="https://fake.test/download/2", size_bytes=GIB,
            state=DownloadState.QUEUED,
        )
        assert (await queue.dispatch_queue(ctx))["sent"] == 1

    async def test_retry_puts_a_failed_item_back(self, ctx) -> None:
        download_id = await self._queued(ctx)
        await ctx.repos.downloads.set_state(download_id, DownloadState.FAILED, error="x")
        assert await queue.retry_download(ctx, download_id)
        assert (await ctx.repos.downloads.get(download_id))["state"] == DownloadState.QUEUED

    async def test_retry_does_not_requeue_a_want_already_in_plex(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="movie", tmdb_id="27205", title="Inception"
        )
        wanted_id = await ctx.repos.wanted.upsert(
            media_id=media_id, season=None, episode=None,
            state=WantedState.SEARCHING,
        )
        download_id = await ctx.repos.downloads.create(
            media_id=media_id, wanted_id=wanted_id, display_title="Inception",
            indexer="Fake", indexer_id="1", size_bytes=GIB,
            state=DownloadState.FAILED, wanted_count=1,
        )
        await ctx.repos.wanted.set_state(
            wanted_id, WantedState.DOWNLOADED, reason="present in library"
        )

        assert await queue.retry_download(ctx, download_id) is False
        assert (await ctx.repos.downloads.get(download_id))["state"] == DownloadState.FAILED


class TestMonitor:
    async def test_legacy_season_range_completion_marks_only_covered_seasons(
        self, ctx
    ) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        for season in (1, 2, 3):
            await ctx.repos.wanted.upsert(
                media_id=media_id, season=season, episode=1,
                state=WantedState.SEARCHING,
            )
        await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (Seasons 1–2)",
            release_name="Silo S01-S02 2160p WEB-DL-GRP",
            indexer="F", indexer_id="legacy-range", season=1,
            is_season_pack=True, state=DownloadState.DOWNLOADING,
            info_hash="b" * 40,
        )
        ctx.qbt.torrents_list.append(TorrentStatus(
            info_hash="b" * 40, name="Silo S01-S02", state="stalledUP",
            progress=1.0, eta_seconds=0, dlspeed=0, size_bytes=80 * GIB,
            save_path="D:\\Torrents", content_path="",
        ))

        await monitor.monitor_downloads(ctx)

        wants = await ctx.repos.wanted.for_media(media_id)
        assert [row["state"] for row in wants] == [
            WantedState.GRABBED, WantedState.GRABBED, WantedState.SEARCHING,
        ]

    async def test_non_contiguous_release_does_not_claim_middle_episode_on_completion(
        self, ctx
    ) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        for episode in (1, 2, 3):
            await ctx.repos.wanted.upsert(
                media_id=media_id, season=1, episode=episode,
                state=WantedState.SEARCHING,
            )
        ctx.config.policy.prefer_season_packs = False
        ctx.indexers, _ = pool([
            make_release("Silo S01E01E03 2160p ATVP WEB-DL DV HDR10 H.265-Kitsune",
                         indexer="Fake", tmdb_id="125988", size_bytes=18 * GIB),
        ])
        assert (await search.run_search(ctx))["grabbed"] == 1
        assert (await queue.dispatch_queue(ctx))["sent"] == 1
        ctx.qbt.complete_all()

        await monitor.monitor_downloads(ctx)

        wants = await ctx.repos.wanted.for_media(media_id)
        assert [row["state"] for row in wants] == [
            WantedState.GRABBED, WantedState.SEARCHING, WantedState.GRABBED
        ]

    async def test_completion_updates_state_and_asks_plex_to_rescan(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        wanted_id = await ctx.repos.wanted.upsert(media_id=media_id, season=1, episode=1)
        download_id = await ctx.repos.downloads.create(
            media_id=media_id, wanted_id=wanted_id, display_title="Silo (S01E01)",
            indexer="Fake", indexer_id="1", download_url="https://fake.test/download/1",
            size_bytes=GIB, season=1, episode_from=1, episode_to=1,
            state=DownloadState.QUEUED,
        )
        ctx.indexers, _ = pool([])
        await queue.dispatch_queue(ctx)
        ctx.qbt.complete_all()

        result = await monitor.monitor_downloads(ctx)
        assert result["completed"] == 1
        assert (await ctx.repos.downloads.get(download_id))["state"] == DownloadState.COMPLETED
        assert (await ctx.repos.wanted.get(wanted_id))["state"] == WantedState.GRABBED
        assert ctx.plex.refreshed == ["show"]

    async def _in_flight(self, ctx) -> tuple[int, int]:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        wanted_id = await ctx.repos.wanted.upsert(media_id=media_id, season=1, episode=1)
        download_id = await ctx.repos.downloads.create(
            media_id=media_id, wanted_id=wanted_id, display_title="Silo (S01E01)",
            indexer="Fake", indexer_id="1", size_bytes=GIB,
            state=DownloadState.DOWNLOADING, info_hash="a" * 40,
        )
        return download_id, wanted_id

    async def test_one_missed_sighting_does_not_cancel_a_download(self, ctx) -> None:
        """qBittorrent fetches URL adds in the background and serves an empty
        list just after a restart. Acting on the first miss turned an ordinary
        delay into a cancelled download that then re-queued in a loop."""
        download_id, wanted_id = await self._in_flight(ctx)

        result = await monitor.monitor_downloads(ctx)
        assert result["lost"] == 1
        assert result["abandoned"] == 0
        row = await ctx.repos.downloads.get(download_id)
        assert row["state"] == DownloadState.DOWNLOADING
        assert row["missing_since"] is not None
        assert (await ctx.repos.wanted.get(wanted_id))["state"] == WantedState.WAITING

    async def test_a_torrent_that_comes_back_clears_the_clock(self, ctx) -> None:
        download_id, _ = await self._in_flight(ctx)
        await monitor.monitor_downloads(ctx)
        assert (await ctx.repos.downloads.get(download_id))["missing_since"] is not None

        ctx.qbt.torrents_list.append(
            TorrentStatus(info_hash="a" * 40, name="Silo", state="downloading",
                          progress=0.4, eta_seconds=60, dlspeed=1.0, size_bytes=GIB,
                          save_path="D:\\", content_path="")
        )
        await monitor.monitor_downloads(ctx)
        assert (await ctx.repos.downloads.get(download_id))["missing_since"] is None

    async def test_a_torrent_gone_past_the_grace_window_returns_to_searching(
        self, ctx
    ) -> None:
        download_id, wanted_id = await self._in_flight(ctx)
        await monitor.monitor_downloads(ctx)
        # Age the clock past the grace window rather than waiting five minutes.
        await ctx.db.execute(
            "UPDATE downloads SET missing_since = datetime('now', '-1 hour') WHERE id = ?",
            (download_id,),
        )

        result = await monitor.monitor_downloads(ctx)
        assert result["abandoned"] == 1
        assert (await ctx.repos.downloads.get(download_id))["state"] == DownloadState.CANCELLED
        assert (await ctx.repos.wanted.get(wanted_id))["state"] == WantedState.SEARCHING

    async def test_a_missing_pack_reopens_every_episode_it_covered(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        wanted_ids = [
            await ctx.repos.wanted.upsert(
                media_id=media_id, season=1, episode=episode,
                state=WantedState.SEARCHING,
            )
            for episode in (1, 2, 3)
        ]
        download_id = await ctx.repos.downloads.create(
            media_id=media_id, wanted_id=wanted_ids[0], display_title="Silo (Season 1)",
            indexer="Fake", indexer_id="pack", size_bytes=GIB,
            season=1, is_season_pack=True, state=DownloadState.DOWNLOADING,
            info_hash="a" * 40, wanted_count=3,
        )
        for wanted_id in wanted_ids:
            await ctx.repos.wanted.set_state(
                wanted_id, WantedState.GRABBED, reason=f"grabbed #{download_id}"
            )
        await monitor.monitor_downloads(ctx)
        await ctx.db.execute(
            "UPDATE downloads SET missing_since = datetime('now', '-1 hour') WHERE id = ?",
            (download_id,),
        )
        await monitor.monitor_downloads(ctx)
        assert (await ctx.repos.downloads.get(download_id))["state"] == DownloadState.CANCELLED
        assert {
            (await ctx.repos.wanted.get(wanted_id))["state"] for wanted_id in wanted_ids
        } == {WantedState.SEARCHING}

        await ctx.repos.wanted.set_state(
            wanted_ids[0], WantedState.GRABBED, reason="grabbed #999"
        )
        assert await queue.retry_download(ctx, download_id) is False
        assert (await ctx.repos.downloads.get(download_id))["state"] == DownloadState.CANCELLED
        await ctx.repos.wanted.set_state(
            wanted_ids[0], WantedState.SEARCHING,
            reason=f"download #{download_id} was removed",
        )
        assert await queue.retry_download(ctx, download_id) is True
        assert (await ctx.repos.downloads.get(download_id))["state"] == DownloadState.QUEUED
        assert {
            (await ctx.repos.wanted.get(wanted_id))["state"] for wanted_id in wanted_ids
        } == {WantedState.GRABBED}

    async def test_cancelled_retry_refuses_a_want_already_claimed_elsewhere(self, ctx) -> None:
        download_id, wanted_id = await self._in_flight(ctx)
        await monitor.monitor_downloads(ctx)
        await ctx.db.execute(
            "UPDATE downloads SET missing_since = datetime('now', '-1 hour') WHERE id = ?",
            (download_id,),
        )
        await monitor.monitor_downloads(ctx)
        await ctx.repos.wanted.set_state(
            wanted_id, WantedState.GRABBED, reason="grabbed #999"
        )

        assert await queue.retry_download(ctx, download_id) is False
        assert (await ctx.repos.downloads.get(download_id))["state"] == DownloadState.CANCELLED

    async def test_legacy_cancelled_single_can_still_be_retried(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        wanted_id = await ctx.repos.wanted.upsert(
            media_id=media_id, season=1, episode=1,
            state=WantedState.SEARCHING,
        )
        download_id = await ctx.repos.downloads.create(
            media_id=media_id, wanted_id=wanted_id, display_title="Silo S01E01",
            indexer="Fake", indexer_id="old", season=1,
            episode_from=1, episode_to=1, state=DownloadState.CANCELLED,
        )
        await ctx.repos.wanted.set_state(
            wanted_id, WantedState.SEARCHING, reason="download was removed"
        )

        assert await queue.retry_download(ctx, download_id) is True
        assert (await ctx.repos.wanted.get(wanted_id))["state"] == WantedState.GRABBED


class TestJanitor:
    async def test_watched_films_become_reclaimable(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="movie", tmdb_id="27205", title="Inception"
        )
        download_id = await ctx.repos.downloads.create(
            media_id=media_id, display_title="Inception", indexer="F", indexer_id="1",
            size_bytes=60 * GIB, state=DownloadState.COMPLETED,
        )
        await ctx.repos.library.replace_all([
            LibraryItem(kind="movie", rating_key="1", tmdb_id="27205", watched=True),
        ])
        await janitor.sync_watched_flags(ctx)
        assert (await ctx.repos.downloads.get(download_id))["watched"] == 1
        assert len(await janitor.cleanup_candidates(ctx)) == 1

    async def test_a_partly_watched_pack_is_not_reclaimable(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (Season 1)", indexer="F", indexer_id="1",
            size_bytes=88 * GIB, season=1, is_season_pack=True,
            state=DownloadState.COMPLETED,
        )
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key="1", show_tmdb_id="125988",
                        season=1, episode=1, watched=True),
            LibraryItem(kind="episode", rating_key="2", show_tmdb_id="125988",
                        season=1, episode=2, watched=False),
        ])
        await janitor.sync_watched_flags(ctx)
        assert await janitor.cleanup_candidates(ctx) == []

    async def test_multi_season_pack_waits_until_every_included_season_is_watched(
        self, ctx
    ) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        download_id = await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (Seasons 1–2)",
            release_name="Silo S01-S02 2160p WEB-DL-GRP",
            indexer="F", indexer_id="range", size_bytes=80 * GIB,
            season=1, is_season_pack=True, state=DownloadState.COMPLETED,
        )
        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key="1", show_tmdb_id="125988",
                        season=1, episode=1, watched=True),
            LibraryItem(kind="episode", rating_key="2", show_tmdb_id="125988",
                        season=2, episode=1, watched=False),
        ])

        await janitor.sync_watched_flags(ctx)

        assert (await ctx.repos.downloads.get(download_id))["watched"] == 0
        assert await janitor.cleanup_candidates(ctx) == []

        await ctx.repos.library.replace_all([
            LibraryItem(kind="episode", rating_key=str(season), show_tmdb_id="125988",
                        season=season, episode=1, watched=True)
            for season in (1, 2)
        ])
        await janitor.sync_watched_flags(ctx)
        assert (await ctx.repos.downloads.get(download_id))["watched"] == 1
        assert len(await janitor.cleanup_candidates(ctx)) == 1

    async def test_a_season_pack_retires_the_singles_it_replaces(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        single = await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (S01E01)", indexer="F", indexer_id="1",
            size_bytes=GIB, season=1, episode_from=1, episode_to=1,
            state=DownloadState.COMPLETED,
        )
        pack = await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (Season 1)", indexer="F", indexer_id="2",
            size_bytes=88 * GIB, season=1, is_season_pack=True,
            state=DownloadState.COMPLETED,
        )
        assert await janitor.retire_superseded_episodes(ctx, media_id, 1, pack) == 1
        assert (await ctx.repos.downloads.get(single))["archived"] == 1
        assert (await ctx.repos.downloads.get(pack))["archived"] == 0

    async def test_season_range_pack_retires_singles_only_within_its_range(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        singles = {}
        for season in (1, 2, 3):
            singles[season] = await ctx.repos.downloads.create(
                media_id=media_id, display_title=f"Silo (S{season:02d}E01)",
                indexer="F", indexer_id=str(season), size_bytes=GIB,
                season=season, episode_from=1, episode_to=1,
                state=DownloadState.COMPLETED,
            )
        await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (Seasons 1–2)",
            release_name="Silo S01-S02 2160p WEB-DL-GRP",
            indexer="F", indexer_id="range", size_bytes=80 * GIB,
            season=1, is_season_pack=True, state=DownloadState.COMPLETED,
        )

        assert (await janitor.housekeeping(ctx))["superseded_retired"] == 2
        assert (await ctx.repos.downloads.get(singles[1]))["archived"] == 1
        assert (await ctx.repos.downloads.get(singles[2]))["archived"] == 1
        assert (await ctx.repos.downloads.get(singles[3]))["archived"] == 0

    async def test_superseded_single_waits_for_seeding_then_retires(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        single = await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (S01E01)", indexer="F", indexer_id="1",
            size_bytes=GIB, season=1, episode_from=1, episode_to=1,
            state=DownloadState.COMPLETED, info_hash="a" * 40,
        )
        pack = await ctx.repos.downloads.create(
            media_id=media_id, display_title="Silo (Season 1)", indexer="F", indexer_id="2",
            size_bytes=88 * GIB, season=1, is_season_pack=True,
            state=DownloadState.COMPLETED,
        )
        torrent = TorrentStatus(
            info_hash="a" * 40, name="Silo S01E01", state="stalledUP", progress=1.0,
            eta_seconds=0, dlspeed=0, size_bytes=GIB, save_path="D:\\Torrents",
            content_path="", seeding_time=60, ratio=0.5,
        )
        ctx.qbt.torrents_list.append(torrent)
        assert await janitor.retire_superseded_episodes(ctx, media_id, 1, pack) == 0
        assert (await ctx.repos.downloads.get(single))["archived"] == 0
        assert ctx.qbt.deleted == []

        torrent.seeding_time = 6 * 86400
        assert (await janitor.housekeeping(ctx))["superseded_retired"] == 1
        assert (await ctx.repos.downloads.get(single))["archived"] == 1
        assert ctx.qbt.deleted == [(["a" * 40], True)]


class TestSeedingAwareReclaim:
    async def _watched_download(self, ctx, seeded_seconds: float, size=40 * GIB) -> int:
        from conduit.domain.models import TorrentStatus

        media_id = await ctx.repos.media.upsert(
            media_type="movie", tmdb_id="27205", title="Inception"
        )
        download_id = await ctx.repos.downloads.create(
            media_id=media_id, display_title="Inception", indexer="F", indexer_id="1",
            size_bytes=size, state=DownloadState.COMPLETED,
        )
        await ctx.repos.downloads.set_hash(download_id, "a" * 40)
        await ctx.repos.downloads.set_watched(download_id, True)
        ctx.qbt.torrents_list.append(
            TorrentStatus(
                info_hash="a" * 40, name="Inception", state="stalledUP", progress=1.0,
                eta_seconds=0, dlspeed=0, size_bytes=size, save_path="D:\\Torrents",
                content_path="", seeding_time=int(seeded_seconds), ratio=1.5,
            )
        )
        return download_id

    async def test_still_seeding_is_listed_but_not_offered(self, ctx) -> None:
        await self._watched_download(ctx, seeded_seconds=2 * 86400)  # 2 of 5 days
        candidates = await janitor.cleanup_candidates(ctx)
        assert len(candidates) == 1
        assert candidates[0]["seed_satisfied"] is False
        assert "seeding left" in candidates[0]["seed_reason"]
        assert 0 < candidates[0]["seed_progress"] < 1

    async def test_seed_goal_met_is_offered(self, ctx) -> None:
        await self._watched_download(ctx, seeded_seconds=6 * 86400)
        candidate = (await janitor.cleanup_candidates(ctx))[0]
        assert candidate["seed_satisfied"] is True
        assert candidate["seed_reason"] == "seed time met"

    async def test_deleting_too_early_is_refused(self, ctx) -> None:
        download_id = await self._watched_download(ctx, seeded_seconds=86400)
        result = await janitor.remove_download(
            ctx, download_id, delete_files=True, respect_seed_goal=True
        )
        assert result["ok"] is False
        assert result["seed_blocked"] is True
        assert ctx.qbt.deleted == []

    async def test_unavailable_client_does_not_claim_the_seed_goal_is_met(self, ctx) -> None:
        download_id = await self._watched_download(ctx, seeded_seconds=86400)

        class UnavailableQbt(FakeQbt):
            async def torrents(self, **kwargs):
                raise ConnectionError("offline")

            async def torrents_by_hash(self, hashes):
                raise ConnectionError("offline")

        ctx.qbt = UnavailableQbt()
        candidate = (await janitor.cleanup_candidates(ctx))[0]
        assert candidate["seed_satisfied"] is False
        assert candidate["seed_reason"] == "seeding state unavailable"

        result = await janitor.remove_download(
            ctx, download_id, delete_files=True, respect_seed_goal=True
        )
        assert result["seed_blocked"] is True
        assert (await ctx.repos.downloads.get(download_id))["archived"] == 0

    async def test_unavailable_client_cannot_archive_an_active_torrent(self, ctx) -> None:
        download_id = await self._watched_download(ctx, seeded_seconds=6 * 86400)
        ctx.qbt = None

        result = await janitor.remove_download(ctx, download_id, delete_files=True)

        assert result["client_unavailable"] is True
        assert (await ctx.repos.downloads.get(download_id))["archived"] == 0

    async def test_stale_hash_does_not_claim_files_were_deleted(self, ctx) -> None:
        download_id = await self._watched_download(ctx, seeded_seconds=6 * 86400)
        ctx.qbt.torrents_list.clear()

        result = await janitor.remove_download(ctx, download_id, delete_files=True)

        assert result == {"ok": True, "client_removed": False}
        assert ctx.qbt.deleted == []
        events = await ctx.repos.events.recent(download_id=download_id)
        assert events[0]["data"]["freed_bytes"] == 0

    async def test_tagged_torrent_without_stored_hash_obeys_seed_goal(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="movie", tmdb_id="27205", title="Inception"
        )
        download_id = await ctx.repos.downloads.create(
            media_id=media_id, display_title="Inception", indexer="F", indexer_id="1",
            size_bytes=GIB, state=DownloadState.COMPLETED,
        )
        await ctx.repos.downloads.set_watched(download_id, True)
        torrent = TorrentStatus(
            info_hash="b" * 40, name="Inception", state="stalledUP", progress=1.0,
            eta_seconds=0, dlspeed=0, size_bytes=GIB, save_path="D:\\Torrents",
            content_path="", tags=[f"{ctx.config.policy.torrent_tag_prefix}_{download_id}"],
            seeding_time=86400,
        )
        ctx.qbt.torrents_list.append(torrent)

        candidate = (await janitor.cleanup_candidates(ctx))[0]
        assert candidate["in_client"] is True
        assert candidate["seed_satisfied"] is False
        result = await janitor.remove_download(
            ctx, download_id, delete_files=True, respect_seed_goal=True
        )
        assert result["seed_blocked"] is True

        torrent.seeding_time = 6 * 86400
        result = await janitor.remove_download(
            ctx, download_id, delete_files=True, respect_seed_goal=True
        )
        assert result["ok"] is True
        assert ctx.qbt.deleted == [(["b" * 40], True)]

    async def test_deleting_after_the_goal_succeeds(self, ctx) -> None:
        download_id = await self._watched_download(ctx, seeded_seconds=6 * 86400)
        result = await janitor.remove_download(
            ctx, download_id, delete_files=True, respect_seed_goal=True
        )
        assert result["ok"] is True
        assert ctx.qbt.deleted == [(["a" * 40], True)]

    async def test_the_guard_can_be_overridden_deliberately(self, ctx) -> None:
        ctx.config.policy.allow_delete_before_seed_goal = True
        download_id = await self._watched_download(ctx, seeded_seconds=60)
        result = await janitor.remove_download(
            ctx, download_id, delete_files=True, respect_seed_goal=True
        )
        assert result["ok"] is True

    async def test_a_ratio_goal_can_satisfy_instead_of_time(self, ctx) -> None:
        ctx.config.policy.min_seed_ratio = 1.0
        await self._watched_download(ctx, seeded_seconds=60)  # ratio is 1.5
        candidate = (await janitor.cleanup_candidates(ctx))[0]
        assert candidate["seed_satisfied"] is True
        assert "ratio" in candidate["seed_reason"]

    async def test_a_torrent_gone_from_the_client_has_nothing_left_to_seed(self, ctx) -> None:
        download_id = await self._watched_download(ctx, seeded_seconds=0)
        ctx.qbt.torrents_list.clear()
        candidate = (await janitor.cleanup_candidates(ctx))[0]
        assert candidate["seed_satisfied"] is True
        assert candidate["in_client"] is False
        result = await janitor.remove_download(
            ctx, download_id, delete_files=True, respect_seed_goal=True
        )
        assert result["ok"] is True
        assert result["client_removed"] is False
        events = await ctx.repos.events.recent(download_id=download_id)
        assert events[0]["data"]["freed_bytes"] == 0


class TestStateSnapshot:
    async def test_pending_items_are_grouped_by_title(self, ctx) -> None:
        media_id = await ctx.repos.media.upsert(
            media_type="show", tmdb_id="125988", title="Silo"
        )
        for season in (1, 2):
            await ctx.repos.downloads.create(
                media_id=media_id, display_title=f"Silo (Season {season})", indexer="F",
                indexer_id=str(season), size_bytes=88 * GIB, season=season,
                is_season_pack=True,
            )
        snapshot = await state.build_state(ctx)
        assert snapshot["summary"]["pending_approval"] == 2
        assert len(snapshot["pending_groups"]) == 1
        assert snapshot["pending_groups"][0]["count"] == 2
        assert len(snapshot["pending_groups"][0]["ids"]) == 2
