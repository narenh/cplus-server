"""Short-lived memory of what the last searches returned.

``POST /grab`` carries only ``{action_id, release_guid, indexer_id}`` — the
client is not asked to echo back a release title or size it was already sent.
This cache is where those two come from: ``/search`` records them per guid, and
``/grab`` enriches its ``grabs`` row from it.

It is deliberately *enrichment only*. A miss (server restarted between search
and grab, or the entry aged out under pressure) costs a nicer-looking history
row, never the grab itself — everything Prowlarr actually needs to perform the
grab arrives in the request body.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass

from ..release.models import ParsedRelease

DEFAULT_CAPACITY = 5_000


@dataclass(frozen=True, slots=True)
class CachedRelease:
    """The parts of a release the grab history wants but the grab body omits."""

    title: str
    size_bytes: int | None
    indexer_id: int | None
    indexer: str | None


class ReleaseCache:
    """Bounded LRU of ``guid -> CachedRelease``.

    Global rather than per-user: a guid identifies a listing on an indexer, not
    a person, and two users searching the same film should share the entry
    rather than double it. Authorisation is enforced on the action, never here.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[str, CachedRelease] = OrderedDict()
        self._lock = asyncio.Lock()

    async def remember(self, releases: list[ParsedRelease]) -> None:
        async with self._lock:
            for release in releases:
                if not release.guid:
                    continue
                self._entries[release.guid] = CachedRelease(
                    title=release.title,
                    size_bytes=release.size_bytes,
                    indexer_id=release.indexer_id,
                    indexer=release.indexer,
                )
                self._entries.move_to_end(release.guid)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    async def get(self, guid: str) -> CachedRelease | None:
        async with self._lock:
            entry = self._entries.get(guid)
            if entry is not None:
                self._entries.move_to_end(guid)
            return entry

    async def size(self) -> int:
        async with self._lock:
            return len(self._entries)
