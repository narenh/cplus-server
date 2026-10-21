"""A torrent's BitTorrent info hash, recovered from whatever Prowlarr gave.

Grab tracking finds a grabbed torrent in qBittorrent by this hash when it can
(see :mod:`cplus_service.tracking`); a release name is the fallback. Prowlarr
offers the hash in up to three places, depending on the indexer: an
``infoHash`` field, the ``btih`` of a ``magnetUrl``, or a magnet link standing
in for the ``guid`` or ``downloadUrl``. Private trackers usually offer none of
them, only a ``.torrent`` download URL, and that is fine — the name fallback is
for exactly those.

Every hash leaves here in one form: lower-case hex, 40 characters for a v1
hash and 64 for a v2 one, which is how qBittorrent reports them.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

_HEX = re.compile(r"^[0-9a-fA-F]+$")
_BASE32 = re.compile(r"^[A-Za-z2-7]{32}$")

#: A v2 info hash in a magnet link is a multihash: ``0x12`` (sha2-256) and
#: ``0x20`` (32 bytes) in front of the digest.
_MULTIHASH_SHA256 = "1220"


def normalise_info_hash(value: str | None) -> str | None:
    """``value`` as lower-case hex, or ``None`` if it is not an info hash.

    Accepts 40-hex (v1), 32-character base32 (v1, as older magnet links write
    it) and 64-hex (v2).
    """
    if not value:
        return None
    text = value.strip()
    if len(text) in (40, 64) and _HEX.match(text):
        return text.lower()
    if _BASE32.match(text):
        try:
            return binascii.hexlify(base64.b32decode(text.upper())).decode("ascii")
        except (binascii.Error, ValueError):
            return None
    return None


def info_hash_from_magnet(uri: str | None) -> str | None:
    """The info hash in a ``magnet:`` link's ``xt``, preferring v1 over v2."""
    if not uri or not uri.lower().startswith("magnet:"):
        return None
    query = urlsplit(uri).query or uri.partition("?")[2]
    topics = parse_qs(query).get("xt", [])
    v2: str | None = None
    for topic in topics:
        lowered = topic.lower()
        if lowered.startswith("urn:btih:"):
            found = normalise_info_hash(topic[len("urn:btih:"):])
            if found:
                return found
        elif lowered.startswith("urn:btmh:"):
            digest = topic[len("urn:btmh:"):]
            if digest.lower().startswith(_MULTIHASH_SHA256):
                v2 = v2 or normalise_info_hash(digest[len(_MULTIHASH_SHA256):])
    return v2


def info_hash_of(raw: Mapping[str, Any]) -> str | None:
    """The info hash of one raw Prowlarr release, from wherever it can be found."""
    direct = normalise_info_hash(str(raw.get("infoHash") or "") or None)
    if direct:
        return direct
    for key in ("magnetUrl", "guid", "downloadUrl"):
        found = info_hash_from_magnet(str(raw.get(key) or "") or None)
        if found:
            return found
    return None
