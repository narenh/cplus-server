"""What a notification actually says.

The two display lines are the contract with the client — an admin reads them on
a lock screen and nothing else about the event reaches them — so they are
asserted verbatim rather than by shape.
"""

from __future__ import annotations

import pytest

from cplus_service.notify.messages import (
    MediaSummary,
    media_from_release_title,
    title_case,
    user_action,
    user_requested,
)
from cplus_service.notify.types import (
    NOTIFICATION_TYPES,
    NOTIFICATION_TYPES_BY_VALUE,
    NotificationType,
)

# --------------------------------------------------------------------------- #
# The two lines
# --------------------------------------------------------------------------- #


def test_a_request_reads_as_the_spec_says() -> None:
    notification = user_requested(
        MediaSummary(title="The End of Oak Street", year=2026),
        username="Jane Dietrich",
    )

    assert notification.title == "The End of Oak Street (2026)"
    assert notification.subtitle == "Requested by Jane Dietrich"
    assert notification.type is NotificationType.USER_REQUESTED


def test_an_action_reads_as_the_spec_says() -> None:
    notification = user_action(
        MediaSummary(title="I Love Boosters", year=2026),
        username="Jane Dietrich",
        action_name="Stream Now",
    )

    assert notification.title == "I Love Boosters (2026)"
    assert notification.subtitle == "Jane Dietrich: Stream Now"
    assert notification.type is NotificationType.USER_ACTION


def test_the_type_travels_in_the_payload_data() -> None:
    """So the app can route a tap without parsing the text back apart."""
    notification = user_requested(
        MediaSummary(title="Anything"), username="Someone", tmdb_id=603
    )
    assert notification.data == {"type": "user_requested", "tmdb_id": 603}


def test_an_unknown_year_drops_the_parenthetical_rather_than_emptying_it() -> None:
    assert MediaSummary(title="Untitled", year=None).display == "Untitled"
    assert MediaSummary(title="Untitled", year=2026).display == "Untitled (2026)"


# --------------------------------------------------------------------------- #
# The release-title fallback
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("release", "expected_title", "expected_year"),
    [
        (
            "The.End.of.Oak.Street.2026.1080p.WEB-DL.DDP5.1.H.264-FLUX",
            "The End of Oak Street",
            2026,
        ),
        (
            "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR.HEVC-FraMeSToR",
            "Dune Part Two",
            2024,
        ),
        (
            "I Love Boosters 2026 1080p WEBRip x264-YIFY",
            "I Love Boosters",
            2026,
        ),
        # A hyphenated name with no group: the parser keeps the hyphen out of
        # group detection, and the year still terminates the title.
        (
            "Spider-Man.2002.1080p.BluRay.x264",
            "Spider Man",
            2002,
        ),
    ],
)
def test_a_release_title_can_stand_in_for_a_real_one(
    release: str, expected_title: str, expected_year: int
) -> None:
    media = media_from_release_title(release)
    assert media.title == expected_title
    assert media.year == expected_year


def test_a_release_with_no_year_still_yields_a_name() -> None:
    media = media_from_release_title("Some.Documentary.1080p.WEB-DL.H264-GRP")
    assert media.year is None
    assert media.title == "Some Documentary"


def test_an_unparseable_release_title_is_shown_rather_than_replaced() -> None:
    """A notification reading like an indexer listing beats one reading 'Unknown'."""
    media = media_from_release_title("...")
    assert media.title == "..."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("the end of oak street", "The End of Oak Street"),
        ("a tale of two cities", "A Tale of Two Cities"),
        # A minor word leading the title is still capitalised.
        ("of mice and men", "Of Mice and Men"),
        ("", ""),
    ],
)
def test_title_casing_leaves_joining_words_alone_unless_they_lead(
    raw: str, expected: str
) -> None:
    assert title_case(raw) == expected


# --------------------------------------------------------------------------- #
# The catalogue
# --------------------------------------------------------------------------- #


def test_every_type_has_a_catalogue_entry() -> None:
    """The admin UI renders from the catalogue, so a type missing here is invisible."""
    assert {info.type for info in NOTIFICATION_TYPES} == set(NotificationType)


def test_the_catalogue_lookup_covers_every_entry() -> None:
    assert set(NOTIFICATION_TYPES_BY_VALUE) == {t.value for t in NotificationType}
