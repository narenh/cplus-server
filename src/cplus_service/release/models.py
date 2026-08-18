"""Structured representation of a parsed release.

This module defines the stable contract that stage 2 (HTTP endpoints) and the
tvOS client both consume.  Field names here are load-bearing — changing one is a
breaking API change.

Two models:

``ParsedTitle``
    Everything that can be derived from a release *title* alone.  Pure, easy to
    unit test.

``ParsedRelease``
    ``ParsedTitle`` plus the Prowlarr fields the client needs in order to do its
    own sectioning/sorting (size, publish date, indexer, guid, ...).

Deliberately absent: any notion of ``category``, ``bucket``, ``section`` or
rank.  Categorisation is a tvOS client-side concern driven purely by these tags
plus ``size_bytes``/``publish_date``.  Do not add one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, computed_field

BYTES_PER_GB = 1024**3


class Resolution(StrEnum):
    """Vertical resolution bucket parsed out of a release title."""

    UHD_2160P = "2160p"
    FHD_1080P = "1080p"
    HD_720P = "720p"
    SD_480P = "480p"
    UNKNOWN = "unknown"


class Source(StrEnum):
    """Provenance of the video.

    ``BLURAY`` means *disc-sourced* — a BluRay/UHD rip or encode.  Untouched
    full discs never reach a caller (they are dropped at the parser boundary),
    so ``BLURAY`` in practice always denotes a disc-sourced encode.

    ``ENCODE`` is the fallback for a title that carries a clear encode codec
    (x264/x265/x266/HEVC/AV1/...) but no recognisable provenance token.
    """

    WEB_DL = "WEB-DL"
    WEBRIP = "WEBRip"
    BLURAY = "BluRay"
    REMUX = "REMUX"
    ENCODE = "encode"
    UNKNOWN = "unknown"


class HdrTag(StrEnum):
    """Canonical HDR/DV tokens used by the ``hdr_match`` quality-profile rule.

    ``DV`` matches a release with *any* Dolby Vision profile; ``DV_P5``/``DV_P7``/
    ``DV_P8`` match a specific profile.  A release carrying DV profile 8 is
    tagged with both ``DV`` and ``DV_P8``, so a profile can express either a
    coarse or a precise preference.

    ``SDR`` is emitted only when a release has no DV, no HDR10+ and no HDR10.
    """

    DV = "DV"
    DV_P5 = "DV_P5"
    DV_P7 = "DV_P7"
    DV_P8 = "DV_P8"
    HDR10_PLUS = "HDR10+"
    HDR10 = "HDR10"
    SDR = "SDR"


class AudioTag(StrEnum):
    """Canonical audio tokens used by the ``audio_match`` quality-profile rule.

    The three are independent: a single release may carry all of them (e.g.
    ``TrueHD.7.1.Atmos``).
    """

    ATMOS = "Atmos"
    DTSX = "DTS:X"
    TRUEHD = "TrueHD"


class ParsedTitle(BaseModel):
    """Everything derivable from a release title alone."""

    model_config = ConfigDict(frozen=True)

    title: str
    """The raw, unmodified release title exactly as Prowlarr returned it."""

    resolution: Resolution = Resolution.UNKNOWN
    source: Source = Source.UNKNOWN

    dv_profile: int = 0
    """Dolby Vision profile number; ``0`` means no Dolby Vision."""

    is_hdr10plus: bool = False
    is_hdr: bool = False
    """Plain HDR10.  Mutually exclusive with :attr:`is_hdr10plus` by design —
    an HDR10+ release sets only ``is_hdr10plus`` so the two tags rank
    distinctly in ``hdr_match``."""

    has_atmos: bool = False
    has_dtsx: bool = False
    has_truehd: bool = False

    is_repack_or_proper: bool = False
    repack_version: int | None = None
    """Best-effort iteration: ``REPACK2`` -> 2, ``REPACK``/``PROPER`` -> 1,
    ``REAL.PROPER`` -> 2.  ``None`` when :attr:`is_repack_or_proper` is False."""

    is_prerelease: bool = False
    """CAM / HDCAM / TS / telesync / telecine / screener / R5 / workprint / DCP."""

    is_full_disc: bool = False
    """Untouched disc (not WEB, not REMUX, not an encode).

    Always ``False`` on anything a caller receives — full discs are dropped at
    the parser boundary and are never eligible for anything downstream."""

    release_group: str | None = None

    base_title: str = ""
    """Normalised title with quality tags and release group stripped, used to
    diff a REPACK/PROPER against the base release of the same underlying
    title."""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hdr_tags(self) -> list[str]:
        """Canonical HDR/DV tokens this release matches, best-first.

        Mirrors the vocabulary ``hdr_match`` rules are written against.
        """
        tags: list[str] = []
        if self.dv_profile:
            tags.append(f"DV_P{self.dv_profile}")
            tags.append(HdrTag.DV.value)
        if self.is_hdr10plus:
            tags.append(HdrTag.HDR10_PLUS.value)
        if self.is_hdr:
            tags.append(HdrTag.HDR10.value)
        if not tags:
            tags.append(HdrTag.SDR.value)
        return tags

    @computed_field  # type: ignore[prop-decorator]
    @property
    def audio_tags(self) -> list[str]:
        """Canonical audio tokens this release matches."""
        tags: list[str] = []
        if self.has_atmos:
            tags.append(AudioTag.ATMOS.value)
        if self.has_dtsx:
            tags.append(AudioTag.DTSX.value)
        if self.has_truehd:
            tags.append(AudioTag.TRUEHD.value)
        return tags


class ParsedRelease(ParsedTitle):
    """A parsed release plus the Prowlarr fields callers pass straight through.

    The passthrough fields exist so the tvOS client can do its own sectioning
    and sorting (by size, age, seeders) without a second round trip.
    """

    guid: str = ""
    indexer_id: int | None = None
    indexer: str | None = None
    size_bytes: int | None = None
    publish_date: datetime | None = None
    seeders: int | None = None
    leechers: int | None = None
    download_url: str | None = None
    info_url: str | None = None
    protocol: str | None = None

    @property
    def size_gb(self) -> float | None:
        """Size in gibibytes, or ``None`` when Prowlarr reported no size."""
        if self.size_bytes is None:
            return None
        return self.size_bytes / BYTES_PER_GB
