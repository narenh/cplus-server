"""A fixed cast of releases for the admin UI's profile preview.

The preview exists to answer "what do these rules actually do?" before an admin
saves anything, and it needs candidates to answer with. Real Prowlarr results
are one way (and the preview offers that too), but they need Prowlarr
configured, a title in mind, and a round trip — none of which an admin has on
the page where they are first shaping a profile.

So this is the default: one invented film in the shapes a real search comes
back in. It is deliberately *awkward* — a 4K remux far too big to stream, a 4K
WEB copy that is the sensible pick, a small 1080p encode, an unlabelled file, a
CAM, a REPACK and its base release — because a set where every rule agrees
teaches nothing. Between them these cover every filter and every choice
condition the builder can express.

Real titles, parsed by the real parser: what the preview shows about a release
is derived exactly as it would be for a live result, so a tag it displays is a
tag the engine will actually match on.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..release.models import BYTES_PER_GB, ParsedRelease
from ..release.parser import parse_prowlarr_result

#: Fixed so the preview is the same every time it is drawn: an admin comparing
#: two edits should see the rules change, not the ages.
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _sample(
    title: str,
    *,
    size_gb: float,
    seeders: int,
    days_old: int,
    indexer: str = "Example Indexer",
) -> ParsedRelease:
    return parse_prowlarr_result(
        {
            "title": title,
            "guid": f"sample:{title}",
            "indexerId": 1,
            "indexer": indexer,
            "size": int(size_gb * BYTES_PER_GB),
            "seeders": seeders,
            "publishDate": (_EPOCH - timedelta(days=days_old)).isoformat(),
            "protocol": "torrent",
        }
    )


#: Ordered the way an indexer might hand them over — that is, not helpfully.
#: A preview that only ever shows a correctly-ordered list would hide the one
#: thing worth seeing, which is the profile doing the ordering.
_SAMPLES: tuple[ParsedRelease, ...] = (
    _sample(
        "Example.Film.2024.2160p.UHD.BluRay.REMUX.DV.HDR10.TrueHD.7.1.Atmos-FRAME",
        size_gb=64.2,
        seeders=31,
        days_old=40,
    ),
    _sample(
        "Example.Film.2024.1080p.WEB-DL.DDP5.1.H.264-NTb",
        size_gb=7.4,
        seeders=280,
        days_old=52,
    ),
    _sample(
        "Example.Film.2024.2160p.WEB-DL.DV.HDR10+.DDP5.1.Atmos.HEVC-FLUX",
        size_gb=22.8,
        seeders=140,
        days_old=50,
    ),
    _sample(
        "Example.Film.2024.720p.HDTS.x264-NOGRP",
        size_gb=1.6,
        seeders=900,
        days_old=95,
    ),
    _sample(
        "Example.Film.2024.2160p.BluRay.x265.10bit.HDR.DTS-HD.MA.5.1-TERMiNAL",
        size_gb=18.1,
        seeders=64,
        days_old=30,
    ),
    _sample(
        "Example.Film.2024.1080p.BluRay.x264.DTS-X.7.1-CtrlHD",
        size_gb=13.9,
        seeders=45,
        days_old=28,
    ),
    _sample(
        "Example.Film.2024.REPACK.1080p.WEB-DL.DDP5.1.H.264-NTb",
        size_gb=7.5,
        seeders=210,
        days_old=48,
    ),
    _sample(
        "Example.Film.2024.2160p.WEBRip.DDP5.1.x265-RARBG",
        size_gb=11.2,
        seeders=95,
        days_old=44,
    ),
    _sample(
        "Example.Film.2024.1080p.WEBRip.x264.AAC-YTS.MX",
        size_gb=2.1,
        seeders=1500,
        days_old=46,
    ),
    _sample(
        "Example Film 2024 480p DVDRip XviD-OLDSCHOOL",
        size_gb=0.7,
        seeders=12,
        days_old=120,
    ),
    _sample(
        "Example.Film.2024.MULTi.VFF.x265-UNTAGGED",
        size_gb=5.0,
        seeders=8,
        days_old=35,
    ),
    _sample(
        "Example.Film.2024.2160p.WEB-DL.DV.HDR10.DDP5.1.Atmos.HEVC-KOGi",
        size_gb=31.6,
        seeders=52,
        days_old=49,
        indexer="Second Indexer",
    ),
)


def sample_releases() -> list[ParsedRelease]:
    """The sample candidate set, in indexer order.

    A fresh list each call — callers rank and re-order it, and the samples
    themselves are frozen models, so nothing here is shared mutable state.
    """
    return list(_SAMPLES)
