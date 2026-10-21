"""Grab tracking's pure parts: hashes, torrent matching, header pieces, paths, libraries.

Everything here is arithmetic over what qBittorrent and Plex report, so it is
tested without either of them.
"""

from __future__ import annotations

import pytest

from cplus_service.plex.client import PlexLibrarySection
from cplus_service.qbittorrent.client import Torrent, TorrentFile
from cplus_service.release.infohash import (
    info_hash_from_magnet,
    info_hash_of,
    normalise_info_hash,
)
from cplus_service.release.parser import parse_prowlarr_result
from cplus_service.tracking import paths
from cplus_service.tracking.hashes import ReleaseHashCache
from cplus_service.tracking.runner import guess_section_type
from cplus_service.tracking.torrent import (
    HEADER_BYTES,
    HeaderPlan,
    file_offset,
    header_pieces,
    main_video_file,
    match_by_hash,
    match_by_name,
    normalise_name,
)

MIB = 1024 * 1024
V1 = "c12fe1c06bba254a9dc9f519b335aa7c1367a88a"
# The same hash in base32, as older magnet links carry it.
V1_BASE32 = "YEX6DQDLXISUVHOJ6UM3GNNKPQJWPKEK"


# --------------------------------------------------------------------------- #
# Info hashes
# --------------------------------------------------------------------------- #


def test_a_hex_hash_is_lower_cased() -> None:
    assert normalise_info_hash(V1.upper()) == V1


def test_a_base32_hash_becomes_hex() -> None:
    assert normalise_info_hash(V1_BASE32) == V1


def test_something_else_is_not_a_hash() -> None:
    assert normalise_info_hash("not-a-hash") is None
    assert normalise_info_hash("") is None


def test_the_hash_comes_out_of_a_magnet_link() -> None:
    magnet = f"magnet:?xt=urn:btih:{V1.upper()}&dn=Some.Release&tr=udp%3A%2F%2Ft.example"
    assert info_hash_from_magnet(magnet) == V1


def test_a_v2_only_magnet_gives_the_v2_hash() -> None:
    digest = "ab" * 32
    assert info_hash_from_magnet(f"magnet:?xt=urn:btmh:1220{digest}") == digest


def test_a_hybrid_magnet_prefers_v1() -> None:
    magnet = f"magnet:?xt=urn:btmh:1220{'ab' * 32}&xt=urn:btih:{V1}"
    assert info_hash_from_magnet(magnet) == V1


def test_a_release_gives_its_hash_from_any_of_the_fields_prowlarr_uses() -> None:
    magnet = f"magnet:?xt=urn:btih:{V1}"
    assert info_hash_of({"infoHash": V1.upper()}) == V1
    assert info_hash_of({"magnetUrl": magnet}) == V1
    assert info_hash_of({"guid": magnet}) == V1
    assert info_hash_of({"downloadUrl": magnet}) == V1
    assert info_hash_of({"guid": "https://tracker.example/t/1", "downloadUrl": "https://x"}) is None


def test_the_parser_carries_the_hash_on_a_search_result() -> None:
    release = parse_prowlarr_result(
        {"title": "Movie.2024.1080p.WEB-DL-GRP", "guid": "g", "infoHash": V1}
    )
    assert release.info_hash == V1


def test_the_hash_cache_is_bounded_and_keyed_by_indexer_and_guid() -> None:
    cache = ReleaseHashCache(max_entries=2)
    cache.remember(1, "g1", "h1")
    cache.remember(2, "g1", "h2")
    cache.remember(1, "g2", None)  # nothing to remember
    assert cache.lookup(1, "g1") == "h1"
    assert cache.lookup(2, "g1") == "h2"
    cache.remember(1, "g3", "h3")
    assert cache.lookup(1, "g1") is None  # the oldest went
    assert len(cache) == 2


# --------------------------------------------------------------------------- #
# Finding the torrent
# --------------------------------------------------------------------------- #


def torrent(name: str, *, added_on: int = 1000, hash: str = "a" * 40, **extra: str) -> Torrent:
    return Torrent.from_api({"hash": hash, "name": name, "added_on": added_on, **extra})


def test_names_compare_without_case_or_punctuation() -> None:
    assert normalise_name("Movie.2024.1080p.WEB-DL") == normalise_name("movie 2024 1080p web dl")


def test_a_torrent_is_found_by_hash() -> None:
    wanted = torrent("Different Name", hash=V1)
    found = match_by_hash([torrent("Other", hash="b" * 40), wanted], [V1])
    assert found is wanted


def test_a_torrent_is_found_by_its_v2_hash() -> None:
    wanted = torrent("x", hash="d" * 40, infohash_v2="e" * 64)
    assert match_by_hash([wanted], ["e" * 64]) is wanted


def test_no_hashes_finds_nothing() -> None:
    assert match_by_hash([torrent("x")], []) is None


