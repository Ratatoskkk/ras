"""Utilities: bencode/info-hash, resilience primitives, text helpers."""

from __future__ import annotations

import asyncio
import hashlib
import time

import pytest

from conduit.util import bencode
from conduit.util.resilience import (
    CircuitBreaker,
    CircuitOpen,
    PermanentError,
    RateLimited,
    RateLimiter,
    RetryPolicy,
    TransientError,
    retry,
)
from conduit.util.text import (
    human_duration,
    human_size,
    normalize_title,
    title_similarity,
)


class TestBencode:
    def test_round_trips_every_type(self) -> None:
        value = {b"a": 1, b"b": [b"x", 2], b"c": {b"d": b"e"}}
        assert bencode.decode(bencode.encode(value)) == value

    def test_info_hash_matches_a_manual_sha1(self) -> None:
        info = {b"name": b"thing.mkv", b"length": 1234, b"piece length": 262144}
        torrent = bencode.encode({b"announce": b"https://t.test/a", b"info": info})
        assert bencode.info_hash(torrent) == hashlib.sha1(bencode.encode(info)).hexdigest()

    def test_summary_reads_a_multi_file_torrent(self) -> None:
        torrent = bencode.encode({
            b"info": {
                b"name": b"Show.S01",
                b"files": [
                    {b"length": 100, b"path": [b"e1.mkv"]},
                    {b"length": 250, b"path": [b"e2.mkv"]},
                ],
            }
        })
        summary = bencode.torrent_summary(torrent)
        assert summary["name"] == "Show.S01"
        assert summary["size_bytes"] == 350
        assert summary["file_count"] == 2

    def test_info_hash_uses_the_original_bytes_of_an_unsorted_info_dict(self) -> None:
        # Keys out of canonical order: re-encoding would sort them and hash
        # something qBittorrent never sees.
        info = b"d6:lengthi5e4:name1:x12:piece lengthi16e6:pieces0:e"
        unsorted = b"d4:name1:x6:lengthi5e12:piece lengthi16e6:pieces0:e"
        assert bencode.info_hash(b"d4:info" + unsorted + b"e") == hashlib.sha1(unsorted).hexdigest()
        assert bencode.info_hash(b"d4:info" + info + b"e") == hashlib.sha1(info).hexdigest()
        summary = bencode.torrent_summary(b"d4:info" + unsorted + b"e")
        assert summary["info_hash"] == hashlib.sha1(unsorted).hexdigest()
        assert summary["size_bytes"] == 5

    def test_rejects_html_error_pages(self) -> None:
        with pytest.raises(bencode.BencodeError):
            bencode.info_hash(b"<html>rate limited</html>")

    def test_rejects_a_torrent_without_an_info_dict(self) -> None:
        with pytest.raises(bencode.BencodeError):
            bencode.info_hash(bencode.encode({b"announce": b"x"}))


class TestRetry:
    async def test_gives_up_after_the_configured_attempts(self) -> None:
        calls = 0

        async def always_fails():
            nonlocal calls
            calls += 1
            raise TransientError("nope")

        with pytest.raises(TransientError):
            await retry(always_fails, policy=RetryPolicy(attempts=3, base_delay=0.001))
        assert calls == 3

    async def test_succeeds_once_the_upstream_recovers(self) -> None:
        calls = 0

        async def flaky():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise TransientError("not yet")
            return "ok"

        assert await retry(flaky, policy=RetryPolicy(attempts=5, base_delay=0.001)) == "ok"

    async def test_honours_a_retry_after_hint(self) -> None:
        started = time.monotonic()
        calls = 0

        async def limited():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RateLimited("slow down", retry_after=0.25)
            return "ok"

        assert await retry(limited, policy=RetryPolicy(attempts=3)) == "ok"
        # Generous lower bound: the Windows timer granularity is ~15 ms and can
        # undershoot a requested sleep slightly.
        assert time.monotonic() - started >= 0.15


    async def test_gives_up_rather_than_sleeping_out_a_long_retry_after(self) -> None:
        async def limited():
            raise RateLimited("slow down", retry_after=3600)

        started = time.monotonic()
        with pytest.raises(RateLimited):
            await retry(limited, policy=RetryPolicy(attempts=3, max_delay=1.0))
        assert time.monotonic() - started < 1.0


