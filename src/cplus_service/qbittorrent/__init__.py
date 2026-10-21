"""A small client for qBittorrent's Web API v2. See :mod:`.client`."""

from .client import (
    QBittorrentAuthError,
    QBittorrentClient,
    QBittorrentError,
    Torrent,
    TorrentFile,
    TorrentProperties,
)

__all__ = [
    "QBittorrentAuthError",
    "QBittorrentClient",
    "QBittorrentError",
    "Torrent",
    "TorrentFile",
    "TorrentProperties",
]
