"""One TLS context for every outbound client.

httpx builds a fresh context, certificate store and all, for each client it
creates; this service keeps several long-lived clients (Plex, Seerr, Prowlarr,
the relay, qBittorrent, webhooks) and makes short-lived ones too. Sharing one
saves about a megabyte a client, which is a noticeable share of a process this
small.
"""

from __future__ import annotations

import ssl
from functools import cache

import certifi


@cache
def tls_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())
