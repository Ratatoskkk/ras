"""REST API.

Everything the dashboard can do is here, and every route is a thin adapter
over a service function -- no business logic, no SQL.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query, Request
from pydantic import BaseModel

from ..__init__ import __version__
from ..config import AppConfig
from ..domain.models import WantedState
from ..logs import ring_buffer
from ..services import calendar, janitor, library, queue, search, state, watchlist
from ..services.context import Conduit
from ..services.supervisor import Supervisor

router = APIRouter(prefix="/api")


def ctx_of(request: Request) -> Conduit:
    return request.app.state.conduit


def supervisor_of(request: Request) -> Supervisor:
    return request.app.state.supervisor


# ---------------------------------------------------------------------------
# State + health
# ---------------------------------------------------------------------------
@router.get("/state")
async def get_state(request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    snapshot = await state.build_state(ctx)
    snapshot["tasks"] = supervisor_of(request).status()
    snapshot["version"] = __version__
    return snapshot


@router.get("/health")
async def get_health(request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    health = await ctx.health()
    degraded = [
        name
        for name, value in (
            ("qbittorrent", health["qbittorrent"].get("state")),
            ("tmdb", health["tmdb"].get("state")),
        )
        if value == "open"
    ]
    degraded += [i["name"] for i in health["indexers"] if i.get("state") == "open"]
    degraded += [p["name"] for p in health["plex"] if p.get("state") == "open"]
    return {
        "status": "degraded" if degraded else "ok",
        "version": __version__,
        "degraded": degraded,
        "checks": health,
    }


@router.get("/accounts")
async def get_accounts(request: Request, refresh: bool = False) -> list[dict[str, Any]]:
    """Ratio, buffer and hit-and-run standing on every tracker.

    Cached for ten minutes: the numbers move slowly and the dashboard asks for
    them on every visit.
    """
    ctx = ctx_of(request)
    if not refresh:
        cached = await ctx.repos.cache.get("indexer:accounts")
        if cached is not None:
            return cached
    try:
        # Hard ceiling regardless of what the clients do. Nothing on a page a
        # human is looking at gets to hang on a third party.
        accounts = await asyncio.wait_for(ctx.indexers.accounts(), timeout=8.0)
    except Exception:
        accounts = []
    # Cache the failure too, briefly, so an unreachable tracker is not
    # re-dialled on every single dashboard visit.
    await ctx.repos.cache.set("indexer:accounts", accounts, 600 if accounts else 120)
    return accounts


@router.get("/drives")
async def get_drives(request: Request) -> list[dict[str, Any]]:
    from ..services import storage

    ctx = ctx_of(request)
    return [d.as_dict() for d in await storage.survey(ctx.settings.download_dirs)]


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------
@router.get("/events")
async def get_events(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    since_id: int = 0,
    category: str | None = None,
    media_id: int | None = None,
) -> list[dict[str, Any]]:
    return await ctx_of(request).repos.events.recent(
        limit=limit, since_id=since_id, category=category, media_id=media_id
    )


@router.get("/logs")
async def get_logs(
    limit: int = Query(200, ge=1, le=1000),
    level: str | None = None,
    since_seq: int = 0,
) -> list[dict[str, Any]]:
    return ring_buffer.snapshot(limit=limit, level=level, since_seq=since_seq)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
@router.get("/tasks")
async def get_tasks(request: Request) -> list[dict[str, Any]]:
    return supervisor_of(request).status()


@router.post("/tasks/{name}/run")
async def run_task(request: Request, name: str) -> dict[str, Any]:
    if not supervisor_of(request).trigger(name):
        raise HTTPException(404, f"No such task: {name}")
    return {"ok": True, "task": name}


@router.post("/tasks/{name}/enabled")
async def toggle_task(request: Request, name: str, enabled: bool = Body(embed=True)) -> dict:
    if not supervisor_of(request).set_enabled(name, enabled):
        raise HTTPException(404, f"No such task: {name}")
    return {"ok": True, "task": name, "enabled": enabled}


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------
class IdList(BaseModel):
    ids: list[int]


@router.get("/downloads/{download_id}")
async def get_download(request: Request, download_id: int) -> dict[str, Any]:
    ctx = ctx_of(request)
    row = await ctx.repos.downloads.get(download_id)
    if not row:
        raise HTTPException(404, "Download not found")
    return {
        "download": row,
        # Filtered in SQL: reading the last 50 events of *any* kind and sifting
        # them here usually returned nothing for an older download.
        "events": await ctx.repos.events.recent(limit=50, download_id=download_id),
    }


@router.post("/downloads/approve")
async def approve_downloads(request: Request, payload: IdList) -> dict[str, Any]:
    ctx = ctx_of(request)
    count = await ctx.repos.downloads.approve_many(payload.ids)
    if count:
        await ctx.record("approval", f"Approved {count} download(s)")
        supervisor_of(request).trigger("queue-dispatch")
    return {"ok": True, "approved": count}


@router.post("/downloads/deny")
async def deny_downloads(request: Request, payload: IdList) -> dict[str, Any]:
    ctx = ctx_of(request)
    rows = await ctx.repos.downloads.deny_many(payload.ids)
    count = len(rows)
    # Denied releases go on the blocklist so the next search does not re-offer them.
    for row in rows:
        if row.get("indexer_id"):
            await ctx.repos.blocklist.add(
                row["indexer"], str(row["indexer_id"]),
                title=row["display_title"], reason="denied from dashboard",
            )
    if count:
        await ctx.record("approval", f"Denied {count} download(s)")
    return {"ok": True, "denied": count}


@router.post("/downloads/{download_id}/retry")
async def retry(request: Request, download_id: int) -> dict[str, Any]:
    ctx = ctx_of(request)
    if not await queue.retry_download(ctx, download_id):
        raise HTTPException(400, "That download is not in a retryable state")
    supervisor_of(request).trigger("queue-dispatch")
    return {"ok": True}


@router.delete("/downloads/{download_id}")
async def remove_download(
    request: Request, download_id: int, delete_files: bool = True,
    respect_seed_goal: bool = False,
) -> dict[str, Any]:
    result = await janitor.remove_download(
        ctx_of(request), download_id, delete_files=delete_files,
        respect_seed_goal=respect_seed_goal,
    )
    if not result.get("ok"):
        if result.get("seed_blocked"):
            raise HTTPException(409, result["error"])
        if result.get("client_unavailable"):
            raise HTTPException(503, result["error"])
        raise HTTPException(404, result.get("error", "not found"))
    return result


@router.get("/history")
async def get_history(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    offset: int = 0,
    q: str = "",
) -> list[dict[str, Any]]:
    return await ctx_of(request).repos.downloads.history(limit=limit, offset=offset, query=q)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
@router.get("/cleanup")
async def get_cleanup(request: Request) -> list[dict[str, Any]]:
    return await janitor.cleanup_candidates(ctx_of(request))


@router.post("/cleanup/scan")
async def scan_cleanup(request: Request) -> dict[str, Any]:
    supervisor_of(request).trigger("library-index")
    supervisor_of(request).trigger("watched-sync")
    return {"ok": True, "message": "Library and watched-state scan queued."}


# ---------------------------------------------------------------------------
# Media
# ---------------------------------------------------------------------------
class MediaPatch(BaseModel):
    monitored: bool | None = None
    ignored: bool | None = None
    profile: str | None = None


@router.get("/media")
async def list_media(
    request: Request, media_type: str | None = None, monitored: bool = False
) -> list[dict[str, Any]]:
    ctx = ctx_of(request)
    rows = await ctx.repos.media.list_all(media_type=media_type, monitored_only=monitored)
    progress = await ctx.repos.library.show_progress()
    wanted = await ctx.repos.wanted.counts_by_media()
    for row in rows:
        info = progress.get(str(row.get("tmdb_id") or ""), {})
        row["library_episodes"] = info.get("episodes", 0)
        row["library_watched"] = info.get("watched", 0)
        counts = wanted.get(int(row["id"]), {})
        row["wanted_outstanding"] = counts.get("outstanding", 0)
        row["wanted_seen"] = counts.get("seen", 0)
        row["wanted_have"] = counts.get("have", 0)
    return rows


@router.get("/media/{media_id}")
async def get_media(request: Request, media_id: int) -> dict[str, Any]:
    ctx = ctx_of(request)
    row = await ctx.repos.media.get(media_id)
    if not row:
        raise HTTPException(404, "Not found")
    return {
        "media": row,
        "wanted": await ctx.repos.wanted.for_media(media_id),
        "events": await ctx.repos.events.recent(limit=50, media_id=media_id),
    }


@router.patch("/media/{media_id}")
async def patch_media(request: Request, media_id: int, payload: MediaPatch) -> dict[str, Any]:
    ctx = ctx_of(request)
    row = await ctx.repos.media.get(media_id)
    if not row:
        raise HTTPException(404, "Not found")
    await ctx.repos.media.set_flags(
        media_id, monitored=payload.monitored, ignored=payload.ignored,
        profile=payload.profile,
    )
    if payload.ignored:
        await ctx.repos.wanted.set_state_for_media(
            media_id, WantedState.IGNORED,
            only_states=(WantedState.WAITING, WantedState.SEARCHING),
        )
        await ctx.record("monitor", f"Stopped following {row['title']}", media_id=media_id)
    elif payload.ignored is False or payload.monitored:
        await ctx.repos.wanted.set_state_for_media(
            media_id, WantedState.SEARCHING, only_states=(WantedState.IGNORED,)
        )
        await ctx.record("monitor", f"Following {row['title']} again", media_id=media_id)
    return {"ok": True}


@router.delete("/media/{media_id}")
async def delete_media(request: Request, media_id: int) -> dict[str, Any]:
    ctx = ctx_of(request)
    await ctx.repos.media.delete(media_id)
    return {"ok": True}


@router.post("/media/{media_id}/search")
async def search_media(request: Request, media_id: int) -> dict[str, Any]:
    return await search.search_media_now(ctx_of(request), media_id)


@router.get("/media/{media_id}/preview")
async def preview_media(
    request: Request, media_id: int, season: int | None = None
) -> dict[str, Any]:
    """Rank what the trackers currently offer, without grabbing anything."""
    return await search.preview_media(ctx_of(request), media_id, season)


@router.post("/media/{media_id}/refresh")
async def refresh_media(request: Request, media_id: int) -> dict[str, Any]:
    ctx = ctx_of(request)
    row = await ctx.repos.media.get(media_id)
    if not row:
        raise HTTPException(404, "Not found")
    created = await calendar.refresh_media(ctx, row)
    return {"ok": True, "wanted": created}


@router.get("/upcoming")
async def get_upcoming(request: Request) -> list[dict[str, Any]]:
    rows = await ctx_of(request).repos.wanted.upcoming(limit=500)
    return [state._upcoming(row) for row in rows]


@router.post("/wanted/{wanted_id}/state")
async def set_wanted_state(
    request: Request, wanted_id: int, value: str = Body(embed=True)
) -> dict[str, Any]:
    ctx = ctx_of(request)
    await ctx.repos.wanted.set_state(wanted_id, value)
    return {"ok": True}


class WatchedPatch(BaseModel):
    ids: list[int] = []
    watched: bool = True


@router.post("/wanted/watched")
async def mark_wanted_watched(request: Request, payload: WatchedPatch) -> dict[str, Any]:
    """Mark specific episodes as already seen -- or undo it.

    Scoped to the episode: the series stays followed and future releases are
    unaffected, which is what makes this different from ignoring a title.
    """
    ctx = ctx_of(request)
    changed = await ctx.repos.wanted.mark_watched(payload.ids, watched=payload.watched)
    if changed:
        verb = "marked as seen" if payload.watched else "put back in the queue"
        await ctx.record("monitor", f"{changed} episode(s) {verb}")
        ctx.bus.publish("calendar.refreshed", changed=changed)
    return {"ok": True, "changed": changed}


class MediaWatchedPatch(BaseModel):
    season: int | None = None
    up_to_episode: int | None = None


@router.post("/media/{media_id}/watched")
async def mark_media_watched(
    request: Request, media_id: int, payload: MediaWatchedPatch
) -> dict[str, Any]:
    """Mark a whole title, one season, or everything up to an episode as seen."""
    ctx = ctx_of(request)
    row = await ctx.repos.media.get(media_id)
    if not row:
        raise HTTPException(404, "Not found")
    changed = await ctx.repos.wanted.mark_watched_for_media(
        media_id, season=payload.season, up_to_episode=payload.up_to_episode
    )
    scope = "everything outstanding"
    if payload.season is not None:
        scope = f"season {payload.season}"
        if payload.up_to_episode is not None:
            scope += f" up to episode {payload.up_to_episode}"
    await ctx.record(
        "monitor",
        f"{row['title']}: {scope} marked as already seen ({changed} episode(s))",
        media_id=media_id,
    )
    ctx.bus.publish("calendar.refreshed", changed=changed)
    return {"ok": True, "changed": changed}


# ---------------------------------------------------------------------------
# Blocklist
# ---------------------------------------------------------------------------
@router.get("/blocklist")
async def get_blocklist(request: Request) -> list[dict[str, Any]]:
    return await ctx_of(request).repos.blocklist.list_all()


@router.delete("/blocklist/{entry_id}")
async def delete_blocklist(request: Request, entry_id: int) -> dict[str, Any]:
    await ctx_of(request).repos.blocklist.remove(entry_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@router.get("/config")
async def get_config(request: Request) -> dict[str, Any]:
    ctx = ctx_of(request)
    return {
        "config": ctx.config.model_dump(mode="json"),
        "paths": {
            "config_file": str(ctx.config_store.path),
            "database": str(ctx.settings.database_path),
            "download_dirs": [str(p) for p in ctx.settings.download_dirs],
        },
        "auth_mode": ctx.settings.conduit_auth_mode,
    }


@router.put("/config")
async def put_config(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist a whole config document.

    Validation happens before anything touches disk, so a bad edit from the UI
    is rejected with a readable error rather than leaving a broken TOML file.
    """
    ctx = ctx_of(request)
    try:
        new_config = AppConfig.model_validate(payload)
    except Exception as exc:
        raise HTTPException(422, f"Invalid configuration: {exc}") from exc
    ctx.config_store.replace(new_config)
    ctx.rebuild_indexers()
    await ctx.record("config", "Configuration updated from the dashboard")
    ctx.bus.publish("config.reloaded")
    return {"ok": True, "config": new_config.model_dump(mode="json")}


