"""The content-source encoding shared by home shelves and the carousel.

The two functions that matter are inverses of each other on every path
:func:`source_options` actually offers — ``source_of(resolve_source(v, libs))
== v`` — which is what lets the dropdown pre-select whatever a shelf is
already set to.
"""

from __future__ import annotations

from cplus_service.api.routes.admin.home_sources import (
    DISCOVER_TRENDING,
    DISCOVER_WATCHLIST,
    ON_DECK,
    collection_shelf_library_id,
    grouped_options,
    resolve_collection_source,
    resolve_source,
    source_of,
    source_options,
)

MOVIE_LIBRARY = {
    "id": "1",
    "serverTitle": "Movies (4K HDR)",
    "type": "movie",
    "hidden": False,
    "name": "Movies",
}
SHOW_LIBRARY = {
    "id": "2",
    "serverTitle": "TV Shows",
    "type": "show",
    "hidden": False,
    "name": "TV Shows",
}
LIBRARIES_BY_ID = {"1": MOVIE_LIBRARY, "2": SHOW_LIBRARY}


# --------------------------------------------------------------------------- #
# resolve_source
# --------------------------------------------------------------------------- #


def test_on_deck_resolves_to_the_apps_own_continue_watching_shelf() -> None:
    fields = resolve_source(ON_DECK, LIBRARIES_BY_ID)
    assert fields == {
        "path": "/library/onDeck",
        "discoverHubKey": None,
        "description": "Continue Watching / On Deck",
        "title": "Continue Watching",
        "style": "card",
        "titleOnly": False,
    }


def test_a_movie_librarys_newest_defaults_to_poster_and_title_only() -> None:
    fields = resolve_source("lib:1:newest", LIBRARIES_BY_ID)
    assert fields["path"] == "/library/sections/1/newest"
    assert fields["description"] == "Movies (4K HDR): Recently Released"
    assert fields["title"] == "Recently Released Movies"
    assert fields["style"] == "poster"
    assert fields["titleOnly"] is True


def test_a_show_librarys_newest_defaults_to_card() -> None:
    # Only a movie library's Recently Released defaults to poster — everything
    # else about a non-movie library's cards is unaffected.
    fields = resolve_source("lib:2:newest", LIBRARIES_BY_ID)
    assert fields["style"] == "card"


def test_recently_added_is_never_title_only() -> None:
    fields = resolve_source("lib:1:recentlyAdded", LIBRARIES_BY_ID)
    assert fields["titleOnly"] is False
    assert fields["title"] == "Recently Added Movies"


def test_all_collections_and_all_items_use_the_librarys_own_name() -> None:
    assert resolve_source("lib:1:collections", LIBRARIES_BY_ID)["title"] == "Collections in Movies"
    assert resolve_source("lib:1:all", LIBRARIES_BY_ID)["title"] == "All Movies"


def test_discover_watchlist_and_trending() -> None:
    watchlist = resolve_source(DISCOVER_WATCHLIST, LIBRARIES_BY_ID)
    assert watchlist["discoverHubKey"] == "/library/sections/watchlist/all"
    assert watchlist["path"] == ""

    trending = resolve_source(DISCOVER_TRENDING, LIBRARIES_BY_ID)
    assert trending["discoverHubKey"] == "/hubs/sections/home/trending-plex"
    assert trending["title"] == "Trending"


def test_an_unknown_library_id_is_unresolvable() -> None:
    assert resolve_source("lib:999:all", LIBRARIES_BY_ID) is None


def test_an_unknown_kind_is_unresolvable() -> None:
    assert resolve_source("lib:1:nonsense", LIBRARIES_BY_ID) is None


def test_a_malformed_source_is_unresolvable() -> None:
    assert resolve_source("not-a-source", LIBRARIES_BY_ID) is None
    assert resolve_source("lib:only-two-parts", LIBRARIES_BY_ID) is None


# --------------------------------------------------------------------------- #
# source_of / resolve_source round-trip
# --------------------------------------------------------------------------- #


def test_source_of_and_resolve_source_round_trip_every_offered_option() -> None:
    for option in source_options([MOVIE_LIBRARY, SHOW_LIBRARY], allow_discover=True):
        if option.value.startswith("collections:"):
            # A placeholder that reveals a second, live picker — not a real,
            # resolvable source. See resolve_collection_source instead.
            continue
        fields = resolve_source(option.value, LIBRARIES_BY_ID)
        assert fields is not None, option.value
        assert source_of(fields) == option.value


