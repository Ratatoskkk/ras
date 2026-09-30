"""WebSocket endpoint: live state, pushed.

A client connects, receives the full snapshot once, then gets deltas as they
happen. No polling loop, no second SSE connection, and no per-client
recomputation of progress.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..logs import get_logger
from ..services import state
from .security import websocket_allowed

log = get_logger("ws")
router = APIRouter()

# If nothing happens for this long, send a ping so proxies keep the socket open.
IDLE_PING_SECONDS = 25
# Upper bound on how many queued messages are coalesced into one snapshot pass.
_MAX_BATCH = 64


@router.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    ctx = websocket.app.state.conduit
    supervisor = websocket.app.state.supervisor
    host = websocket.client.host if websocket.client else None
    token = websocket.query_params.get("token", "")

    if not websocket_allowed(ctx.settings, host, token):
        await websocket.close(code=4403, reason="Access denied")
        return

    await websocket.accept()
    subscription = ctx.bus.subscribe()

    try:
        snapshot = await state.build_state(ctx)
        snapshot["tasks"] = supervisor.status()
        await websocket.send_json({"topic": "state", "state": snapshot})

        reader = asyncio.create_task(_drain(websocket))
        try:
            while True:
                if not subscription.active:
                    await websocket.close(code=1013, reason="Update stream fell behind")
                    return
                try:
                    message = await asyncio.wait_for(
                        subscription.queue.get(), timeout=IDLE_PING_SECONDS
                    )
                except TimeoutError:
                    if not subscription.active:
                        continue
                    await websocket.send_json({"topic": "ping"})
                    continue

                if not subscription.active:
                    continue

                # A single search pass can publish dozens of events at once.
                # Take everything already queued in one go, so the expensive
                # part -- rebuilding the snapshot -- happens once per burst
                # rather than once per message.
                batch = [message]
                while not subscription.queue.empty() and len(batch) < _MAX_BATCH:
                    batch.append(subscription.queue.get_nowait())

                carrier = snapshot_carrier([m.topic for m in batch])
                snapshot = None
                if carrier is not None:
                    # State-shaped events carry a fresh snapshot so the UI never
                    # has to reconcile partial updates by hand.
                    snapshot = await state.build_state(ctx)
                    snapshot["tasks"] = supervisor.status()

                for index, item in enumerate(batch):
                    payload = item.as_dict()
                    if index == carrier:
                        payload["state"] = snapshot
                    await websocket.send_json(payload)
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.debug("websocket closed", extra={"err": str(exc)})
    finally:
        subscription.close()


def snapshot_carrier(topics: list[str]) -> int | None:
    """Which message of a burst should carry the fresh snapshot.

    The *last* state-shaped one: the client only reads ``state`` off those
    topics, and attaching it to the earlier ones would re-render the dashboard
    once per message for no gain. ``None`` means the burst changed nothing
    worth rebuilding for.
    """
    for index in reversed(range(len(topics))):
        if topics[index] in _RESYNC_TOPICS:
            return index
    return None


_RESYNC_TOPICS = {
    "download.created",
    "download.completed",
    "queue.dispatched",
    "cleanup.removed",
    "cleanup.updated",
    "watchlist.synced",
    "calendar.refreshed",
    "search.finished",
    "config.reloaded",
    "library.indexed",
}


async def _drain(websocket: WebSocket) -> None:
    """Consume client frames so close notifications arrive promptly."""
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        return
