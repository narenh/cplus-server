"""Release-title parser.

Scene and P2P release names use ``.`` as their word delimiter
(``Movie.2024.2160p.HDR.DTS-HD.MA-GROUP``) while many trackers and Usenet
indexers hand back the same name space-delimited, and plenty of real titles mix
both.  Every pattern in this module therefore runs against a *normalised* form
in which ``.`` and ``_`` have been folded to spaces, so a single pattern matches
all three shapes.  Matching on spaces alone silently misses the majority of
real-world titles — that is the specific bug this module exists to fix, so if
you add a pattern here, add a dot-delimited test for it.

Token boundaries use explicit lookarounds rather than ``\\b`` so that ``x264``
does not match inside ``x2640`` while ``-GROUP`` and ``WEB-DL`` still parse: a
hyphen counts as a boundary, an alphanumeric does not.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from .models import ParsedRelease, ParsedTitle, Resolution, Source

# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #

_DELIMITERS = re.compile(r"[._]+")
_WHITESPACE = re.compile(r"\s+")


def normalize(title: str) -> str:
    """Fold a release title into the form every pattern below is written against.

    Lower-cases, collapses ``.``/``_``/whitespace runs into single spaces, and
    leaves ``-``, ``+`` and ``:`` intact (they carry meaning in ``WEB-DL``,
    ``HDR10+`` and ``DTS:X``).
    """
    text = _DELIMITERS.sub(" ", title.lower())
    return _WHITESPACE.sub(" ", text).strip()


def _tok(pattern: str) -> re.Pattern[str]:
    """Compile ``pattern`` so it only matches as a whole token.

    Alphanumerics on either side block a match; ``-``, spaces and ``+`` do not.
    """
    return re.compile(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])")


# --------------------------------------------------------------------------- #
# Patterns
# --------------------------------------------------------------------------- #

# Ordered — the first hit wins, so 2160p is tested before 1080p.
_RESOLUTIONS: list[tuple[Resolution, re.Pattern[str]]] = [
    (Resolution.UHD_2160P, _tok(r"(?:2160[pi]?|4k|uhd|3840\s?x\s?2160)")),
    (Resolution.FHD_1080P, _tok(r"(?:1080[pi]|1920\s?x\s?1080)")),
    (Resolution.HD_720P, _tok(r"(?:720[pi]|1280\s?x\s?720)")),
    (Resolution.SD_480P, _tok(r"(?:480[pi]|576[pi]|640\s?x\s?480|dvd\s?rip)")),
]

_REMUX = _tok(r"(?:bd|uhd|br)?remux")
_WEB_DL = _tok(r"web[\s-]?dl")
_WEBRIP = _tok(r"web[\s-]?rip")
_WEB_BARE = _tok(r"web(?:hd)?")

# Disc-sourced provenance.  DVD is deliberately absent: there is no DVD member
# on Source, and every DVD release we care about is a rip (handled below).
_BLURAY_FAMILY = _tok(r"(?:blu[\s-]?ray|bd[\s-]?rip|br[\s-]?rip|bd(?:25|50|66|100)|bd|hd[\s-]?dvd)")

# Evidence that the video was re-encoded, i.e. it is NOT an untouched disc.
_ENCODE_CODEC = _tok(r"(?:[xh][\s-]?26[456]|hevc|av1|xvid|divx)")
_RIP = _tok(r"(?:bd[\s-]?rip|br[\s-]?rip|dvd[\s-]?rip|hd[\s-]?rip|web[\s-]?rip|hdtv[\s-]?rip)")

_DISC_MARKER = _tok(
    r"(?:bdmv|video[\s-]?ts|m2ts|iso|bd(?:25|50|66|100)|untouched|full[\s-]?blu[\s-]?ray|disc)"
)
_COMPLETE = _tok(r"complete")

_DV = _tok(r"(?:dolby[\s-]?vision|dovi|dv|dvhe|dvh1)")
_DV_EXPLICIT_PROFILE = re.compile(
    r"(?:dolby[\s-]?vision|dovi|dvhe|dvh1|dv)[\s-]*(?:profile[\s-]*|p)(\d{1,2})(?![0-9])"
)
_DVHE_PROFILE = re.compile(r"dvhe[\s-]*(\d{1,2})(?![0-9])")
_DV_DUAL_LAYER = _tok(r"(?:fel|mel)")

_HDR10PLUS = _tok(r"(?:hdr\s?10\s?\+|hdr10plus|hdrplus|hdr\+)")
_HDR = _tok(r"(?:hdr10|hdr)")

_ATMOS = _tok(r"atmos")
_DTSX = _tok(r"dts[\s:-]?x")
_TRUEHD = _tok(r"true[\s-]?hd")

_REPACK = _tok(r"repack\s?(\d*)")
_PROPER = _tok(r"proper")
_REAL_PROPER = _tok(r"real[\s-]?proper")
_RERIP = _tok(r"re[\s-]?rip")

_PRERELEASE = _tok(
    r"(?:hd)?(?:cam(?:rip)?|ts|telesync|tc|telecine|screener|scr|dvd\s?scr|bd\s?scr"
    r"|r5|workprint|dcp|pre\s?dvd)"
)

_YEAR = re.compile(r"(?<![0-9])(?:19|20)\d{2}(?![0-9])")

# Earliest of these marks where the movie name ends, when there is no year.
_FIRST_QUALITY_TOKEN = _tok(
    r"(?:2160[pi]?|1080[pi]|720[pi]|480[pi]|576[pi]|4k|uhd|web[\s-]?dl|web[\s-]?rip|web"
    r"|blu[\s-]?ray|bd[\s-]?rip|br[\s-]?rip|dvd[\s-]?rip|remux|hdtv|[xh][\s-]?26[456]"
    r"|hevc|av1|xvid|divx)"
)

_FILE_EXT = re.compile(r"\.(?:mkv|mp4|avi|m2ts|iso|srt|nfo)$", re.IGNORECASE)
_TRAILING_BRACKETS = re.compile(r"(?:\s*[\[({][^\[\](){}]*[\])}])+$")
# One optional internal hyphen so real groups like ``D-Z0N3`` survive.  Dots and
# spaces are excluded on purpose: without that, a title with a hyphen in the
# movie name and no group at all (``Spider-Man.2002.1080p.BluRay.x264``) would
# report everything after the hyphen as the group.
_TRAILING_GROUP = re.compile(
    r"[-–]\s*(?P<first>[A-Za-z0-9][A-Za-z0-9_@]{0,20})"
    r"(?:-(?P<second>[A-Za-z0-9_@]{1,20}))?\s*$"
)

# Left halves that mean the hyphen belongs to a quality tag, not to the group:
# ``...DTS-X-GRP`` is DTS:X plus group GRP, not a group called "X-GRP".
_TAG_HYPHEN_PREFIXES = {
    "x", "dts", "hd", "ma", "dd", "ddp", "web", "blu", "ray", "dl", "true", "dolby",
    "ac3", "eac3", "e", "dv", "hdr", "atmos", "mpeg", "vc", "h",
}
_LEADING_GROUP = re.compile(r"^\[([A-Za-z0-9][A-Za-z0-9_ .-]{0,29})\]")

# Trailing tokens that look like a group but are really quality tags.  A title
# ending in one of these has no parseable group.
_NOT_A_GROUP = {
    "dl", "hd", "ma", "hr", "sd", "uhd", "web", "webdl", "webrip", "bluray", "bdrip",
    "brrip", "dvdrip", "remux", "hdtv", "x264", "x265", "x266", "h264", "h265", "h266",
    "hevc", "av1", "xvid", "divx", "avc", "dts", "dtshd", "ddp", "eac3", "ac3", "aac",
    "flac", "truehd", "atmos", "hdr", "hdr10", "sdr", "dv", "dovi", "repack", "proper",
    "internal", "limited", "extended", "unrated", "remastered", "2160p", "1080p", "720p",
    "480p", "576p", "4k",
}
# Deliberately absent: CAM, TS, TC, SCR.  Those are pre-release markers when they
# appear mid-title, but a token in trailing position after the codec is a group
# name (``...H.264-TS``), and stripping the group is what keeps them from being
# read as pre-release markers in the first place.


# --------------------------------------------------------------------------- #
# Field extraction
# --------------------------------------------------------------------------- #


def _parse_resolution(norm: str) -> Resolution:
    for resolution, pattern in _RESOLUTIONS:
        if pattern.search(norm):
            return resolution
    return Resolution.UNKNOWN


def _parse_source(norm: str) -> tuple[Source, bool]:
    """Return ``(source, is_full_disc)``.

    A release is a full disc when it is not WEB, not REMUX and carries no
    evidence of re-encoding — either because it is explicitly marked (``BDMV``,
    ``COMPLETE.BLURAY``, ``BD66``, ``ISO``, ``UNTOUCHED``) or because it names a
    disc source with no encode codec at all.  AVC/VC-1/MPEG-2 are deliberately
    absent from the encode-codec set for exactly this reason: they appear on
    untouched discs, so ``1080p.BluRay.AVC.DTS-HD.MA`` reads as a disc while
    ``1080p.BluRay.x264`` reads as an encode.

    Note the deliberate conservatism trade-off: a BluRay-tagged title carrying
    no codec token at all (``...1080p.BluRay.DTS-HD.MA.5.1-GROUP``) is treated
    as a full disc, because "not WEB, not REMUX, not encode" is the definition
    we were given.  In practice disc-sourced encodes essentially always name
    their codec.

    The source label on a full disc is never observed downstream — full discs
    are dropped before any caller sees them.
    """
    if _REMUX.search(norm):
        return Source.REMUX, False
    if _WEBRIP.search(norm):
        return Source.WEBRIP, False
    if _WEB_DL.search(norm) or _WEB_BARE.search(norm):
        return Source.WEB_DL, False

    bluray = bool(_BLURAY_FAMILY.search(norm))
    encoded = bool(_ENCODE_CODEC.search(norm) or _RIP.search(norm))
    disc_marker = bool(_DISC_MARKER.search(norm)) or (bluray and bool(_COMPLETE.search(norm)))
    is_full_disc = disc_marker or (bluray and not encoded)

    if bluray or disc_marker:
        return Source.BLURAY, is_full_disc
    if encoded:
        return Source.ENCODE, False
    return Source.UNKNOWN, False


def _parse_dv_profile(norm: str, *, source: Source, is_encode: bool, is_hdr: bool) -> int:
    """Best-effort Dolby Vision profile; ``0`` when the release has no DV.

    Explicit markers in the title always win — some groups spell the profile
    out (``DV.P8``, ``DVHE.05``, and ``FEL``/``MEL`` which are both the
    dual-layer profile 7).  Only when nothing explicit is present do we fall
    back to inferring from source.
    """
    if not _DV.search(norm):
        return 0

    match = _DVHE_PROFILE.search(norm) or _DV_EXPLICIT_PROFILE.search(norm)
    if match:
        profile = int(match.group(1))
        if 0 < profile < 20:
            return profile
    if _DV_DUAL_LAYER.search(norm):
        return 7

    if source is Source.REMUX:
        return 7
    if is_encode:
        return 8
    if source in (Source.WEB_DL, Source.WEBRIP):
        return 8 if is_hdr else 5
    # Neither remux, encode nor WEB: single-layer profile 5 is the safer guess.
    return 5


def _parse_repack(norm: str) -> tuple[bool, int | None]:
    repack = _REPACK.search(norm)
    proper = _PROPER.search(norm)
    real_proper = _REAL_PROPER.search(norm)
    rerip = _RERIP.search(norm)

    if not (repack or proper or rerip):
        return False, None

    version = 1
    if repack and repack.group(1):
        version = int(repack.group(1))
    elif real_proper:
        version = 2
    return True, version


def _extract_release_group(title: str) -> str | None:
    """Best-effort trailing ``-GROUP`` extraction, with an anime-style fallback."""
    cleaned = _FILE_EXT.sub("", title.strip())
    cleaned = _TRAILING_BRACKETS.sub("", cleaned).strip()

    match = _TRAILING_GROUP.search(cleaned)
    if match:
        first, second = match.group("first"), match.group("second")
        if second is None:
            candidate = first
        elif first.lower() in _TAG_HYPHEN_PREFIXES:
            candidate = second
        else:
            candidate = f"{first}-{second}"
        if candidate.lower() not in _NOT_A_GROUP and not candidate.isdigit():
            return candidate

    leading = _LEADING_GROUP.search(title.strip())
    if leading:
        candidate = leading.group(1).strip()
        if candidate.lower() not in _NOT_A_GROUP and not candidate.isdigit():
            return candidate
    return None


def _extract_base_title(norm: str, release_group: str | None) -> str:
    """The underlying movie name, used to diff a REPACK against its base release.

    Cuts the normalised title at the release year when there is one, otherwise
    at the first quality token.
    """
    text = _strip_group_segment(norm, release_group)

    year = _YEAR.search(text)
    if year:
        text = text[: year.end()]
    else:
        token = _FIRST_QUALITY_TOKEN.search(text)
        if token:
            text = text[: token.start()]

    text = re.sub(r"[^a-z0-9]+", " ", text)
    return _WHITESPACE.sub(" ", text).strip()


def _strip_group_segment(norm: str, release_group: str | None) -> str:
    """Drop the trailing ``-group`` so group names cannot trip token matching.

    Without this, ``...H.264-TS`` (a group literally named TS) would be read as
    a telesync, and ``-CAM`` as a cam rip.
    """
    if not release_group:
        return norm
    return re.sub(rf"[-–]\s*{re.escape(release_group.lower())}\s*$", "", norm).strip()


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def parse_title(title: str) -> ParsedTitle:
    """Parse a release title into structured metadata.

    Pure and side-effect free.  ``is_full_disc`` may be ``True`` here; callers
    that hand results onward are responsible for dropping those (or, more
    simply, use :func:`parse_prowlarr_results`, which does it for you).
    """
    norm = normalize(title)
    release_group = _extract_release_group(title)
    body = _strip_group_segment(norm, release_group)

    # All tag matching runs against `body` — the title minus its trailing
    # `-GROUP` — so a group named TS, CAM or DVSUX cannot masquerade as a
    # telesync, a cam rip or Dolby Vision.
    source, is_full_disc = _parse_source(body)
    is_encode = bool(_ENCODE_CODEC.search(body) or _RIP.search(body))

    is_hdr10plus = bool(_HDR10PLUS.search(body))
    has_hdr_token = bool(_HDR.search(body))
    is_hdr = has_hdr_token and not is_hdr10plus

    dv_profile = _parse_dv_profile(
        body,
        source=source,
        is_encode=is_encode,
        is_hdr=has_hdr_token or is_hdr10plus,
    )
    is_repack_or_proper, repack_version = _parse_repack(body)

    return ParsedTitle(
        title=title,
        resolution=_parse_resolution(body),
        source=source,
        dv_profile=dv_profile,
        is_hdr10plus=is_hdr10plus,
        is_hdr=is_hdr,
        has_atmos=bool(_ATMOS.search(body)),
        has_dtsx=bool(_DTSX.search(body)),
        has_truehd=bool(_TRUEHD.search(body)),
        is_repack_or_proper=is_repack_or_proper,
        repack_version=repack_version,
        is_prerelease=bool(_PRERELEASE.search(body)),
        is_full_disc=is_full_disc,
        release_group=release_group,
        base_title=_extract_base_title(norm, release_group),
    )


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def parse_prowlarr_result(raw: Mapping[str, Any]) -> ParsedRelease:
    """Parse one Prowlarr ``ReleaseResource`` into a :class:`ParsedRelease`.

    Unknown/missing keys are tolerated — indexers vary in what they populate.
    """
    parsed = parse_title(str(raw.get("title") or ""))
    return ParsedRelease(
        **parsed.model_dump(exclude={"hdr_tags", "audio_tags"}),
        guid=str(raw.get("guid") or ""),
        indexer_id=raw.get("indexerId"),
        indexer=raw.get("indexer"),
        size_bytes=raw.get("size"),
        publish_date=_coerce_datetime(raw.get("publishDate")),
        seeders=raw.get("seeders"),
        leechers=raw.get("leechers"),
        download_url=raw.get("downloadUrl") or raw.get("magnetUrl"),
        info_url=raw.get("infoUrl"),
        protocol=raw.get("protocol"),
    )


def parse_prowlarr_results(raws: Iterable[Mapping[str, Any]]) -> list[ParsedRelease]:
    """Parse a Prowlarr search payload, dropping full discs entirely.

    This is the parser boundary the rest of the service sits behind: nothing
    downstream ever sees a full-disc release.  The list is returned in the order
    Prowlarr gave it — no sorting, bucketing or categorisation happens here or
    anywhere else in this service.
    """
    parsed = (parse_prowlarr_result(raw) for raw in raws)
    return [release for release in parsed if not release.is_full_disc]
