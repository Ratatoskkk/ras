"""Quality-profile scoring.

Every release is scored against a declarative profile and either accepted with
a numeric score or rejected with an explicit reason. Both halves matter: the
score picks the winner, and the reasons are what the dashboard shows when the
answer is "nothing was good enough", instead of the reference project's silent
``return None``.
"""

from __future__ import annotations

from ..config import QualityProfile
from .models import ParsedRelease, Rejection, Release, ScoredRelease
from .parser import parse_release

GIB = 1024**3


def evaluate(
    release: Release,
    profile: QualityProfile,
    *,
    parsed: ParsedRelease | None = None,
    episode_count: int = 1,
) -> ScoredRelease:
    """Score one release. ``episode_count`` lets size limits apply per episode."""
    parsed = parsed or parse_release(release)
    scored = ScoredRelease(release=release, parsed=parsed)
    lowered = release.name.lower()

    # -- hard filters -------------------------------------------------------
    # Blocked and required terms are lower-cased when the profile is validated,
    # so matching here is a plain substring test.
    for term in profile.blocked_terms:
        if term in lowered:
            scored.rejections.append(Rejection("blocked_term", term))
    if parsed.is_full_disc and any(
        t in ("full disc", "bd50", "bd25") for t in profile.blocked_terms
    ):
        scored.rejections.append(Rejection("full_disc", "full Blu-ray disc"))

    for term in profile.required_terms:
        if term not in lowered:
            scored.rejections.append(Rejection("required_term_missing", term))

    if profile.require_internal and not release.internal:
        scored.rejections.append(Rejection("not_internal", "profile requires internal releases"))

    if release.seeders < profile.seeder_floor:
        scored.rejections.append(
            Rejection("seeders", f"{release.seeders} < {profile.seeder_floor}")
        )

    # -- size window --------------------------------------------------------
    size = float(release.size_bytes or 0)
    if size <= 0:
        scored.rejections.append(Rejection("size", "unknown size"))
    else:
        if size < profile.min_size_bytes:
            scored.rejections.append(
                Rejection("size_min", f"{size / GIB:.1f} GB < {profile.min_size_gb} GB")
            )
        if size > profile.max_size_bytes:
            scored.rejections.append(
                Rejection("size_max", f"{size / GIB:.1f} GB > {profile.max_size_gb} GB")
            )
        if profile.max_size_per_episode_gb > 0 and episode_count > 0:
            per_episode = size / episode_count / GIB
            if per_episode > profile.max_size_per_episode_gb:
                scored.rejections.append(
                    Rejection(
                        "size_per_episode",
                        f"{per_episode:.1f} GB/ep > {profile.max_size_per_episode_gb} GB",
                    )
                )

    # -- attribute scores ---------------------------------------------------
    total = 0
    breakdown: dict[str, int] = {}

    for bucket, value, required in (
        ("resolutions", parsed.resolution, True),
        ("sources", parsed.source, True),
        ("dynamic_range", parsed.dynamic_range, False),
        ("video_codecs", parsed.video_codec, False),
        ("audio", parsed.audio, False),
        ("groups", (parsed.release_group or "").lower() or None, False),
    ):
        points = profile.lookup(bucket, value)
        if points is None:
            configured = bool(getattr(profile, bucket))
            if required and configured:
                if value is None and not profile.allow_unknown_resolution:
                    scored.rejections.append(Rejection(bucket, "could not be determined"))
                elif value is not None:
                    scored.rejections.append(Rejection(bucket, f"{value!r} not in profile"))
            continue
        total += points
        breakdown[bucket] = points

    # -- bonuses ------------------------------------------------------------
    if profile.seeder_bonus_per_10 and release.seeders:
        bonus = min(release.seeders // 10, 20) * profile.seeder_bonus_per_10
        if bonus:
            total += bonus
            breakdown["seeders"] = bonus
    if profile.freeleech_bonus and release.freeleech:
        total += profile.freeleech_bonus
        breakdown["freeleech"] = profile.freeleech_bonus
    if release.internal:
        total += 10
        breakdown["internal"] = 10
    if parsed.is_repack or parsed.is_proper:
        total += 15
        breakdown["repack"] = 15
    if parsed.is_hybrid:
        total += 10
        breakdown["hybrid"] = 10
    if release.indexer_score_bonus:
        total += release.indexer_score_bonus
        breakdown["indexer"] = release.indexer_score_bonus

    scored.score = total
    scored.breakdown = breakdown

    if not scored.rejections and total < profile.min_score:
        scored.rejections.append(
            Rejection("min_score", f"score {total} < required {profile.min_score}")
        )
    return scored


def rank(
    releases: list[Release],
    profile: QualityProfile,
    *,
    episode_counts: dict[tuple[str, str], int] | None = None,
    parsed: dict[tuple[str, str], ParsedRelease] | None = None,
) -> list[ScoredRelease]:
    """Score everything and return it best-first, rejects included.

    Rejects are kept so the UI can explain the decision; callers filter with
    :func:`best`. ``episode_counts`` and ``parsed`` are keyed by
    ``Release.key`` -- torrent ids are only unique within one tracker. Pass
    ``parsed`` when the caller already parsed the names, so they are not
    parsed a second time.
    """
    episode_counts = episode_counts or {}
    parsed = parsed or {}
    scored = [
        evaluate(
            release,
            profile,
            parsed=parsed.get(release.key),
            episode_count=episode_counts.get(release.key, 1),
        )
        for release in releases
    ]
    return sorted(scored, key=_sort_key(profile), reverse=True)


def best(scored: list[ScoredRelease], profile: QualityProfile) -> ScoredRelease | None:
    accepted = [s for s in scored if s.accepted]
    if not accepted:
        return None
    return max(accepted, key=_sort_key(profile))


def _sort_key(profile: QualityProfile):
    if profile.tie_break == "seeders":
        return lambda s: (s.score, s.release.seeders, -s.release.size_bytes)
    if profile.tie_break == "score_only":
        return lambda s: (s.score, 0, 0)
    return lambda s: (s.score, s.release.size_bytes, s.release.seeders)


def compare(candidate: ScoredRelease, incumbent_score: int, margin: int = 50) -> bool:
    """Is ``candidate`` a worthwhile upgrade over something already grabbed?

    A margin stops trivial score wobble from re-downloading a 70 GB remux.
    """
    return candidate.accepted and candidate.score >= incumbent_score + margin
