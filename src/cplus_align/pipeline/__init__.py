"""The alignment engine. Needs numpy, torch and the model.

Runs only on the runtime the supervisor installs into the aligner volume
(``python -m cplus_align.pipeline``), never on the image's own Python — see
:mod:`cplus_align`.

Method, unchanged from the standalone ``bookalign.py`` it came from, and with
no reliance on the audio's own chapter markers:

1. Decode the audio to 16 kHz mono and run the MMS CTC model over all of it
   (the *emissions*).
2. Greedy-decode the emissions to a rough letter stream, find 12-letter
   sequences that occur exactly once in the book, and keep the longest in-order
   chain of them — anchors between audio time and book position.
3. Between anchors, run CTC forced alignment on ~75 s segments. Stretches where
   the book has far more text than the audio has time for are marked unspoken.
4. Group word times into sentences.

The ctc-forced-aligner code and the model are CC-BY-NC 4.0 (non-commercial).
"""