# ---------------------------------------------------------------------------
# Manual triggers
# ---------------------------------------------------------------------------
@router.post("/actions/sync-watchlist")
async def action_sync_watchlist(request: Request) -> dict[str, Any]:
    return await watchlist.sync_watchlist(ctx_of(request))


@router.post("/actions/index-library")
async def action_index_library(request: Request) -> dict[str, Any]:
    return await library.index_library(ctx_of(request))


@router.post("/actions/clear-ignored")
async def action_clear_ignored(request: Request) -> dict[str, Any]:
    """Un-ignore every title and bring its wants back into play."""
    ctx = ctx_of(request)
    titles = await ctx.repos.media.clear_ignored()
    wants = await ctx.repos.wanted.clear_ignored()
    await ctx.record(
        "monitor", f"Ignore list cleared: {titles} title(s), {wants} want(s) restored"
    )
    ctx.bus.publish("library.indexed", cleared=titles)
    return {"ok": True, "titles": titles, "wants": wants}


@router.post("/actions/follow-watched")
async def action_follow_watched(request: Request) -> dict[str, Any]:
    added = await library.track_watched_shows(ctx_of(request))
    return {"ok": True, "added": added}


@router.post("/actions/refresh-calendar")
async def action_refresh_calendar(request: Request) -> dict[str, Any]:
    return await calendar.refresh_calendar(ctx_of(request))


@router.post("/actions/search-now")
async def action_search_now(request: Request, fresh: bool = False) -> dict[str, Any]:
    return await search.run_search(ctx_of(request), fresh_only=fresh)


@router.post("/actions/dispatch-queue")
async def action_dispatch(request: Request) -> dict[str, Any]:
    return await queue.dispatch_queue(ctx_of(request))


@router.post("/actions/clear-history")
async def action_clear_history(request: Request) -> dict[str, Any]:
    """Drop denied, failed and cancelled rows. Completed history is kept."""
    # One statement, and the count is what was actually deleted -- the previous
    # version paged through history, deleted a subset, and reported the page
    # size (which included the completed rows it had deliberately kept).
    removed = await ctx_of(request).repos.downloads.purge_history()
    return {"ok": True, "removed": removed}
