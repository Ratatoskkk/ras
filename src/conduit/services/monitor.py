"""Download monitor: track progress, detect completion, react.

Torrents are matched by info-hash first and by Conduit's own tag second. Only
if both fail does it consider a download lost -- which is why a torrent renamed
in the client, or one whose release name never resembled the Plex title, stays
correctly tracked here.
"""

from __future__ import annotations

from typing import Any

from ..domain.models import DownloadState, EventLevel, Release, TorrentStatus, WantedState
from ..domain.parser import parse_release
from ..logs import get_logger
from ..util.text import human_size
from .context import Conduit

log = get_logger("monitor")

# How long a download may be absent from the client before we conclude it is
# really gone. qBittorrent fetches URL-added torrents in the background and
# serves an empty list for a moment after a restart, so acting on a single
# missed sighting turns an ordinary delay into a cancelled download.
MISSING_GRACE_SECONDS = 300


async def monitor_downloads(ctx: Conduit) -> dict[str, int]:
    rows = await ctx.repos.downloads.in_flight()
    if not rows or ctx.qbt is None:
        return {"tracked": 0}

    torrents = await ctx.qbt.torrents()
    by_hash = {t.info_hash: t for t in torrents if t.info_hash}
    by_tag = _index_by_tag(torrents, ctx.config.policy.torrent_tag_prefix)

    progress_batch: list[tuple[float, int, float, str, int]] = []
    live: dict[str, dict[str, Any]] = {}
    completed = 0
    lost = 0

    for row in rows:
        download_id = int(row["id"])
        torrent = _locate(row, by_hash, by_tag, ctx.config.policy.torrent_tag_prefix)

        if torrent is None:
            lost += 1
            # Start (or continue) the clock rather than giving up now.
            await ctx.repos.downloads.mark_missing(download_id)
            continue

        if row.get("missing_since"):
            # It came back. A restart or a slow URL add, not a deletion.
            await ctx.repos.downloads.clear_missing(download_id)
        if not row.get("info_hash") and torrent.info_hash:
            await ctx.repos.downloads.set_hash(download_id, torrent.info_hash)

        if torrent.is_errored:
            await ctx.repos.downloads.set_state(
                download_id, DownloadState.FAILED,
                error=f"qBittorrent reports state '{torrent.state}'",
            )
            await ctx.record(
                "download",
                f"{row['display_title']} failed in the client ({torrent.state})",
                level=EventLevel.ERROR,
                download_id=download_id,
            )
            continue

        if torrent.is_complete:
            await _handle_complete(ctx, row, torrent)
            completed += 1
            continue

        progress_batch.append(
            (
                round(torrent.progress, 5),
                torrent.eta_seconds,
                torrent.dlspeed,
                torrent.content_path or None,
                download_id,
            )
        )
        live[str(download_id)] = {
            "progress": round(torrent.progress, 5),
            "eta_seconds": torrent.eta_seconds,
            "speed_bps": torrent.dlspeed,
            "state": torrent.state,
        }

    if progress_batch:
        await ctx.repos.downloads.update_progress(progress_batch)
    if live:
        ctx.bus.publish("download.progress", downloads=live)

    abandoned = 0
    for row in await ctx.repos.downloads.missing_beyond(MISSING_GRACE_SECONDS):
        await _handle_missing(ctx, row)
        abandoned += 1

    return {"tracked": len(rows), "completed": completed, "lost": lost,
            "abandoned": abandoned}


def _index_by_tag(torrents: list[TorrentStatus], prefix: str) -> dict[int, TorrentStatus]:
    indexed: dict[int, TorrentStatus] = {}
    marker = f"{prefix}_"
    for torrent in torrents:
        for tag in torrent.tags:
            if tag.startswith(marker):
                try:
                    indexed[int(tag[len(marker):])] = torrent
                except ValueError:
                    continue
    return indexed


