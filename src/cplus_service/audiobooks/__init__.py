"""Audiobook read-along: aligning an epub's sentences to a Plex audiobook's audio.

cplus-service owns everything an admin or a client sees — the Audiobooks tab,
the job records, the finished alignments — and none of the heavy lifting. That
happens in the ``cplus-aligner`` sidecar (:mod:`cplus_align`), which this side
talks to only through the shared directory described in
:mod:`cplus_align.protocol`:

* :mod:`.runtime` reads whether the sidecar is up and its runtime installed,
  and asks it to install or remove the runtime.
* :mod:`.jobs` turns an upload into a job directory the sidecar will pick up.
* :mod:`.monitor` follows each job's files, keeps its row current, and ingests
  the result (:mod:`.ingest`) when one appears.

The sidecar reaches back in for exactly one thing — the audio — through
``/internal/aligner/jobs/{id}/tracks/{n}``, which relays Plex with the admin
token so the token itself never leaves this container.
"""