def test_source_of_a_shelf_with_no_recognisable_path_is_blank() -> None:
    assert source_of({"path": "/library/collections/9/children", "discoverHubKey": None}) == ""


# --------------------------------------------------------------------------- #
# source_options / grouped_options
# --------------------------------------------------------------------------- #


def test_source_options_leads_with_continue_watching() -> None:
    options = source_options([], allow_discover=True)
    assert options[0].value == ON_DECK
    assert options[0].group is None


def test_source_options_offers_five_entries_per_library() -> None:
    options = source_options([MOVIE_LIBRARY], allow_discover=False)
    per_library = [o for o in options if o.group == "Movies"]
    assert {o.value for o in per_library} == {
        "lib:1:newest",
        "lib:1:recentlyAdded",
        "lib:1:collections",
        "lib:1:all",
        "collections:1",
    }


def test_discover_hubs_are_withheld_from_the_carousel() -> None:
    shelf_options = source_options([], allow_discover=True)
    carousel_options = source_options([], allow_discover=False)

    assert any(o.value == DISCOVER_WATCHLIST for o in shelf_options)
    assert not any(o.value == DISCOVER_WATCHLIST for o in carousel_options)


def test_grouped_options_buckets_contiguous_runs_without_sorting() -> None:
    # None mixed with library-name groups would blow up Jinja's own groupby
    # (it sorts by the key first, and None can't be compared to a str).
    options = source_options([MOVIE_LIBRARY, SHOW_LIBRARY], allow_discover=True)
    groups = grouped_options(options)

    labels = [label for label, _ in groups]
    assert labels[0] is None  # Continue Watching, ungrouped
    assert "Movies" in labels
    assert "TV Shows" in labels
    assert "Discover Hubs" in labels
    # Each library's five options land in one bucket, not split across several.
    assert len([label for label in labels if label == "Movies"]) == 1


# --------------------------------------------------------------------------- #
# resolve_collection_source
# --------------------------------------------------------------------------- #


def test_resolve_collection_source_names_the_library_and_the_collection() -> None:
    fields = resolve_collection_source("col:1:99", "Best of 2026", LIBRARIES_BY_ID)
    assert fields == {
        "path": "/library/collections/99/children",
        "discoverHubKey": None,
        "description": "Movies (4K HDR): Items in Best of 2026",
        "title": "Best of 2026",
        "style": "poster",
        "titleOnly": False,
    }


def test_resolve_collection_source_without_a_known_library_drops_the_prefix() -> None:
    fields = resolve_collection_source("col:999:99", "Best of 2026", {})
    assert fields is not None
    assert fields["description"] == "Items in Best of 2026"


def test_resolve_collection_source_rejects_a_blank_title() -> None:
    assert resolve_collection_source("col:1:99", "   ", LIBRARIES_BY_ID) is None


def test_resolve_collection_source_rejects_a_malformed_value() -> None:
    assert resolve_collection_source("col:1", "Title", LIBRARIES_BY_ID) is None
    assert resolve_collection_source("lib:1:all", "Title", LIBRARIES_BY_ID) is None


def test_a_collection_shelf_has_no_recognisable_source() -> None:
    # Plex collection ids are not scoped to a library, so there is no way to
    # reconstruct which library's "Collection Items…" entry it came from —
    # the main dropdown falls back to its own description-only placeholder.
    fields = resolve_collection_source("col:1:99", "Best of 2026", LIBRARIES_BY_ID)
    assert fields is not None
    assert source_of(fields) == ""


# --------------------------------------------------------------------------- #
# collection_shelf_library_id
# --------------------------------------------------------------------------- #


def test_collection_shelf_library_id_recovers_the_library_and_collection() -> None:
    shelf = resolve_collection_source("col:1:99", "Best of 2026", LIBRARIES_BY_ID)
    assert shelf is not None
    assert collection_shelf_library_id(shelf, LIBRARIES_BY_ID) == ("1", "99")


def test_collection_shelf_library_id_is_none_for_an_ordinary_shelf() -> None:
    shelf = resolve_source("lib:1:newest", LIBRARIES_BY_ID)
    assert shelf is not None
    assert collection_shelf_library_id(shelf, LIBRARIES_BY_ID) is None


def test_collection_shelf_library_id_is_none_when_no_library_matches() -> None:
    # The description's library-name prefix is all there is to go on — a
    # library since renamed or removed leaves this unrecoverable, same as
    # source_of already being unable to reconstruct a plain source for one.
    shelf = resolve_collection_source("col:1:99", "Best of 2026", {})
    assert collection_shelf_library_id(shelf, LIBRARIES_BY_ID) is None
