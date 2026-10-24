"""Chapters for a switcher: clean titles from the epub's table of contents, timed by the alignment.

The result is one list, contiguous over the whole audio and over the whole sentence
stream, that clients show as the book's chapters. Titles come from the epub's table
of contents (or, when it has none, from the headings that open each file); a table of
contents entry becomes a chapter only if it has real spoken audio, so a title page, a
"Book One" divider or the copyright page do not clutter the list — they merge into a
neighbour, which is why a book's opening and closing credits belong to its first and
last chapters.

Standard library only: this runs in the aligner sidecar, and is tested without numpy.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from typing import Any

from ..epub import EpubBook

#: A table of contents entry with less spoken audio than this merges into a neighbour.
MIN_CHAPTER_SECONDS = 90.0

_NUMBER = r"(?:[ivxlcdm]+|\d+)"
_DASHES = "\\-\u2013\u2014"
_CHAPTER = re.compile(rf"^\s*chapter\s+({_NUMBER})\b\s*[:.{_DASHES}]*\s*(.*)$", re.I)
_PART = re.compile(
    r"^\s*((?:book|part)\s+(?:[ivxlcdm]+|\d+|one|two|three|four|five|six|seven|eight|nine|ten))"
    rf"\b\s*[:.{_DASHES}]*\s*(.*)$",
    re.I,
)
#: Words a title keeps in lower case (unless first or last).
_SMALL = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "nor", "of", "on",
    "or", "the", "to", "with",
}  # fmt: skip


@dataclass
class TocPoint:
    """A table of contents entry, located in the sentence stream."""

    title: str
    doc: int  # index of the spine document
    blk: int  # index of the block within it
    parent: str | None


def smart_title(text: str) -> str:
    """ALL-CAPS headings become Title Case; anything already mixed-case is left alone."""
    text = " ".join(text.split())
    letters = [c for c in text if c.isalpha()]
    if not letters or not all(c.isupper() for c in letters):
        return text

    def cap(word: str) -> str:
        return word[:1].upper() + word[1:]

    words = text.lower().split(" ")
    out = []
    for i, word in enumerate(words):
        if "-" in word:
            out.append("-".join(cap(p) for p in word.split("-")))
        elif i in (0, len(words) - 1) or word not in _SMALL:
            out.append(cap(word))
        else:
            out.append(word)
    return " ".join(out)


def roman_or_int(token: str) -> int:
    if token.isdigit():
        return int(token)
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = prev = 0
    for char in reversed(token.lower()):
        value = values[char]
        total += -value if value < prev else value
        prev = max(prev, value)
    return total


def resolve_toc(epub: EpubBook) -> list[TocPoint]:
    """The table of contents placed in reading order, one point per position.

    An entry that points at an anchor inside a file lands at the block that anchor opens;
    entries that point at the same place collapse to the first. An epub without a usable
    table of contents falls back to the headings that open each spine document.
    """
    where = {doc.path: i for i, doc in enumerate(epub.docs) if doc.path}
    points = []
    for entry in epub.toc:
        i = where.get(entry.path)
        if i is None:
            continue
        blk = epub.docs[i].anchors.get(entry.fragment, 0) if entry.fragment else 0
        points.append(TocPoint(entry.title, i, blk, entry.parent))
    if not points:
        for i, doc in enumerate(epub.docs):
            heads = []
            for tag, text in doc.blocks[:4]:
                if tag not in ("h1", "h2", "h3"):
                    break
                heads.append(text)
            if heads:
                points.append(TocPoint(": ".join(heads), i, 0, None))
    points.sort(key=lambda p: (p.doc, p.blk))
    return [
        p
        for k, p in enumerate(points)
        if k == 0 or (p.doc, p.blk) != (points[k - 1].doc, points[k - 1].blk)
    ]


def _single(title: str | None, count: int, duration: float) -> list[dict[str, Any]]:
    name = title or "Full audio"
    return [
        {
            "index": 0, "title": name, "label": None, "number": None, "part": None,
            "display": name, "start": 0.0, "end": round(duration, 2), "sentences": [0, count],
        }
    ]  # fmt: skip


def derive_chapters(
    toc: list[TocPoint],
    where: list[tuple[int, int]],
    sentences: list[dict[str, Any]],
    duration: float,
    title: str | None = None,
) -> list[dict[str, Any]]:
    """Chapters from the table of contents and the aligned sentences.

    ``where[i]`` is the ``(document, block)`` sentence ``i`` came from, ``sentences`` the
    aligned sentences (with ``start``/``end``, ``None`` when not spoken), ``duration`` the
    audio's length. Each chapter has ``title`` (clean and title-cased), ``label``/``number``
    ("Chapter 6", 6), ``part`` ("Book Two"), ``display`` (what to show: the part, the number
    and the title together), ``start``/``end`` and ``sentences`` as a half-open ``[i0, i1)``.
    """
    keys = [(p.doc, p.blk) for p in toc]
    groups: list[list[int]] = [[] for _ in toc]
    for k, position in enumerate(where):
        j = bisect.bisect_right(keys, position) - 1
        if j >= 0:
            groups[j].append(k)

    def spoken(j: int) -> float:
        return sum(
            sentences[k]["end"] - sentences[k]["start"]
            for k in groups[j]
            if sentences[k]["start"] is not None
        )

    big = [j for j in range(len(toc)) if spoken(j) >= MIN_CHAPTER_SECONDS]
    if not big:
        return _single(title, len(sentences), duration)

    # Which "Book One" / "Part Two" each entry falls under: a divider entry, or nesting.
    part: str | None = None
    parts: list[str | None] = []
    for point in toc:
        divider = _PART.match(point.title)
        if divider:
            rest = f": {divider.group(2)}" if divider.group(2) else ""
            part = smart_title(divider.group(1) + rest)
        elif point.parent and _PART.match(point.parent):
            part = smart_title(point.parent)
        parts.append(part)

    # Every entry belongs to the next chapter (or, past the last, the previous one); a
    # chapter starts at the earliest sentence of any entry that belongs to it.
    first: dict[int, int] = {}
    for j, group in enumerate(groups):
        if group:
            owner = next((b for b in big if b >= j), big[-1])
            first[owner] = min(first.get(owner, group[0]), group[0])
    starts = [0] + [first[j] for j in big[1:]]

    chapters: list[dict[str, Any]] = []
    for n, j in enumerate(big):
        i0 = starts[n]
        i1 = starts[n + 1] if n + 1 < len(big) else len(sentences)
        times = [sentences[k]["start"] for k in range(i0, i1) if sentences[k]["start"] is not None]
        match = _CHAPTER.match(toc[j].title)
        number = roman_or_int(match.group(1)) if match else None
        name = smart_title(match.group(2)) if match and match.group(2) else None
        label = f"Chapter {number}" if number else None
        chapter_title = name or label or smart_title(toc[j].title)
        heading = f"{label}: {name}" if label and name else chapter_title
        is_divider = bool(_PART.match(toc[j].title))
        chapters.append(
            {
                "index": n,
                "title": chapter_title,
                "label": label,
                "number": number,
                "part": parts[j],
                "display": f"{parts[j]} \u00b7 {heading}"
                if parts[j] and not is_divider
                else heading,
                "start": 0.0 if n == 0 else round(min(times), 2),
                "end": None,
                "sentences": [i0, i1],
            }
        )
    for n, chapter in enumerate(chapters):
        chapter["end"] = chapters[n + 1]["start"] if n + 1 < len(chapters) else round(duration, 2)
    return chapters


def apply_sections(
    sentences: list[dict[str, Any]], chapters: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Point each sentence at its chapter (``sec``) and return the ``sections`` list clients read.

    ``sections`` is what cplus-service stores and serves as the book's chapters:
    ``index``, ``title`` (the display string), ``start``, ``end`` and ``sentences`` (a
    count) are what it keeps; the rest (``range``, ``label``, ``number``, ``part``,
    ``name``) describes the chapter for anything that wants more than the title.
    """
    for chapter in chapters:
        for i in range(*chapter["sentences"]):
            sentences[i]["sec"] = chapter["index"]
    return [
        {
            "index": c["index"],
            "title": c["display"],
            "start": c["start"],
            "end": c["end"],
            "sentences": c["sentences"][1] - c["sentences"][0],
            "range": c["sentences"],
            "label": c["label"],
            "number": c["number"],
            "part": c["part"],
            "name": c["title"],
        }
        for c in chapters
    ]
