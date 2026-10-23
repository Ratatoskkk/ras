"""PiPup client: pop-up notifications on an Android / Google TV.

PiPup (https://github.com/rogro82/PiPup) runs a small HTTP server on the TV
and draws whatever is POSTed to ``/notify`` as an overlay above the current
app. The TV is often asleep or switched off, so every call is a single short
attempt: a missed pop-up is not worth retrying, and must never hold up the
pipeline that triggered it.
"""

from __future__ import annotations

from typing import Any

from ..util.resilience import RetryPolicy
from .http import HttpService

PIPUP_PORT = 7979

POSITIONS = {
    "top_right": 0,
    "top_left": 1,
    "bottom_right": 2,
    "bottom_left": 3,
    "center": 4,
}

SINGLE_ATTEMPT = RetryPolicy(attempts=1)


class PiPupClient:
    def __init__(self, *, timeout: float = 4.0) -> None:
        # No base URL: the TV's address is live config, so it is passed per call
        # and a Settings edit applies to the very next pop-up.
        self.http = HttpService(
            "pipup",
            timeout=timeout,
            connect_timeout=timeout,
            retry_policy=SINGLE_ATTEMPT,
            # A switched-off TV is the normal case, not an outage. Tripping a
            # breaker would only make "Send test" fail after the TV wakes up.
            breaker_threshold=10**9,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def notify(
        self,
        host: str,
        *,
        title: str,
        message: str,
        duration: int = 12,
        position: str = "top_right",
        accent: str = "#FFFFFF",
        image_url: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "duration": duration,
            "position": POSITIONS.get(position, 0),
            "title": title,
            "titleColor": accent,
            "titleSize": 20,
            "message": message,
            "messageColor": "#EEEEEE",
            "messageSize": 16,
            "backgroundColor": "#E6141414",
            # Styling from the maintained fork (lapicidae/PiPup). The original
            # app ignores fields it does not know, so these are safe on either.
            "borderRadius": 16,
            "contentPadding": 20,
            "animationType": 2,  # slide
            "animationExit": True,
        }
        if image_url:
            payload["media"] = {"image": {"uri": image_url, "width": 120}}
            payload["mediaPosition"] = 2  # poster left of the text
        await self.http.request("POST", f"{base_url(host)}/notify", json=payload)


def base_url(host: str) -> str:
    """Accept ``192.168.1.5``, ``192.168.1.5:7979`` or a full URL."""
    host = host.strip().rstrip("/")
    if "://" not in host:
        host = f"http://{host}"
    if host.count(":") < 2:
        host = f"{host}:{PIPUP_PORT}"
    return host
