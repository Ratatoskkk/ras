"""Pop-up alerts on the TV for the moments worth interrupting a film for.

Services call :meth:`Conduit.alert` right after recording the matching
timeline event. Sending happens in a background task, so an unreachable TV
costs the pipeline nothing; the failure is logged and forgotten.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..logs import get_logger

if TYPE_CHECKING:
    from .context import Conduit

log = get_logger("notify")

POSTER_URL = "https://image.tmdb.org/t/p/w342{path}"

# kind -> (config toggle, heading, accent colour)
ALERTS: dict[str, tuple[str, str, str]] = {
    "started": ("on_grab", "Download started", "#7FB3FF"),
    "completed": ("on_complete", "Ready to watch", "#7BD88F"),
    "failed": ("on_error", "Download problem", "#FF7B72"),
    "approval": ("on_approval_required", "Needs approval", "#F2C14E"),
    "test": ("", "rás", "#FFFFFF"),
}


def wanted(ctx: Conduit, kind: str) -> bool:
    settings = ctx.config.notifications
    if ctx.tv is None or not settings.tv_host.strip():
        return False
    toggle = ALERTS[kind][0]
    return not toggle or bool(getattr(settings, toggle))


async def test(ctx: Conduit, host: str) -> None:
    """Send a pop-up straight away, to an address that may not be saved yet."""
    settings = ctx.config.notifications
    _, heading, accent = ALERTS["test"]
    await ctx.tv.notify(
        host,
        title=heading,
        message="TV notifications are working",
        duration=settings.tv_duration_seconds,
        position=settings.tv_position,
        accent=accent,
    )


async def send(ctx: Conduit, kind: str, text: str, *, media_id: int | None = None) -> None:
    """Show one pop-up. Raises on failure -- :meth:`Conduit.alert` swallows it."""
    settings = ctx.config.notifications
    _, heading, accent = ALERTS[kind]
    image_url = None
    if media_id is not None:
        media = await ctx.repos.media.get(int(media_id))
        if media and media.get("poster_path"):
            image_url = POSTER_URL.format(path=media["poster_path"])
    await ctx.tv.notify(
        settings.tv_host,
        title=heading,
        message=text,
        duration=settings.tv_duration_seconds,
        position=settings.tv_position,
        accent=accent,
        image_url=image_url,
    )