def test_by_name_the_newest_matching_torrent_since_the_grab_wins() -> None:
    old = torrent("Some.Release.2024-GRP", added_on=500, hash="1" * 40)
    newer = torrent("some release 2024 grp", added_on=2000, hash="2" * 40)
    newest_other = torrent("Another.Film.2024-GRP", added_on=3000, hash="3" * 40)

    found = match_by_name([old, newer, newest_other], "Some.Release.2024-GRP", added_since=1000)

    assert found is newer


def test_a_matching_torrent_added_before_the_grab_is_ignored_unless_asked() -> None:
    old = torrent("Some.Release.2024-GRP", added_on=500)
    assert match_by_name([old], "Some.Release.2024-GRP", added_since=1000) is None
    assert match_by_name([old], "Some.Release.2024-GRP", added_since=None) is old


def test_a_name_with_a_tracker_suffix_still_matches() -> None:
    suffixed = torrent("Some.Release.2024.1080p.WEB-DL-GRP[TGx]")
    found = match_by_name([suffixed], "Some.Release.2024.1080p.WEB-DL-GRP", added_since=None)
    assert found is suffixed


def test_a_short_partial_match_is_not_trusted() -> None:
    assert match_by_name([torrent("Up.2009-GRP.Extras")], "Up", added_since=None) is None


# --------------------------------------------------------------------------- #
# The main file and its header pieces
# --------------------------------------------------------------------------- #


def tfile(index: int, name: str, size: int, piece_range: tuple[int, int]) -> TorrentFile:
    return TorrentFile(index=index, name=name, size=size, progress=0.0, piece_range=piece_range)


def test_the_main_file_is_the_largest_video() -> None:
    files = [
        tfile(0, "R/R.nfo", 5_000, (0, 0)),
        tfile(1, "R/Sample/sample.mkv", 50 * MIB, (0, 50)),
        tfile(2, "R/R.mkv", 4_000 * MIB, (50, 4050)),
        tfile(3, "R/R.iso.part", 9_000 * MIB, (4050, 13050)),  # bigger, not a video
    ]
    assert main_video_file(files) is files[2]


def test_a_torrent_without_video_has_no_main_file() -> None:
    assert main_video_file([tfile(0, "R/R.rar", 100, (0, 0))]) is None


def test_single_file_aligned_header() -> None:
    """A single-file torrent starts at byte 0: 3 MiB in 1 MiB pieces is pieces 0–2."""
    piece = 1 * MIB
    main = tfile(0, "R.mkv", 100 * MIB, (0, 99))
    assert header_pieces(main, piece, offset=0) == [0, 1, 2, 99]


def test_header_with_large_pieces_is_the_first_and_last() -> None:
    piece = 16 * MIB
    main = tfile(0, "R.mkv", 4_000 * MIB, (0, 249))
    assert header_pieces(main, piece, offset=0) == [0, 249]


