"""Release-name parsing.

Every string here is a real release name taken from a live tracker or from a
Plex library, not an invented one.
"""

from __future__ import annotations

import pytest

from conduit.domain.models import Release
from conduit.domain.parser import parse, parse_release


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "Inception 2010 Hybrid 2160p UHD BluRay REMUX DV HDR HEVC DTS-HD MA 5.1-LM",
            {"title": "Inception", "year": 2010, "resolution": "2160p", "source": "remux",
             "dynamic_range": "dv_hdr10", "video_codec": "hevc", "audio": "dts_hd_ma",
             "release_group": "LM", "is_hybrid": True},
        ),
        (
            "Silo S03E05 Memory 2160p ATVP WEB-DL DD+ 5.1 Atmos DV HDR10+ H.265-Kitsune",
            {"title": "Silo", "season": 3, "episodes": [5], "resolution": "2160p",
             "source": "webdl", "dynamic_range": "dv_hdr10plus", "audio": "eac3_atmos",
             "release_group": "Kitsune"},
        ),
        (
            "Game of Thrones S03 1080p GER Blu-ray AVC DTS-HD MA 5.1-prte",
            {"title": "Game of Thrones", "season": 3, "is_season_pack": True,
             "source": "bluray", "video_codec": "avc", "release_group": "prte"},
        ),
        (
            "The.Bear.S02E01-E03.1080p.DSNP.WEB-DL.DDP5.1.H.264-NTb",
            {"title": "The Bear", "season": 2, "episodes": [1, 2, 3], "audio": "dd_plus"},
        ),
        (
            "Breaking.Bad.S01-S05.COMPLETE.1080p.BluRay.x265-GROUP",
            {"title": "Breaking Bad", "is_complete_series": True, "is_season_pack": True},
        ),
        (
            "The.Daily.Show.2024.05.14.1080p.WEB.h264-BAE",
            {"title": "The Daily Show", "year": None, "resolution": "1080p"},
        ),
        (
            "Dune Part Two 2024 2160p UHD BluRay DV HDR10+ TrueHD 7.1 Atmos x265-W4NK3R",
            {"dynamic_range": "dv_hdr10plus", "audio": "truehd_atmos",
             "audio_channels": "7.1", "release_group": "W4NK3R"},
        ),
        (
            "House.of.the.Dragon.S03E04.Episode.4.REPACK.2160p.HMAX.WEB-DL.DDP5.1.Atmos.DV.H.265-Kitsune",
            {"title": "House of the Dragon", "season": 3, "episodes": [4], "is_repack": True,
             "dynamic_range": "dv", "audio": "eac3_atmos"},
        ),
    ],
)
def test_parses_real_release_names(name: str, expected: dict) -> None:
    parsed = parse(name)
    for key, want in expected.items():
        assert getattr(parsed, key) == want, f"{key} on {name!r}"


def test_year_at_position_zero_is_the_title_not_metadata() -> None:
    """`1923` and `2012` are titles. Treating them as years loses the show."""
    assert parse("1923.S01E01.1923.2160p.AMZN.WEB-DL.DDP5.1.H.265-NTb").title == "1923"
    movie = parse("2012.2009.1080p.BluRay.DTS.x264-DON")
    assert movie.title == "2012"
    assert movie.year == 2009


def test_date_based_episode_does_not_become_the_year() -> None:
    parsed = parse("The.Daily.Show.2024.05.14.1080p.WEB.h264-BAE")
    assert parsed.air_date is not None
    assert parsed.air_date.isoformat() == "2024-05-14"
    assert parsed.year is None


def test_complete_series_phrase_is_stripped_from_the_title() -> None:
    parsed = parse("Firefly The Complete Series 2002 1080p BluRay REMUX AVC DTS-HD MA 5.1-EPSiLON")
    assert parsed.title == "Firefly"
    assert parsed.is_complete_series


def test_attribute_words_are_not_mistaken_for_a_group() -> None:
    assert parse("Inception 2010 720p AMZN WEB-DL DD+ 5.1 H.264").release_group is None


def test_multi_episode_range_expands() -> None:
    assert parse("Show.S01E05-E08.1080p.WEB-DL-X").episodes == [5, 6, 7, 8]


def test_x_format_episode_numbering() -> None:
    parsed = parse("Show Name 2x07 1080p HDTV x264-GRP")
    assert parsed.season == 2
    assert parsed.episodes == [7]


class TestIndexerHints:
    """The tracker's own classification beats guessing from the name."""

    def test_full_disc_type_is_trusted_even_when_the_name_is_silent(self) -> None:
        release = Release(
            indexer="T", indexer_id="1", name="Some Movie 2020 2160p UHD Blu-ray HEVC",
            size_bytes=1, download_url="", type_name="Full Disc", resolution_name="2160p",
        )
        parsed = parse_release(release)
        assert parsed.is_full_disc is True

    def test_resolution_hint_normalises_4k(self) -> None:
        release = Release(
            indexer="T", indexer_id="1", name="Some Movie 2020 BluRay", size_bytes=1,
            download_url="", resolution_name="4K",
        )
        assert parse_release(release).resolution == "2160p"

    def test_remux_type_overrides_a_vague_name(self) -> None:
        release = Release(
            indexer="T", indexer_id="1", name="Some Movie 2020 2160p UHD", size_bytes=1,
            download_url="", type_name="Remux",
        )
        assert parse_release(release).source == "remux"


class TestCoverage:
    def test_season_pack_covers_any_episode_of_that_season(self) -> None:
        parsed = parse("Silo S02 2160p ATVP WEB-DL DV HDR10+ H.265-Kitsune")
        assert parsed.covers(2, 7) is True
        assert parsed.covers(3, 1) is False

    def test_single_episode_covers_only_itself(self) -> None:
        parsed = parse("Silo S02E07 2160p ATVP WEB-DL H.265-Kitsune")
        assert parsed.covers(2, 7) is True
        assert parsed.covers(2, 8) is False

    def test_complete_series_covers_everything(self) -> None:
        parsed = parse("Breaking.Bad.S01-S05.COMPLETE.1080p.BluRay.x265-GRP")
        assert parsed.covers(4, 9) is True
        assert parsed.covers(6, 1) is False
