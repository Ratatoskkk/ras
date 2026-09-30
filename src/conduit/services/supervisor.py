"""Async task supervisor -- replaces APScheduler.

Why not APScheduler: it runs jobs on a thread pool, which forces every async
client back through ``run_coroutine_threadsafe``; it swallows exceptions into a
logger nobody reads; and its intervals are fixed at registration, so changing
one means a restart.

This supervisor runs each task as a plain asyncio task, re-reads its interval
from config on every cycle, applies startup jitter so six jobs do not all fire
at once, records duration and failures per task, and can be triggered on
demand from the API.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..logs import get_logger
from .context import Conduit

log = get_logger("supervisor")

TaskFn = Callable[[Conduit], Awaitable[Any]]
IntervalFn = Callable[[Conduit], int]

# How often a sleeping task re-reads its configured interval. Cheap, and it is
# what makes a cadence change in Settings take effect while you are looking at
# it rather than after the old delay expires.
INTERVAL_RECHECK_SECONDS = 5.0


@dataclass
class TaskSpec:
    name: str
    description: str
    run: TaskFn
    interval: IntervalFn
    run_at_start: bool = True
    start_delay: float = 0.0
    jitter: float = 0.1
    critical: bool = False


@dataclass
class TaskState:
    spec: TaskSpec
    task: asyncio.Task | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    running: bool = False
    enabled: bool = True
    last_start: str | None = None
    last_finish: str | None = None
    last_duration: float = 0.0
    last_error: str | None = None
    run_count: int = 0
    error_count: int = 0
    consecutive_errors: int = 0
    next_run_at: float = 0.0

    def snapshot(self, now: float) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "description": self.spec.description,
            "enabled": self.enabled,
            "running": self.running,
            "last_start": self.last_start,
            "last_finish": self.last_finish,
            "last_duration": round(self.last_duration, 3),
            "last_error": self.last_error,
            "run_count": self.run_count,
            "error_count": self.error_count,
            "seconds_until_next": (
                max(0, round(self.next_run_at - now)) if self.enabled and not self.running else None
            ),
        }


class Supervisor:
    def __init__(self, ctx: Conduit) -> None:
        self.ctx = ctx
        self.states: dict[str, TaskState] = {}
        self._stopping = asyncio.Event()

    def register(self, spec: TaskSpec) -> None:
        self.states[spec.name] = TaskState(spec=spec)

    def register_all(self, specs: list[TaskSpec]) -> None:
        for spec in specs:
            self.register(spec)

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        self._stopping.clear()
        for state in self.states.values():
            state.task = asyncio.create_task(self._loop(state), name=f"conduit:{state.spec.name}")
        log.info("supervisor started", extra={"tasks": len(self.states)})

    async def stop(self, timeout: float = 15.0) -> None:
        self._stopping.set()
        for state in self.states.values():
            state.wake.set()
            if state.task:
                state.task.cancel()
        pending = [s.task for s in self.states.values() if s.task]
        if pending:
            await asyncio.wait(pending, timeout=timeout)
        log.info("supervisor stopped")

    # -- control ------------------------------------------------------------
    def trigger(self, name: str) -> bool:
        """Ask a task to run now instead of waiting out its interval."""
        state = self.states.get(name)
        if not state or not state.enabled:
            return False
        state.next_run_at = time.monotonic()
        state.wake.set()
        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        state = self.states.get(name)
        if not state:
            return False
        state.enabled = enabled
        state.wake.set()
        return True

    def status(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        return [s.snapshot(now) for s in self.states.values()]

    @property
    def busy(self) -> list[str]:
        return [n for n, s in self.states.items() if s.running]

    # -- internals ----------------------------------------------------------
    async def _loop(self, state: TaskState) -> None:
        spec = state.spec
        if spec.start_delay:
            await self._sleep(state, spec.start_delay)
        if not spec.run_at_start:
            await self._sleep(state, self._interval(spec))

        while not self._stopping.is_set():
            if not state.enabled:
                await self._sleep(state, 5.0)
                continue

            await self._run_once(state)

            interval = self._delay_for(state)
            jitter = interval * spec.jitter
            delay = max(5.0, interval + random.uniform(-jitter, jitter))
            state.next_run_at = time.monotonic() + delay
            await self._sleep(state, delay)

    async def _run_once(self, state: TaskState) -> None:
        spec = state.spec
        state.running = True
        state.last_start = datetime.now(UTC).isoformat()
        state.run_count += 1
        started = time.perf_counter()
        error: str | None = None

        self.ctx.bus.publish("task.start", task=spec.name)
        with contextlib.suppress(Exception):
            await self.ctx.repos.tasks.start(spec.name)

        try:
            await spec.run(self.ctx)
            state.consecutive_errors = 0
            state.last_error = None
        except asyncio.CancelledError:
            state.running = False
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            state.error_count += 1
            state.consecutive_errors += 1
            state.last_error = error
            log.exception("task failed", extra={"task": spec.name})
            if state.consecutive_errors in (1, 5, 20):
                with contextlib.suppress(Exception):
                    await self.ctx.record(
                        "task", f"{spec.name} failed: {error}", level="error"
                    )
        finally:
            state.running = False
            state.last_duration = time.perf_counter() - started
            state.last_finish = datetime.now(UTC).isoformat()
            with contextlib.suppress(Exception):
                await self.ctx.repos.tasks.finish(spec.name, state.last_duration, error)
            self.ctx.bus.publish(
                "task.finish",
                task=spec.name,
                duration=round(state.last_duration, 3),
                error=error,
            )

    def _interval(self, spec: TaskSpec) -> int:
        try:
            return max(int(spec.interval(self.ctx)), 5)
        except Exception:
            return 60

    def _delay_for(self, state: TaskState) -> float:
        """The task's current cadence, including failure back-off."""
        interval = float(self._interval(state.spec))
        # Back off exponentially while a task keeps failing, so a dead tracker
        # is not hammered every 30 seconds.
        if state.consecutive_errors:
            interval = min(interval * (2 ** min(state.consecutive_errors, 4)), 3600.0)
        return interval

    async def _sleep(self, state: TaskState, seconds: float) -> None:
        """Wait for the next run, waking early on trigger or shutdown.

        The deadline is re-checked against the *live* interval every few
        seconds, so shortening a cadence in Settings takes effect within
        seconds. Previously the task simply slept out the delay it had already
        computed -- for the library index that was up to half an hour, which
        looked exactly like the setting not working at all.
        """
        started = time.monotonic()
        deadline = started + seconds

        while not self._stopping.is_set():
            if state.wake.is_set():
                state.wake.clear()
                return
            # Only ever brings the deadline forward: a lengthened interval
            # applies from the next cycle, a shortened one applies now.
            deadline = min(deadline, started + self._delay_for(state))
            state.next_run_at = deadline
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(
                    state.wake.wait(), timeout=min(remaining, INTERVAL_RECHECK_SECONDS)
                )
            except TimeoutError:
                continue
            state.wake.clear()
            return  # triggered from the API, or shutting down
