"""Info hashes remembered from recent searches, keyed by release.

The shipped app grabs with ``{release_guid, indexer_id, release_title, …}``
and nothing else — not the info hash, which it was never sent. Prowlarr does
not hand the hash back from a grab either. But this service ran the search the
release came from, moments earlier, and the hash was in that result. So each
search drops the hashes it saw here, and a grab looks its release up.

Purely a shortcut. A miss — a restart between search and grab, a release from
an indexer that gives no hash, a cache that has rolled over — costs nothing but
falling back to matching the torrent by name, which is what grab tracking does
for every private-tracker release anyway. The grab itself never depends on it.
"""

from __future__ import annotations

from collections import OrderedDict

#: Enough for a household's searches over a good while: a search returns tens
#: to low hundreds of releases, and an entry is two short strings.
MAX_ENTRIES = 5000


class ReleaseHashCache:
    """A bounded, least-recently-written map of ``(indexer_id, guid)`` to info hash."""

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: OrderedDict[tuple[int | None, str], str] = OrderedDict()
        self._max = max_entries

    def __len__(self) -> int:
        return len(self._entries)

    def remember(self, indexer_id: int | None, guid: str, info_hash: str | None) -> None:
        if not guid or not info_hash:
            return
        key = (indexer_id, guid)
        self._entries[key] = info_hash
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def lookup(self, indexer_id: int | None, guid: str) -> str | None:
        return self._entries.get((indexer_id, guid))
