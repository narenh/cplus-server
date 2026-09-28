"""An epub turned into what alignment works on: sentences, words and one letter string."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..epub import WORD_SPLIT, EpubBook

#: Seed length in letters, shared by anchoring and verification.
K = 12


def norm_word(word: str) -> str:
    """Letters the model can say: a-z and apostrophe, accents folded away."""
    word = unicodedata.normalize("NFKD", word.replace("’", "'").replace("‘", "'"))
    word = word.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z']", "", word).strip("'")


# Untrained Punkt knows no abbreviations, so "Mrs. Baggins" splits after "Mrs.". Seed
# a fixed list (training on the book learns junk abbreviations like "ink." and merges
# real sentences).
ABBREVS = "mr mrs ms mx dr st prof mt jr sr capt col gen sgt lt cmdr vs etc".split()
# Punkt still splits some cases (an opening quote glued to the title: “Mr.), so also
# merge any sentence ending in a title with the one after it.
_TITLE_END = re.compile(
    r"(?:^|[\s\"'“‘(])(?:mr|mrs|ms|mx|dr|prof|capt|col|gen|sgt|lt|cmdr)\.$", re.I
)


def _punkt() -> Any:
    from nltk.tokenize.punkt import PunktParameters, PunktSentenceTokenizer

    params = PunktParameters()
    params.abbrev_types = set(ABBREVS)
    return PunktSentenceTokenizer(params)


def split_sentences(punkt: Any, text: str) -> list[str]:
    out: list[str] = []
    for sentence in punkt.tokenize(text):
        if out and _TITLE_END.search(out[-1]):
            out[-1] += " " + sentence
        else:
            out.append(sentence)
    return out


@dataclass
class Book:
    title: str | None
    sections: list[dict[str, Any]]
    sents: list[dict[str, Any]]
    words: list[str]
    wlen: np.ndarray
    offs: np.ndarray
    letters: str
    owner: np.ndarray


def build_book(epub: EpubBook) -> Book:
    punkt = _punkt()
    sents: list[dict[str, Any]] = []
    words: list[str] = []
    sections: list[dict[str, Any]] = []
    para = 0
    for si, doc in enumerate(epub.docs):
        sec: dict[str, Any] = {"index": si, "href": doc.href, "title": None}
        sections.append(sec)
        for tag, text in doc.blocks:
            if sec["title"] is None and tag in ("h1", "h2", "h3"):
                sec["title"] = text
            for sentence in split_sentences(punkt, text):
                toks = [t for t in WORD_SPLIT.split(sentence) if t]
                if not toks:
                    continue
                w0 = len(words)
                words.extend(norm_word(t) for t in toks)
                sents.append(
                    {"sec": si, "para": para, "text": sentence, "w0": w0, "w1": len(words)}
                )
            para += 1
    wlen = np.array([len(w) for w in words], dtype=np.int64)
    offs = np.r_[0, np.cumsum(wlen)]
    owner = np.repeat(np.arange(len(words)), wlen)
    return Book(
        title=epub.title,
        sections=sections,
        sents=sents,
        words=words,
        wlen=wlen,
        offs=offs,
        letters="".join(words),
        owner=owner,
    )


def letters_only(epub: EpubBook) -> str:
    """The book's letter string without sentence splitting — all verification needs."""
    return "".join(
        norm_word(t)
        for doc in epub.docs
        for _tag, text in doc.blocks
        for t in WORD_SPLIT.split(text)
        if t
    )


def unique_seeds(letters: str) -> dict[str, int]:
    """Every K-letter sequence mapped to its offset, or -1 where it is not unique."""
    first: dict[str, int] = {}
    for j in range(len(letters) - K + 1):
        seed = letters[j : j + K]
        first[seed] = -1 if seed in first else j
    return first
