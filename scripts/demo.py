#!/usr/bin/env python
"""Exercise the stage-1 modules without a running server.

    python scripts/demo.py                 # parse a canned release set, recommend
    python scripts/demo.py tt0111161       # same, but against a live Prowlarr

The live mode needs a Prowlarr to talk to::

    export PROWLARR_URL=http://prowlarr.local:9696
    export PROWLARR_API_KEY=...
    export PREFERRED_INDEXER_ID=3          # optional; unset means "All indexers"
"""

from __future__ import annotations

import asyncio
import os
import sys

from cplus_service.prowlarr.client import ProwlarrClient, ProwlarrError
from cplus_service.quality.engine import preferred_indexer_candidates, rank, recommend
from cplus_service.quality.models import (
    ExcludePrereleaseRule,
    HdrMatchRule,
    QualityProfile,
    ResolutionOrderRule,
    SizeCapGbRule,
    SizeDirection,
    SizeRule,
    SourceOrderRule,
    default_profile,
)
from cplus_service.release.models import ParsedRelease, Resolution, Source
from cplus_service.release.parser import parse_prowlarr_results

SAMPLE_RELEASES = [
    "Dune.Part.Two.2024.2160p.WEB-DL.DDP5.1.Atmos.DV.HDR10+.HEVC-FLUX",
    "Dune Part Two 2024 2160p WEB-DL DDP5 1 Atmos DV HDR HEVC-FLUX",
    "Dune.Part.Two.2024.REPACK.2160p.WEB-DL.DDP5.1.Atmos.DV.P8.HEVC-FLUX",
    "Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.P7.FEL.HDR.HEVC.TrueHD.7.1.Atmos-FraMeSToR",
    "Dune.Part.Two.2024.1080p.BluRay.x264.DTS-HD.MA.5.1-SPARKS",
    "Dune.Part.Two.2024.2160p.BluRay.x265.10bit.HDR.DTS-X.7.1-GROUP",
    "Dune.Part.Two.2024.HDCAM.1080p.x264-SPOOKY",
    "Dune.Part.Two.2024.COMPLETE.UHD.BLURAY-TERMiNAL",
    "Dune.Part.Two.2024.1080p.WEBRip.x264-YIFY",
]

STREAM_NOW = QualityProfile(
    name="Stream Now",
    rules=[
        ExcludePrereleaseRule(),
        SizeCapGbRule(value=25.0),
        ResolutionOrderRule(values=[Resolution.FHD_1080P, Resolution.UHD_2160P]),
        SourceOrderRule(values=[Source.WEB_DL, Source.WEBRIP, Source.BLURAY]),
        SizeRule(direction=SizeDirection.SMALLEST),
    ],
)

ADD_4K = QualityProfile(
    name="Add 4K",
    rules=[
        ExcludePrereleaseRule(),
        ResolutionOrderRule(values=[Resolution.UHD_2160P, Resolution.FHD_1080P]),
        HdrMatchRule(values=["DV_P7", "DV_P8", "HDR10+", "HDR10", "SDR"]),
        SourceOrderRule(values=[Source.REMUX, Source.BLURAY, Source.WEB_DL]),
        SizeRule(direction=SizeDirection.LARGEST),
    ],
)

PROFILES = [STREAM_NOW, ADD_4K, default_profile("Default")]


def describe(release: ParsedRelease) -> str:
    tags = [release.resolution.value, release.source.value]
    tags += release.hdr_tags + release.audio_tags
    if release.is_repack_or_proper:
        tags.append(f"REPACK v{release.repack_version}")
    if release.is_prerelease:
        tags.append("PRERELEASE")
    size = f"{release.size_gb:.1f} GB" if release.size_gb is not None else "size unknown"
    return f"{' / '.join(tags):<62} {size:>12}   {release.title}"


def report(candidates: list[ParsedRelease]) -> None:
    print(f"\n{len(candidates)} candidates after parsing (full discs already dropped)\n")
    for release in candidates:
        print(f"  {describe(release)}")

    for profile in PROFILES:
        best = recommend(candidates, profile)
        print(f"\n── {profile.name} " + "─" * (60 - len(profile.name)))
        if best is None:
            print("  no eligible release (everything was filtered out)")
            continue
        print(f"  recommended: {best.title}")
        for position, release in enumerate(rank(candidates, profile)[1:4], start=2):
            print(f"  {position}. {release.title}")


def run_offline() -> None:
    # Sizes are made up but plausible, so the size rules have something to bite on.
    sizes_gb = [22, 20, 23, 78, 12, 18, 3, 85, 4]
    raws = [
        {
            "title": title,
            "guid": f"guid-{index}",
            "indexerId": 1,
            "size": size_gb * 1024**3,
        }
        for index, (title, size_gb) in enumerate(
            zip(SAMPLE_RELEASES, sizes_gb, strict=True)
        )
    ]
    report(parse_prowlarr_results(raws))


async def run_live(imdb_id: str) -> None:
    url = os.environ.get("PROWLARR_URL")
    api_key = os.environ.get("PROWLARR_API_KEY")
    if not url or not api_key:
        sys.exit("PROWLARR_URL and PROWLARR_API_KEY must be set for live mode")

    preferred = os.environ.get("PREFERRED_INDEXER_ID")
    preferred_indexer_id = int(preferred) if preferred else None

    async with ProwlarrClient(url, api_key) as prowlarr:
        status = await prowlarr.verify_connection()
        print(f"connected to {status.app_name or 'Prowlarr'} {status.version or '?'}")

        candidates = await prowlarr.search_movie(imdb_id)
        print(f"searched {imdb_id}: {len(candidates)} usable releases")

        # The preferred-indexer hard filter is the caller's job, not the engine's.
        candidates = preferred_indexer_candidates(candidates, preferred_indexer_id)
        report(candidates)


def main() -> None:
    if len(sys.argv) > 1:
        try:
            asyncio.run(run_live(sys.argv[1]))
        except ProwlarrError as exc:
            sys.exit(f"Prowlarr error: {exc}")
    else:
        run_offline()


if __name__ == "__main__":
    main()
