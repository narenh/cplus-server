"""Grab tracking: from a grab to a playable Plex item, as fast as the download allows.

A grab goes to qBittorrent through Prowlarr exactly as it always has. With
qBittorrent configured and the switch on, cplus then finds that torrent in
qBittorrent, has it fetch the start of the main file first, waits until Plex
can identify the file, scans just that folder, and catches the new item the
moment Plex adds it — reporting each step to the app through
``GET /grabs/{id}``.

* :mod:`.torrent` and :mod:`.paths` — the pure arithmetic: matching a torrent,
  choosing its main file and header pieces, building paths and choosing a
  library.
* :mod:`.state` — one grab's state and the long-poll wait on it.
* :mod:`.runner` — the phases, as one background task per grab.
* :mod:`.tracker` — the registry of grabs and tasks on ``AppState``.
* :mod:`.hashes` — info hashes remembered from searches, since the app's grab
  request does not carry one.

Deliberately imports nothing here: :mod:`cplus_service.prowlarr.client` uses
:mod:`.hashes`, and importing the runner from this package would drag the
database layer into the Prowlarr client.
"""