def _locate(
    row: dict[str, Any], by_hash: dict[str, TorrentStatus],
    by_tag: dict[int, TorrentStatus], prefix: str,
) -> TorrentStatus | None:
    info_hash = (row.get("info_hash") or "").lower()
    if info_hash and info_hash in by_hash:
        return by_hash[info_hash]
    return by_tag.get(int(row["id"]))


async def _handle_missing(ctx: Conduit, row: dict[str, Any]) -> None:
    """A tracked torrent has been absent long enough to call it gone.

    Usually a manual delete. Reached only after ``MISSING_GRACE_SECONDS``, so
    a slow URL add or a client restart never lands here.
    """
    download_id = int(row["id"])
    minutes = MISSING_GRACE_SECONDS // 60
    await ctx.repos.downloads.set_state(
        download_id, DownloadState.CANCELLED,
        error=f"not in qBittorrent for over {minutes} minutes",
    )
    await ctx.repos.wanted.reopen_for_download(
        download_id, int(row["wanted_id"]) if row.get("wanted_id") else None
    )
    await ctx.record(
        "download",
        f"{row['display_title']} has been missing from qBittorrent for over "
        f"{minutes} minutes — back to searching",
        level=EventLevel.WARNING,
        media_id=row.get("media_id"),
        download_id=download_id,
    )


async def _handle_complete(ctx: Conduit, row: dict[str, Any], torrent: TorrentStatus) -> None:
    download_id = int(row["id"])
    title = row["display_title"]

    await ctx.repos.downloads.set_state(
        download_id, DownloadState.COMPLETED, save_path=torrent.save_path or None
    )
    await ctx.repos.downloads.update_progress(
        [(1.0, 0, 0.0, torrent.content_path or None, download_id)]
    )

    media_id = row.get("media_id")
    if media_id:
        season = row.get("season")
        if season is not None:
            if not row.get("wanted_count"):
                if row.get("is_season_pack"):
                    from .janitor import _pack_seasons

                    for covered_season in _pack_seasons(row) or {int(season)}:
                        await ctx.repos.wanted.mark_covered(
                            int(media_id), covered_season, None
                        )
                else:
                    await ctx.repos.wanted.mark_covered(
                        int(media_id), int(season), _episode_span(row)
                    )
        elif row.get("wanted_id"):
            await ctx.repos.wanted.set_state(
                int(row["wanted_id"]), WantedState.DOWNLOADED, reason="download completed"
            )

    await ctx.record(
        "download",
        f"Completed {title} ({human_size(torrent.size_bytes)})",
        level=EventLevel.SUCCESS,
        media_id=media_id,
        download_id=download_id,
        data={"save_path": torrent.save_path, "content_path": torrent.content_path},
    )
    ctx.bus.publish("download.completed", download_id=download_id, title=title)
    log.info("download complete", extra={"title": title,
                                         "size": human_size(torrent.size_bytes)})

    if ctx.config.policy.trigger_plex_refresh and ctx.plex is not None:
        media = await ctx.repos.media.get(int(media_id)) if media_id else None
        media_type = media["media_type"] if media else "movie"
        sections = await ctx.plex.refresh_sections(media_type)
        if sections:
            log.debug("asked Plex to rescan", extra={"sections": ", ".join(sections)})

    if row.get("is_season_pack") and row.get("season") is not None and media_id:
        from .janitor import _pack_seasons, retire_superseded_episodes

        seasons = _pack_seasons(row)
        if seasons:
            await retire_superseded_episodes(ctx, int(media_id), seasons, download_id)


def _episode_span(row: dict[str, Any]) -> list[int] | None:
    """Explicit episode list for a single/multi-episode grab, None for a pack."""
    if row.get("is_season_pack"):
        return None
    release_name = row.get("release_name") or ""
    if release_name:
        parsed = parse_release(Release(
            indexer=row.get("indexer") or "", indexer_id=str(row.get("indexer_id") or ""),
            name=release_name, size_bytes=int(row.get("size_bytes") or 0), download_url="",
        ))
        if parsed.episodes:
            return parsed.episodes
    start, end = row.get("episode_from"), row.get("episode_to")
    if start is None:
        return None
    return list(range(int(start), int(end or start) + 1))
