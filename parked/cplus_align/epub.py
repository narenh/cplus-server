"""Reading an epub: its text in reading order, and what it says about itself.

Standard library only — cplus-service runs :func:`inspect_epub` on an upload
before anything is queued, and the pipeline reads the text with
:func:`read_epub`. Both go through the same parser so what the admin is told
about a book ("41,000 words, English") is what the aligner then works on.

Paragraphs are split on block tags, never on newlines: epub sources are often
hard-wrapped mid-sentence.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import IO
from urllib.parse import unquote

BLOCK = {
    "p",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "blockquote",
    "tr",
    "section",
    "article",
    "pre",
    "dt",
    "dd",
    "caption",
    "figcaption",
    "table",
    "ul",
    "ol",
    "body",
}
SKIP = {"script", "style", "head", "title"}

#: The largest single file read out of the zip. A content document is tens of
#: KB; this only exists so a crafted archive cannot claim gigabytes.
MAX_MEMBER_BYTES = 64 * 1024 * 1024

#: Encryption algorithms that are font obfuscation, not DRM. DRM-free books from
#: many publishers list their embedded fonts in ``encryption.xml`` under one of
#: these, so the file's presence alone proves nothing.
FONT_OBFUSCATION = {
    "http://www.idpf.org/2008/embedding",
    "http://ns.adobe.com/pdf/enc#RC",
}

#: Files that only DRM schemes write (Adobe ADEPT, Apple FairPlay).
DRM_MARKERS = ("META-INF/rights.xml", "META-INF/sinf.xml")

WORD_SPLIT = re.compile(r"[\s\-‐-―]+")


class EpubError(ValueError):
    """The file is not an epub this service can read."""


class _Blocks(HTMLParser):
    """Collect text blocks (paragraphs, headings); line wraps inside a block collapse."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, str]] = []
        self.buf: list[str] = []
        self.skip = 0
        self.cur = ""
        #: element id -> index of the block it opens in (where a TOC entry can point)
        self.ids: dict[str, int] = {}

    def _flush(self) -> None:
        text = " ".join("".join(self.buf).split())
        if text:
            self.blocks.append((self.cur, text))
        self.buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP:
            self.skip += 1
        elif tag == "br":
            self.buf.append(" ")
        elif tag in BLOCK:
            self._flush()
            self.cur = tag
        if tag not in SKIP:
            for name, value in attrs:
                if name == "id" and value:
                    self.ids.setdefault(value, len(self.blocks))

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in BLOCK:
            self._flush()
            self.cur = ""

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


@dataclass
class EpubDoc:
    """One spine item: its href and its text blocks as ``(tag, text)``."""

    href: str
    blocks: list[tuple[str, str]]
    #: The document's path inside the archive, and where each ``id`` in it falls
    #: (as an index into ``blocks``): what a table-of-contents entry points at.
    path: str = ""
    anchors: dict[str, int] = field(default_factory=dict)


@dataclass
class TocEntry:
    """One line of the epub's table of contents, resolved to a file and maybe an anchor in it."""

    title: str
    path: str
    fragment: str | None
    #: The title of the entry this one is nested under, if any ("Book One" over its chapters).
    parent: str | None = None


@dataclass
class EpubBook:
    title: str | None
    authors: list[str]
    language: str | None
    docs: list[EpubDoc]
    drm: bool = False
    toc: list[TocEntry] = field(default_factory=list)

    @property
    def words(self) -> int:
        return sum(
            len([t for t in WORD_SPLIT.split(text) if t])
            for doc in self.docs
            for _tag, text in doc.blocks
        )


@dataclass
class EpubInfo:
    """What an upload is, for the admin to see before anything is queued."""

    title: str | None
    authors: list[str]
    language: str | None
    words: int
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise EpubError(f"{name} is implausibly large ({info.file_size} bytes)")
    return archive.read(info)


