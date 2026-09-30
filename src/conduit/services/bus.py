"""In-process pub/sub used to push live updates to connected dashboards.

The reference project had the browser poll ``/api/state`` every five seconds
*and* hold an SSE connection that re-derived progress independently. Here one
bus feeds every WebSocket, so a change is broadcast once, immediately, and the
UI never polls.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..logs import get_logger

log = get_logger("bus")

QUEUE_LIMIT = 200


@dataclass(slots=True)
class Message:
    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "ts": self.ts, **self.payload}


class Subscription:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.queue: asyncio.Queue[Message] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self.active = True

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._bus.unsubscribe(self)

    async def __aiter__(self) -> AsyncIterator[Message]:
        while True:
            yield await self.queue.get()


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[Subscription] = set()

    def subscribe(self) -> Subscription:
        sub = Subscription(self)
        self._subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        self._subscribers.discard(sub)
        sub.active = False

    @property
    def listener_count(self) -> int:
        return len(self._subscribers)

    def publish(self, topic: str, **payload: Any) -> None:
        """Fire-and-forget broadcast. A slow client is dropped, never blocking."""
        if not self._subscribers:
            return
        message = Message(topic=topic, payload=payload)
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(message)
            except asyncio.QueueFull:
                log.debug("dropping message for a stalled subscriber", extra={"topic": topic})
                self.unsubscribe(sub)
