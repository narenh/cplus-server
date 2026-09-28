"""The audiobook aligner: everything that runs in the ``cplus-aligner`` sidecar.

Two halves with different dependencies, and the split is the point:

* **Standard library only** — :mod:`.protocol`, :mod:`.epub`, :mod:`.supervisor`,
  :mod:`.install` and this file. The supervisor runs on the image's own Python
  and must work before anything has been downloaded; cplus-service imports
  :mod:`.protocol` and :mod:`.epub` too, and its image carries none of the
  aligner's dependencies.
* :mod:`.pipeline` — numpy, torch and the model. It only ever runs inside the
  runtime the supervisor downloads into the aligner volume when an admin
  enables audiobooks, never on the image's own Python.

Nothing in the first half may import from the second.
"""
