"""Queue dispatcher: hand approved grabs to qBittorrent.

The important move here is fetching the .torrent ourselves first. That gives us
the info-hash and the true payload size *before* anything is added, so we can:

* check the real size against free space rather than the tracker's figure;
* notice the torrent is already in the client and adopt it instead of
  duplicating it;
* track it afterwards by hash, not by guessing from its name.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..clients.qbittorrent import BUSY_STATES
from ..domain.models import DownloadState, EventLevel, Release
from ..logs import get_logger
from ..util import bencode
from ..util.text import human_size
from . import storage
from .context import Conduit

log = get_logger("queue")

# How hard to look for a torrent we have just handed over.
CONFIRM_ATTEMPTS = 6
CONFIRM_DELAY_SECONDS = 1.5


async def dispatch_queue(ctx: Conduit) -> dict[str, int]:
    async with ctx.queue_dispatch_lock:
        return await _dispatch_queue(ctx)


async def _dispatch_queue(ctx: Conduit) -> dict[str, int]:
    policy = ctx.config.policy
    queued = await ctx.repos.downloads.queued()
    if not queued:
        return {"sent": 0}

    if policy.dry_run:
        log.info("dry run: %d items would be sent", len(queued))
        return {"sent": 0, "dry_run": len(queued)}

    if ctx.qbt is None:
        return {"sent": 0}

    await ctx.qbt.login()
    await ctx.qbt.ensure_category(policy.torrent_category)

    # One listing answers both questions -- how busy the client is, and what it
    # already holds. Asking twice doubled the work on every dispatch tick.
    torrents = await ctx.qbt.torrents()
    active = sum(1 for t in torrents if t.state in BUSY_STATES)
    slots = policy.max_active_downloads - active
    if slots <= 0:
        log.debug("client is full", extra={"active": active,
                                           "limit": policy.max_active_downloads})
        return {"sent": 0, "active": active}

    # Force a fresh look: we are about to commit tens of gigabytes against it.
    drives = await storage.survey(ctx.settings.download_dirs, force=True)
    existing_hashes = {t.info_hash for t in torrents if t.info_hash}
    sent = 0

    for row in queued:
        if slots <= 0:
            break
        try:
            if await _dispatch_one(ctx, row, drives, existing_hashes):
                sent += 1
                slots -= 1
        except Exception as exc:
            log.exception("dispatch failed", extra={"title": row["display_title"]})
            await ctx.repos.downloads.set_state(
                int(row["id"]), DownloadState.FAILED, error=str(exc)[:500]
            )
            await ctx.record(
                "queue",
                f"Could not start {row['display_title']}: {exc}",
                level=EventLevel.ERROR,
                download_id=int(row["id"]),
            )

    if sent:
        ctx.bus.publish("queue.dispatched", sent=sent)
    return {"sent": sent, "active": active}


async def _dispatch_one(
    ctx: Conduit, row: dict[str, Any], drives: list[storage.DriveInfo],
    existing_hashes: set[str],
) -> bool:
    policy = ctx.config.policy
    download_id = int(row["id"])
    title = row["display_title"]

    content, info_hash, size_bytes = await _fetch_torrent(ctx, row)
    if info_hash and info_hash in existing_hashes:
        # Already in the client (added manually, or left over from a crash).
        await ctx.repos.downloads.set_verified_size(download_id, size_bytes)
        await ctx.repos.downloads.set_hash(download_id, info_hash)
        await ctx.repos.downloads.set_state(download_id, DownloadState.DOWNLOADING)
        await ctx.record(
            "queue",
            f"Adopted {title} — already present in qBittorrent",
            download_id=download_id,
        )
        return True

    reported_size = float(row["size_bytes"] or 0)
    auto_approved = (
        policy.auto_approve_below_gb > 0
        and size_bytes < policy.auto_approve_below_gb * 1024**3
    )
    needs_new_approval = (
        reported_size <= policy.approval_size_bytes < size_bytes
        and not auto_approved
    )
    await ctx.repos.downloads.set_verified_size(
        download_id, size_bytes, require_approval=needs_new_approval
    )
    if needs_new_approval:
        await ctx.record(
            "approval",
            f"Needs approval: {title} — verified size is {human_size(size_bytes)}",
            level=EventLevel.WARNING,
            download_id=download_id,
        )
        ctx.bus.publish("download.updated", download_id=download_id)
        return False

    drive = storage.choose(
        drives,
        size_bytes,
        reserve_gb=policy.reserve_free_space_gb,
        headroom_percent=policy.size_headroom_percent,
    )
    if drive is None:
        if row["state"] != DownloadState.NO_SPACE:
            reason = storage.format_shortfall(drives, size_bytes)
            await ctx.repos.downloads.set_state(
                download_id, DownloadState.NO_SPACE, error=reason
            )
            await ctx.record(
                "queue",
                f"No room for {title} — {reason}",
                level=EventLevel.WARNING,
                download_id=download_id,
            )
        return False

    own_tag = f"{policy.torrent_tag_prefix}_{download_id}"
    tags = f"{policy.torrent_tag_prefix},{own_tag}"
    await ctx.qbt.add_torrent_file(
        content,
        filename=f"conduit-{download_id}.torrent",
        save_path=drive.path,
        category=policy.torrent_category,
        tags=tags,
    )

    # qBittorrent answers "Ok." when it *accepts* the request, not when the
    # torrent exists. Without this check the
    # download is reported as started, marked downloading, and then reappears
    # moments later as "disappeared from qBittorrent".
    landed = await _confirm_added(ctx, info_hash=info_hash, tag=own_tag)
    if landed is None:
        reason = "qBittorrent accepted the file but the torrent never appeared."
        await ctx.repos.downloads.set_state(
            download_id, DownloadState.FAILED, error=reason
        )
        await ctx.record(
            "queue", f"Could not start {title} — {reason}",
            level=EventLevel.ERROR, media_id=row.get("media_id"),
            download_id=download_id,
        )
        return False

    info_hash = info_hash or landed
    if info_hash:
        await ctx.repos.downloads.set_hash(download_id, info_hash)
        existing_hashes.add(info_hash)

    await ctx.repos.downloads.set_state(
        download_id, DownloadState.DOWNLOADING, save_path=drive.path
    )
    drive.free_bytes = max(0, drive.free_bytes - int(size_bytes))

    await ctx.record(
        "queue",
        f"Started {title} → {drive.label} ({human_size(size_bytes)})",
        level=EventLevel.SUCCESS,
        media_id=row.get("media_id"),
        download_id=download_id,
        data={"drive": drive.path, "info_hash": info_hash, "size_bytes": size_bytes},
    )
    log.info("sent to client", extra={"title": title, "drive": drive.path,
                                      "size": human_size(size_bytes)})
    return True


async def _confirm_added(
    ctx: Conduit, *, info_hash: str | None, tag: str
) -> str | None:
    """Wait briefly for a just-added torrent to actually exist in the client.

    Returns its info-hash, or ``None`` if it never turned up. Adding can be
    asynchronous, so this polls rather than asking once.
    """
    for attempt in range(CONFIRM_ATTEMPTS):
        if attempt:
            await asyncio.sleep(CONFIRM_DELAY_SECONDS)
        try:
            if info_hash:
                found = await ctx.qbt.torrents_by_hash([info_hash])
                if info_hash in found:
                    return info_hash
            # The hash may not have registered yet: our per-download tag is
            # another way to confirm the add.
            for torrent in await ctx.qbt.torrents(tag=tag):
                if tag in torrent.tags:
                    return torrent.info_hash
        except Exception as exc:
            log.debug("could not confirm the add yet", extra={"err": str(exc)})
    return None


async def _fetch_torrent(
    ctx: Conduit, row: dict[str, Any]
) -> tuple[bytes, str, float]:
    """Download the .torrent and read its true identity and size."""
    url = row.get("download_url")
    if not url:
        raise RuntimeError("no torrent download URL")

    release = Release(
        indexer=row.get("indexer") or "",
        indexer_id=str(row.get("indexer_id") or ""),
        name=row.get("release_name") or row["display_title"],
        size_bytes=int(row.get("size_bytes") or 0),
        download_url=url,
    )
    try:
        content = await ctx.indexers.fetch_torrent(release)
    except Exception as exc:
        raise RuntimeError("could not fetch .torrent; retry when the tracker is available") from exc

    if not content:
        raise RuntimeError("tracker did not return a .torrent file")

    try:
        summary = bencode.torrent_summary(content)
    except bencode.BencodeError as exc:
        raise RuntimeError(f"tracker returned a malformed torrent file: {exc}") from exc

    return content, str(summary["info_hash"]), float(summary["size_bytes"])


async def retry_download(ctx: Conduit, download_id: int) -> bool:
    """Push a failed or space-blocked item back into the queue."""
    async with ctx.download_decision_lock:
        row = await ctx.repos.downloads.get(download_id)
        if not row or row["archived"] or row["state"] not in (
            DownloadState.FAILED, DownloadState.NO_SPACE, DownloadState.CANCELLED
        ):
            return False
        if (row["state"] == DownloadState.CANCELLED
                and not await ctx.repos.wanted.reclaim_for_retry(
                    download_id, int(row["wanted_count"]),
                    int(row["wanted_id"])
                    if row["wanted_id"] and not row["is_season_pack"]
                    and (row["episode_to"] is None
                         or row["episode_to"] == row["episode_from"])
                    else None,
                )):
            return False
        if (row["state"] != DownloadState.CANCELLED and row["wanted_count"]
                and not await ctx.repos.wanted.still_claimed_for_download(
                    download_id, int(row["wanted_count"])
                )):
            return False
        await ctx.repos.downloads.set_state(download_id, DownloadState.QUEUED, error=None)
    await ctx.record("queue", f"Retrying {row['display_title']}", download_id=download_id)
    return True
