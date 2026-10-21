"""Paths as qBittorrent and Plex see them, and which Plex library holds one.

cplus never touches the disk. qBittorrent and Plex run on the same machine and
see the same paths (``E:\\Movies\\Some.Release.2024\\file.mkv``), so a path is
only ever *passed along*: built from what qBittorrent reports, handed to Plex
to scan, and compared with what Plex reports back. Nothing here maps one
machine's paths onto another's.

Two kinds of comparison happen, and they want different things. A path sent
to Plex is kept in qBittorrent's own spelling — its separators, its case —
because that is what the filesystem underneath both of them calls it. A path
*compared* with one from Plex is normalised first (case folded, either slash
direction), because Windows treats ``E:\\Movies`` and ``e:/movies`` as the same
folder and the two programs are not obliged to spell it alike.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from ..plex.client import PlexLibrarySection

_DRIVE = re.compile(r"^[A-Za-z]:")
_SEPARATORS = re.compile(r"[\\/]+")


def is_windows_path(path: str) -> bool:
    return bool(_DRIVE.match(path)) or path.startswith("\\\\") or "\\" in path


def _separator(path: str) -> str:
    return "\\" if "\\" in path else "/"


def join(save_path: str, name: str) -> str:
    """``name`` (relative, as ``torrents/files`` gives it) under ``save_path``.

    Uses ``save_path``'s own separator throughout, so a Windows save path with
    backslashes gets a backslashed file path even if qBittorrent wrote the
    relative part with forward slashes. On a non-Windows path a backslash is a
    legal file name character and is left alone.
    """
    sep = _separator(save_path)
    rel = name
    if is_windows_path(save_path):
        rel = _SEPARATORS.sub(lambda _: sep, name)
    base = save_path.rstrip("\\/")
    return base + sep + rel.lstrip("\\/")


def parent(path: str) -> str:
    """``path`` without its last component, keeping a drive root's separator."""
    sep = _separator(path)
    trimmed = path.rstrip("\\/")
    cut = max(trimmed.rfind("\\"), trimmed.rfind("/"))
    if cut < 0:
        return trimmed
    head = trimmed[:cut]
    if _DRIVE.fullmatch(head) or head == "":
        return head + sep
    return head


def release_folder(save_path: str, file_name: str, *, multi_file: bool) -> str:
    """The folder Plex should scan for a torrent's main file.

    A multi-file torrent keeps its files under its own folder, the first
    component of each file's relative name: that folder is the release, and
    scanning it picks up the main file however deep it sits. A single-file
    torrent has no folder of its own, so its file's directory is scanned.
    """
    parts = [p for p in _SEPARATORS.split(file_name) if p]
    if multi_file and len(parts) > 1:
        return join(save_path, parts[0])
    return parent(join(save_path, file_name))


def normalise(path: str) -> str:
    """``path`` for comparison only: forward slashes, case folded, no trailing slash."""
    return _SEPARATORS.sub("/", path).rstrip("/").casefold()


def is_within(path: str, folder: str) -> bool:
    """Whether ``path`` is ``folder`` or inside it, on component boundaries."""
    p, f = normalise(path), normalise(folder)
    if not f:
        return False
    return p == f or p.startswith(f + "/")


def section_for_path(
    sections: Sequence[PlexLibrarySection], path: str
) -> PlexLibrarySection | None:
    """The library whose folder holds ``path``; the deepest one if several do.

    A library with ``E:\\Media`` and another with ``E:\\Media\\Movies`` both
    "hold" a film under the second, and only the second is right.
    """
    best: tuple[int, PlexLibrarySection] | None = None
    for section in sections:
        for location in section.locations:
            if is_within(path, location):
                depth = len(normalise(location))
                if best is None or depth > best[0]:
                    best = (depth, section)
    return best[1] if best else None


def same_file(a: str, b: str) -> bool:
    return normalise(a) == normalise(b) and bool(a)


def any_same_file(paths: Iterable[str], target: str) -> bool:
    return any(same_file(p, target) for p in paths)
