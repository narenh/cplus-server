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


@dataclass
class EpubBook:
    title: str | None
    authors: list[str]
    language: str | None
    docs: list[EpubDoc]
    drm: bool = False

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
        manifest = {e.get("id"): e.get("href") for e in opf.iter() if _local(e) == "item"}
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
            docs.append(EpubDoc(href=href, blocks=parser.blocks))

    return EpubBook(title=title, authors=authors, language=language, docs=docs, drm=drm)


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
