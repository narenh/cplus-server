"""Audiobook read-along: an epub's sentences timed against a Plex audiobook's audio.

cplus serves alignments; it doesn't make them. An admin uploads one made
elsewhere (see ``parked/cplus_align`` for the aligner this service used to run
alongside it), :mod:`.ingest` checks it against the album in Plex and stores it
in chunks, and clients read it through :mod:`cplus_service.api.routes.audiobooks`.
:mod:`.access` decides who may see which book.
"""