class TestRateLimiter:
    async def test_spreads_requests_across_the_budget(self) -> None:
        limiter = RateLimiter(rate_per_minute=600, burst=1)  # 10/s
        await limiter.acquire()
        started = time.monotonic()
        await limiter.acquire()
        assert time.monotonic() - started >= 0.05

    async def test_zero_disables_limiting(self) -> None:
        limiter = RateLimiter(rate_per_minute=0)
        started = time.monotonic()
        await asyncio.gather(*(limiter.acquire() for _ in range(50)))
        assert time.monotonic() - started < 0.1


class TestCircuitBreaker:
    async def test_opens_after_repeated_failures(self) -> None:
        breaker = CircuitBreaker("test", threshold=2, recovery_seconds=60)

        async def boom():
            raise TransientError("down")

        for _ in range(2):
            with pytest.raises(TransientError):
                await breaker.call(boom)
        assert breaker.state == "open"
        with pytest.raises(CircuitOpen):
            await breaker.call(boom)

    async def test_configuration_errors_do_not_trip_it(self) -> None:
        """A bad API key is not a reason to stop calling everything else."""
        breaker = CircuitBreaker("test", threshold=1)

        async def bad_key():
            raise PermanentError("401")

        with pytest.raises(PermanentError):
            await breaker.call(bad_key)
        assert breaker.state == "closed"

    async def test_recovers_after_the_window(self) -> None:
        breaker = CircuitBreaker("test", threshold=1, recovery_seconds=0.05)

        async def boom():
            raise TransientError("down")

        with pytest.raises(TransientError):
            await breaker.call(boom)
        await asyncio.sleep(0.06)
        assert breaker.state == "half_open"
        assert await breaker.call(lambda: asyncio.sleep(0, result="ok")) == "ok"
        assert breaker.state == "closed"


    async def test_a_failed_half_open_probe_reopens_it(self) -> None:
        breaker = CircuitBreaker("test", threshold=1, recovery_seconds=60)

        async def boom():
            raise TransientError("down")

        with pytest.raises(TransientError):
            await breaker.call(boom)
        # Backdated rather than slept: Windows timers can undershoot a short
        # sleep, which made this flaky.
        breaker._opened_at -= 61
        assert breaker.state == "half_open"
        with pytest.raises(TransientError):
            await breaker.call(boom)
        assert breaker.state == "open"
        with pytest.raises(CircuitOpen):
            await breaker.call(boom)


class TestText:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("The Lord of the Rings: Part II", "Lord of the Rings Part 2"),
            ("Mission: Impossible - Dead Reckoning", "Mission Impossible Dead Reckoning"),
            ("Wall·E", "Wall E"),
            ("Law & Order", "Law and Order"),
        ],
    )
    def test_equivalent_titles_normalise_to_the_same_key(self, left, right) -> None:
        assert normalize_title(left) == normalize_title(right)

    def test_different_titles_stay_apart(self) -> None:
        assert title_similarity("Dune", "Dunkirk") < 0.85

    def test_similarity_is_symmetric_and_bounded(self) -> None:
        assert title_similarity("Silo", "Silo") == 1.0
        assert title_similarity("", "Silo") == 0.0

    def test_sizes_read_the_way_people_write_them(self) -> None:
        assert human_size(0) == "0 B"
        assert human_size(1536) == "2 KB"
        assert human_size(88 * 1024**3) == "88.0 GB"

    def test_durations_degrade_gracefully(self) -> None:
        assert human_duration(-1) == "∞"
        assert human_duration(None) == "∞"
        assert human_duration(45) == "45s"
        assert human_duration(3600) == "1h 00m"
        assert human_duration(90000) == "1d 01h"