def test_header_of_a_file_starting_mid_piece_spills_one_piece_further() -> None:
    """The first 3 MiB from byte 1000 of piece 0 end in piece 3, not piece 2."""
    piece = 1 * MIB
    nfo = tfile(0, "R/R.nfo", 1000, (0, 0))
    size = 100 * MIB
    main = tfile(1, "R/R.mkv", size, (0, (1000 + size - 1) // piece))
    files = [nfo, main]

    offset = file_offset(files, main)

    assert offset == 1000
    assert header_pieces(main, piece, offset=offset) == [0, 1, 2, 3, 100]


def test_a_file_starting_exactly_on_a_boundary_needs_no_extra_piece() -> None:
    piece = 1 * MIB
    first = tfile(0, "R/a.nfo", 2 * MIB, (0, 1))
    main = tfile(1, "R/R.mkv", 10 * MIB, (2, 11))
    assert header_pieces(main, piece, offset=file_offset([first, main], main)) == [2, 3, 4, 11]


def test_an_offset_that_disagrees_with_piece_range_falls_back_to_the_worst_case() -> None:
    """Padding files are not listed, so summed sizes can be wrong; then assume the
    file begins on the last byte of its first piece."""
    piece = 1 * MIB
    main = tfile(1, "R/R.mkv", 10 * MIB, (5, 15))
    assert header_pieces(main, piece, offset=123) == [5, 6, 7, 8, 15]


def test_a_file_smaller_than_the_header_needs_all_its_pieces() -> None:
    piece = 1 * MIB
    main = tfile(0, "R.mkv", 2 * MIB, (0, 1))
    assert header_pieces(main, piece, offset=0) == [0, 1]
    assert HEADER_BYTES > 2 * MIB


def test_a_file_inside_one_piece_needs_only_that_piece() -> None:
    main = tfile(0, "R.mkv", 1000, (7, 7))
    assert header_pieces(main, 4 * MIB, offset=7 * 4 * MIB) == [7]


def test_header_plan_counts_complete_pieces() -> None:
    plan = HeaderPlan(
        file=tfile(0, "R.mkv", 100 * MIB, (0, 99)),
        file_path="E:\\R.mkv",
        folder="E:\\",
        pieces=(0, 1, 2, 99),
        piece_size=MIB,
    )
    states = [2, 2, 1] + [0] * 96 + [2]
    assert plan.complete(states) == 3
    assert plan.have_bytes(states) == 3 * MIB
    assert plan.need_bytes == 4 * MIB
    assert not plan.ready(states)
    assert plan.ready([2] * 100)
    assert not plan.ready([2, 2])  # a short pieceStates answer never reads as done


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def test_a_windows_file_path_keeps_qbittorrents_backslashes() -> None:
    assert (
        paths.join("E:\\Movies\\", "Some.Release.2024/Some.Release.2024.mkv")
        == "E:\\Movies\\Some.Release.2024\\Some.Release.2024.mkv"
    )


def test_a_forward_slash_windows_path_stays_forward() -> None:
    assert paths.join("E:/Movies", "R/R.mkv") == "E:/Movies/R/R.mkv"


def test_a_posix_path_is_joined_with_slashes() -> None:
    assert paths.join("/data/movies/", "R/R.mkv") == "/data/movies/R/R.mkv"


def test_a_multi_file_release_folder_is_the_torrents_own_folder() -> None:
    assert (
        paths.release_folder("E:\\Movies", "Some.Release.2024/Some.Release.2024.mkv",
                             multi_file=True)
        == "E:\\Movies\\Some.Release.2024"
    )


def test_a_nested_main_file_still_scans_the_release_folder() -> None:
    assert (
        paths.release_folder("E:\\Movies", "R/Video/R.mkv", multi_file=True) == "E:\\Movies\\R"
    )


def test_a_single_file_torrent_scans_the_files_own_directory() -> None:
    assert paths.release_folder("E:\\Movies\\", "R.mkv", multi_file=False) == "E:\\Movies"


def test_a_single_file_at_a_drive_root_keeps_the_root_separator() -> None:
    assert paths.release_folder("E:\\", "R.mkv", multi_file=False) == "E:\\"


def sections() -> list[PlexLibrarySection]:
    return [
        PlexLibrarySection("1", "Movies", "movie", False, locations=("E:\\Movies",)),
        PlexLibrarySection("2", "TV", "show", False, locations=("F:/TV", "G:\\More TV\\")),
        PlexLibrarySection("3", "Media", "movie", False, locations=("H:\\Media",)),
        PlexLibrarySection("4", "Kids", "movie", False, locations=("H:\\Media\\Kids",)),
    ]


def test_the_section_is_the_one_whose_folder_holds_the_path() -> None:
    found = paths.section_for_path(sections(), "E:\\Movies\\Some.Release.2024")
    assert found is not None and found.id == "1"


def test_section_matching_ignores_case_and_slash_direction() -> None:
    assert paths.section_for_path(sections(), "f:\\tv\\Show.S01E01").id == "2"  # type: ignore[union-attr]
    assert paths.section_for_path(sections(), "G:/more tv/Show").id == "2"  # type: ignore[union-attr]


def test_the_deepest_holding_folder_wins() -> None:
    assert paths.section_for_path(sections(), "H:\\Media\\Kids\\Film").id == "4"  # type: ignore[union-attr]
    assert paths.section_for_path(sections(), "H:\\Media\\Film").id == "3"  # type: ignore[union-attr]


def test_a_folder_only_sharing_a_prefix_is_not_inside() -> None:
    """``E:\\Movies2`` is not in ``E:\\Movies``."""
    assert paths.section_for_path(sections(), "E:\\Movies2\\Film") is None


def test_no_section_holds_an_unrelated_path() -> None:
    assert paths.section_for_path(sections(), "Z:\\Downloads\\Film") is None


def test_plex_and_qbittorrent_spellings_of_a_file_are_the_same_file() -> None:
    assert paths.same_file("E:\\Movies\\R\\R.mkv", "e:/movies/r/R.MKV")
    assert not paths.same_file("E:\\Movies\\R\\R.mkv", "E:\\Movies\\R\\R2.mkv")
    assert not paths.same_file("", "")


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        ("Show.Name.S01E02.1080p.WEB-DL-GRP", "show"),
        ("Show Name S02 1080p WEB-DL", "show"),
        ("Show.Name.Season.3.Complete", "show"),
        ("Movie.2024.1080p.WEB-DL-GRP", "movie"),
        ("Se7en.1995.2160p.UHD", "movie"),
    ],
)
def test_a_release_kind_is_guessed_from_its_name(title: str, kind: str) -> None:
    assert guess_section_type(title) == kind
