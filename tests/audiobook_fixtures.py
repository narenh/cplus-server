"""Builders shared by the audiobook tests: Plex payloads and alignment documents."""

from __future__ import annotations

PLEX_SERVER_URL = "http://plex.local:32400"
SERVER_ID = "abc123"
ALBUM_KEY = "501"
PART_KEY = "/library/parts/9001/1690000000/file.mp3"


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


def result_payload(*, sentences: int = 6) -> dict:
    """A finished ``version: 1`` alignment, as the aligner writes it."""
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
