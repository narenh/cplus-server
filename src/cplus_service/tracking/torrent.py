"""The arithmetic of a grabbed torrent: which one it is, which file matters,
and which of its pieces Plex needs before it can scan that file.

All pure functions over what qBittorrent reports, so every rule here is tested
without a qBittorrent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..qbittorrent.client import PIECE_COMPLETE, Torrent, TorrentFile

#: How much of the start of the main file must be on disk before Plex is asked
#: to scan it. Plex reads the container header to identify a file; a few MB
#: covers every MKV and MP4 header seen in practice (an MP4 with its index at
#: the *end* is the case the last piece covers, see :func:`needs_last_piece`).
HEADER_BYTES = 3 * 1024 * 1024

#: Containers whose start alone identifies them to Plex: Matroska keeps its
#: segment info (duration included) and track list at the front, so a scan needs
#: nothing from the end. Waiting for the last piece too cost the most time in
#: the first live runs (a 16 MB last piece, fetched alongside the first).
HEAD_ONLY_EXTENSIONS = frozenset({"mkv", "mk3d", "webm"})

#: What counts as a video file when choosing the main file of a torrent.
VIDEO_EXTENSIONS = frozenset(
    {
        "mkv", "mp4", "m4v", "avi", "ts", "m2ts", "mts", "mov", "wmv",
        "mpg", "mpeg", "webm", "vob", "flv", "ogm", "divx",
    }
)

#: A name match shorter than this (after normalising) is too weak to trust as
#: a *partial* match — see :func:`match_by_name`.
MIN_PARTIAL_MATCH = 12

_NOT_ALNUM = re.compile(r"[^0-9a-z]+")


def normalise_name(name: str) -> str:
    """Lower-case with every run of punctuation and whitespace removed.

    ``Movie.2024.1080p.WEB-DL`` and ``Movie 2024 1080p WEB DL`` are the same
    release to a person and the same string here. Indexers and torrent files
    disagree on exactly these characters far more often than on anything else.
    """
    return _NOT_ALNUM.sub("", name.lower())


def match_by_hash(torrents: Iterable[Torrent], hashes: Iterable[str]) -> Torrent | None:
    """The torrent answering to any of ``hashes`` (v1 or v2), if there is one."""
    wanted = {h.lower() for h in hashes if h}
    if not wanted:
        return None
    for torrent in torrents:
        if torrent.hashes & wanted:
            return torrent
    return None


def match_by_name(
    torrents: Sequence[Torrent], title: str, *, added_since: int | None
) -> Torrent | None:
    """The newest torrent added since ``added_since`` whose name is ``title``.

    Names are compared after :func:`normalise_name`. An exact match wins; failing
    that, a torrent whose name contains the title or is contained by it (an
    indexer that appends ``[TGx]``, or a torrent named without the group) is
    accepted when the shorter of the two is long enough to mean something.

    ``added_since`` is a Unix time in qBittorrent's clock; ``None`` considers
    every torrent regardless of age.
    """
    target = normalise_name(title)
    if not target:
        return None
    candidates = [
        t for t in torrents if added_since is None or t.added_on >= added_since
    ]
    candidates.sort(key=lambda t: t.added_on, reverse=True)

    for torrent in candidates:
        if normalise_name(torrent.name) == target:
            return torrent
    for torrent in candidates:
        name = normalise_name(torrent.name)
        shorter = min(len(name), len(target))
        if shorter >= MIN_PARTIAL_MATCH and (name in target or target in name):
            return torrent
    return None


def extension(name: str) -> str:
    base = re.split(r"[\\/]", name)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def main_video_file(files: Sequence[TorrentFile]) -> TorrentFile | None:
    """The largest video file in the torrent — the film or episode itself.

    Samples, extras and subtitles are all smaller, so size alone picks the
    right one without a list of names to exclude.
    """
    videos = [f for f in files if extension(f.name) in VIDEO_EXTENSIONS and f.size > 0]
    if not videos:
        return None
    return max(videos, key=lambda f: (f.size, -f.index))


def file_offset(files: Sequence[TorrentFile], target: TorrentFile) -> int:
    """Where ``target`` starts in the torrent's byte stream: the size of every
    file before it, in torrent order."""
    offset = 0
    for f in sorted(files, key=lambda f: f.index):
        if f.index == target.index:
            break
        offset += f.size
    return offset


def needs_last_piece(name: str) -> bool:
    """Whether Plex needs the end of ``name`` too before it can scan it.

    True for anything but Matroska: an MP4 or MOV may keep its index (``moov``)
    at the end, and for the rarer containers waiting is the safe side.
    """
    return name.rsplit(".", 1)[-1].lower() not in HEAD_ONLY_EXTENSIONS


def header_pieces(
    target: TorrentFile,
    piece_size: int,
    *,
    offset: int | None = None,
    header_bytes: int = HEADER_BYTES,
    include_last: bool = True,
) -> list[int]:
    """The pieces holding the first ``header_bytes`` of ``target``, plus its last
    piece unless ``include_last`` is false.

    A file rarely starts on a piece boundary in a multi-file torrent: its first
    piece is shared with the tail of the file before it, so the first 3 MB of
    the file can spill one piece further than ``3 MB / piece_size`` suggests.
    ``offset`` (the file's start in the torrent, from :func:`file_offset`) says
    exactly where it starts. It is only trusted when it agrees with
    qBittorrent's own ``piece_range`` — padding files, which qBittorrent does not
    list, would throw it off — and otherwise the worst case is assumed: that the
    file starts on the last byte of its first piece. That costs at most one
    extra piece, never a scan of a file whose header is missing.
    """
    if piece_size <= 0:
        raise ValueError("piece_size must be positive")
    first, last = target.piece_range
    size = max(target.size, 1)

    agrees = (
        offset is not None
        and offset // piece_size == first
        and (offset + size - 1) // piece_size == last
    )
    start = offset if agrees and offset is not None else first * piece_size + piece_size - 1

    wanted = min(header_bytes, size)
    header_last = min(last, (start + wanted - 1) // piece_size)
    pieces = list(range(first, header_last + 1))
    if include_last and last not in pieces:
        pieces.append(last)
    return pieces


@dataclass(frozen=True)
class HeaderPlan:
    """What the header wait is waiting for, worked out once metadata exists."""

    file: TorrentFile
    file_path: str
    """The main file's full path as qBittorrent's machine sees it."""
    folder: str
    """The folder Plex is asked to scan."""
    pieces: tuple[int, ...]
    piece_size: int

    @property
    def need_bytes(self) -> int:
        return len(self.pieces) * self.piece_size

    def have_bytes(self, states: Sequence[int]) -> int:
        return self.complete(states) * self.piece_size

    def complete(self, states: Sequence[int]) -> int:
        return sum(1 for p in self.pieces if p < len(states) and states[p] == PIECE_COMPLETE)

    def ready(self, states: Sequence[int]) -> bool:
        return self.complete(states) == len(self.pieces)
