"""Builders shared by the audiobook tests: tiny epubs, Plex payloads, sidecar files."""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

from cplus_align.protocol import AlignPaths, write_json

PLEX_SERVER_URL = "http://plex.local:32400"
SERVER_ID = "abc123"
ALBUM_KEY = "501"
PART_KEY = "/library/parts/9001/1690000000/file.mp3"

WORDS = (
    "In a hole in the ground there lived a hobbit. Not a nasty, dirty, wet hole, "
    "filled with the ends of worms and an oozy smell, nor yet a dry, bare, sandy hole "
    "with nothing in it to sit down on or to eat: it was a hobbit-hole, and that means comfort."
)

FONT_OBFUSCATION = """<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.idpf.org/2008/embedding"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/font.otf"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>"""

CONTENT_ENCRYPTION = """<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"
            xmlns:enc="http://www.w3.org/2001/04/xmlenc#">
  <enc:EncryptedData>
    <enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>
    <enc:CipherData><enc:CipherReference URI="OEBPS/ch1.xhtml"/></enc:CipherData>
  </enc:EncryptedData>
</encryption>"""


def make_epub(
    *,
    paragraphs: int = 30,
    language: str | None = "en",
    title: str = "The Hobbit",
    creator: str = "J. R. R. Tolkien",
    encryption: str | None = None,
    files: dict[str, str] | None = None,
) -> bytes:
    """A minimal valid epub: one chapter of ``paragraphs`` copies of :data:`WORDS`."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        lang = f"<dc:language>{language}</dc:language>" if language else ""
        archive.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator>{lang}</metadata>"
            '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="c1"/></spine></package>',
        )
        # Hard-wrapped mid-sentence, the way real epub sources often are.
        body = "".join(
            f"<p>{WORDS.replace(' ground ', ' ground\n')}</p>" for _ in range(paragraphs)
        )
        archive.writestr(
            "OEBPS/ch1.xhtml",
            '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
            f"<head><title>ignored</title></head><body><h1>An Unexpected Party</h1>{body}"
            "</body></html>",
        )
        if encryption:
            archive.writestr("META-INF/encryption.xml", encryption)
        for name, content in (files or {}).items():
            archive.writestr(name, content)
    return buf.getvalue()


def sections_payload() -> dict:
    return {
        "MediaContainer": {
            "Directory": [
                {"key": "1", "title": "Movies", "type": "movie", "hidden": 0},
                {"key": "7", "title": "Audiobooks", "type": "artist", "hidden": 0},
                {"key": "8", "title": "Music", "type": "artist", "hidden": 0},
            ]
        }
    }


def album_payload(rating_key: str = ALBUM_KEY, title: str = "The Hobbit") -> dict:
    return {
        "ratingKey": rating_key,
        "type": "album",
        "title": title,
        "parentTitle": "J. R. R. Tolkien",
        "librarySectionID": 7,
        "thumb": f"/library/metadata/{rating_key}/thumb/1690000000",
        "leafCount": 1,
        "year": 2012,
    }


def albums_payload(*albums: dict, total: int | None = None) -> dict:
    return {"MediaContainer": {"Metadata": list(albums), "totalSize": total or len(albums)}}


def children_payload(*, size: int = 600_000_000, part_id: int = 9001) -> dict:
    return {
        "MediaContainer": {
            "Metadata": [
                {
                    "ratingKey": "502",
                    "title": "The Hobbit",
                    "index": 1,
                    "parentIndex": 1,
                    "duration": 37_500_000,
                    "Media": [
                        {
                            "container": "mp3",
                            "Part": [
                                {
                                    "id": part_id,
                                    "key": f"/library/parts/{part_id}/1690000000/file.mp3",
                                    "size": size,
                                    "duration": 37_500_000,
                                    "container": "mp3",
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    }


def sidecar_up(paths: AlignPaths, *, status: str = "ready", runtime: str | None = None) -> None:
    """Make the sidecar look alive with its runtime in ``status``."""
    from cplus_align import install

    write_json(paths.heartbeat_file, {"t": time.time(), "engine": "idle"})
    write_json(
        paths.state_file,
        {"status": status, "runtime": runtime or install.runtime_id(), "disk": 2_300_000_000},
    )


def result_payload(*, sentences: int = 6) -> dict:
    """A finished alignment as the sidecar writes it."""
    sents = []
    for i in range(sentences):
        timed = i != 1  # sentence 1 was never read aloud
        sents.append(
            {
                "i": i,
                "sec": 0 if i < 3 else 1,
                "para": i // 2,
                "text": f"Sentence {i}.",
                "start": 10.0 * i if timed else None,
                "end": 10.0 * i + 4 if timed else None,
                "flags": [] if timed else ["unspoken"],
                **({"wps": 2.5, "score": -0.3} if timed else {}),
            }
        )
    return {
        "version": 1,
        "book": {"title": "The Hobbit", "authors": ["J. R. R. Tolkien"]},
        "audio": {"duration": 37500.0, "tracks": [{"n": 0, "offset": 0.0, "duration": 37499.9}]},
        "sections": [
            {
                "index": 0,
                "href": "a.xhtml",
                "title": "One",
                "start": 0.0,
                "end": 24.0,
                "sentences": 3,
            },
            {
                "index": 1,
                "href": "b.xhtml",
                "title": "Two",
                "start": 30.0,
                "end": 54.0,
                "sentences": 3,
            },
        ],
        "sentences": sents,
        "extra_audio": [],
        "stats": {"sentences": sentences, "aligned": sentences - 1, "unspoken": 1},
    }


def write_result(folder: Path, payload: dict | None = None) -> None:
    (folder / "result.json").write_text(json.dumps(payload or result_payload()))