_XML_ENCODING = re.compile(rb"""<\?xml[^>]*encoding=["']([A-Za-z0-9._-]+)["']""")
_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?([A-Za-z0-9._-]+)""", re.I)


def _decode(raw: bytes) -> str:
    """Decode a content document by what it declares, falling back to UTF-8."""
    head = raw[:1024]
    match = _XML_ENCODING.search(head) or _META_CHARSET.search(head)
    if match:
        try:
            return raw.decode(match.group(1).decode("ascii"), errors="replace")
        except LookupError:
            pass
    return raw.decode("utf-8", errors="replace")


def _has_drm(archive: zipfile.ZipFile) -> bool:
    names = set(archive.namelist())
    if any(marker in names for marker in DRM_MARKERS):
        return True
    if "META-INF/encryption.xml" not in names:
        return False
    try:
        root = ET.fromstring(_read(archive, "META-INF/encryption.xml"))
    except ET.ParseError:
        return True  # an unreadable manifest of encrypted files is not reassuring
    for data in (e for e in root.iter() if _local(e) == "EncryptedData"):
        algorithms = {
            m.get("Algorithm", "") for m in data.iter() if _local(m) == "EncryptionMethod"
        }
        if algorithms and algorithms <= FONT_OBFUSCATION:
            continue
        return True
    return False


#: More entries than this is not a table of contents; it is a crafted archive.
MAX_TOC_ENTRIES = 5000


def _target(base: str, href: str) -> tuple[str, str | None]:
    """An href from the package or nav document -> (archive path, fragment)."""
    path, _, fragment = href.partition("#")
    return posixpath.normpath(posixpath.join(base, unquote(path))), unquote(fragment) or None


def _toc_ncx(archive: zipfile.ZipFile, path: str) -> list[TocEntry]:
    """An EPUB2 ``toc.ncx``: nested ``navPoint`` elements."""
    root = ET.fromstring(_read(archive, path))
    base = posixpath.dirname(path)
    out: list[TocEntry] = []

    def walk(node: ET.Element, parent: str | None) -> None:
        for point in node:
            if _local(point) != "navPoint":
                continue
            label = " ".join(
                next((t.text or "" for t in point.iter() if _local(t) == "text"), "").split()
            )
            src = next((c.get("src") for c in point if _local(c) == "content"), None)
            if src and label and len(out) < MAX_TOC_ENTRIES:
                out.append(TocEntry(label, *_target(base, src), parent))
            walk(point, label or parent)

    for nav_map in (e for e in root.iter() if _local(e) == "navMap"):
        walk(nav_map, None)
    return out


def _toc_nav(archive: zipfile.ZipFile, path: str) -> list[TocEntry]:
    """An EPUB3 navigation document: the ``<nav epub:type="toc">`` list."""
    root = ET.fromstring(_read(archive, path))
    base = posixpath.dirname(path)
    out: list[TocEntry] = []
    navs = [n for n in root.iter() if _local(n) == "nav"]
    nav = next(
        (n for n in navs if any(k.endswith("type") and "toc" in v for k, v in n.attrib.items())),
        navs[0] if navs else None,
    )

    def walk(ol: ET.Element, parent: str | None) -> None:
        for item in ol:
            if _local(item) != "li":
                continue
            link = next((c for c in item if _local(c) in ("a", "span")), None)
            label = " ".join("".join(link.itertext()).split()) if link is not None else ""
            href = link.get("href") if link is not None and _local(link) == "a" else None
            if href and label and len(out) < MAX_TOC_ENTRIES:
                out.append(TocEntry(label, *_target(base, href), parent))
            sub = next((c for c in item if _local(c) == "ol"), None)
            if sub is not None:
                walk(sub, label or parent)

    if nav is not None:
        for ol in (c for c in nav if _local(c) == "ol"):
            walk(ol, None)
    return out


def _read_toc(
    archive: zipfile.ZipFile, opf: ET.Element, items: dict[str | None, ET.Element], base: str
) -> list[TocEntry]:
    """The table of contents: the EPUB3 nav document if there is one, else the NCX.

    Never raises. A missing or broken table of contents only means the book gets its
    chapters from the headings that open each file instead.
    """
    nav = next((e for e in items.values() if "nav" in (e.get("properties") or "").split()), None)
    ncx_id = next((e.get("toc") for e in opf.iter() if _local(e) == "spine" and e.get("toc")), None)
    ncx = items.get(ncx_id)
    if ncx is None:
        ncx = next(
            (e for e in items.values() if e.get("media-type") == "application/x-dtbncx+xml"), None
        )
    for item, reader in ((nav, _toc_nav), (ncx, _toc_ncx)):
        href = item.get("href") if item is not None else None
        if not href:
            continue
        try:
            entries = reader(archive, posixpath.normpath(posixpath.join(base, unquote(href))))
        except (KeyError, ET.ParseError, EpubError):
            continue
        if entries:
            return entries
    return []


def read_epub(source: str | Path | IO[bytes]) -> EpubBook:
    """Parse an epub into its spine's text blocks. Raises :class:`EpubError`."""
    try:
        archive = zipfile.ZipFile(source)
    except (zipfile.BadZipFile, OSError) as exc:
        raise EpubError("not a zip archive, so not an epub") from exc

    with archive:
        try:
            container = ET.fromstring(_read(archive, "META-INF/container.xml"))
            opf_path = next(e.get("full-path") for e in container.iter() if _local(e) == "rootfile")
            if not opf_path:
                raise StopIteration
            opf = ET.fromstring(_read(archive, opf_path))
        except (KeyError, StopIteration, ET.ParseError) as exc:
            raise EpubError("missing or unreadable package document") from exc

        drm = _has_drm(archive)
        base = posixpath.dirname(opf_path)
        items = {e.get("id"): e for e in opf.iter() if _local(e) == "item"}
        manifest = {item_id: e.get("href") for item_id, e in items.items()}
        title = next((e.text.strip() for e in opf.iter() if _local(e) == "title" and e.text), None)
        authors = [
            e.text.strip()
            for e in opf.iter()
            if _local(e) == "creator" and e.text and e.text.strip()
        ]
        language = next(
            (e.text.strip() for e in opf.iter() if _local(e) == "language" and e.text), None
        )

        docs: list[EpubDoc] = []
        for ref in (e for e in opf.iter() if _local(e) == "itemref"):
            href = manifest.get(ref.get("idref"))
            if not href or not href.lower().endswith((".xhtml", ".html", ".htm", ".xml")):
                continue
            full = posixpath.normpath(posixpath.join(base, unquote(href)))
            try:
                raw = _read(archive, full)
            except KeyError:
                continue
            parser = _Blocks()
            parser.feed(_decode(raw))
            parser.close()
            docs.append(EpubDoc(href=href, blocks=parser.blocks, path=full, anchors=parser.ids))
        toc = _read_toc(archive, opf, items, base)

    return EpubBook(title=title, authors=authors, language=language, docs=docs, drm=drm, toc=toc)


#: Below this many words there is nothing worth aligning — or the text is
#: encrypted and decoded to noise.
MIN_WORDS = 1000


def is_english(language: str | None) -> bool:
    """English, or unstated (many epubs omit it, and the audio check still runs)."""
    if not language:
        return True
    return language.strip().lower().replace("_", "-").split("-")[0] in {"en", "eng"}


def inspect_epub(source: str | Path | IO[bytes]) -> EpubInfo:
    """Everything an upload can be refused for without touching the audio."""
    try:
        book = read_epub(source)
    except EpubError as exc:
        return EpubInfo(None, [], None, 0, problems=[f"This isn't a readable epub: {exc}."])

    problems: list[str] = []
    words = book.words
    if book.drm:
        problems.append("This epub is DRM-protected. Only DRM-free epubs can be aligned.")
    elif words < MIN_WORDS:
        problems.append(f"This epub has almost no readable text ({words} words).")
    if not is_english(book.language):
        problems.append(f"This epub is in '{book.language}'. Only English books can be aligned.")
    return EpubInfo(book.title, book.authors, book.language, words, problems)
