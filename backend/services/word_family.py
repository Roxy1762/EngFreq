"""
Word-family / cognate grouping.

Strategy (V1 — no external data file required):
  1. Use spaCy lemma as the canonical lemma.
  2. Apply ordered suffix-stripping rules to derive a *family_root*.
     This approximates derivational morphology without needing Nation-style
     word-family lists.
  3. The family_root is stored as family_id.

Design notes:
  - lemma grouping   : studies / studying / studied → study
  - family grouping  : study / student / studious / studio → "studi" (root)
  - Both are kept distinct in the data model (lemma vs family_id).
  - A future V2 can replace family_id assignment by loading a JSON word-family
    list and mapping each lemma to its family head — the interface stays the same.
"""
from __future__ import annotations

import re
from typing import Optional

# Derivational suffixes, ordered longest-first so we strip the most specific one.
# Each entry: (suffix, min_remaining_stem_length)
_SUFFIXES: list[tuple[str, int]] = [
    # adverb / adjective
    ("ically", 3),
    ("lessly", 3),
    ("fully", 3),
    ("ously", 3),
    ("ively", 3),
    ("ally", 3),
    ("ly", 4),
    # noun
    ("ations", 3), ("ation", 3),
    ("itions", 3), ("ition", 3),
    ("sions", 3),  ("sion", 3),
    ("tions", 3),  ("tion", 3),
    ("nesses", 3), ("ness", 3),
    ("ments", 3),  ("ment", 3),
    ("ities", 3),  ("ity", 3),
    ("ances", 3),  ("ance", 3),
    ("ences", 3),  ("ence", 3),
    ("ings", 3),   ("ing", 3),
    ("ers", 3),    ("er", 4),
    ("ors", 3),    ("or", 4),
    ("ists", 3),   ("ist", 4),
    # adjective
    ("ational", 3),
    ("ional", 3),
    ("ical", 3),
    ("able", 3),
    ("ible", 3),
    ("ative", 3),
    ("itive", 3),
    ("ive", 4),
    ("ful", 4),
    ("less", 4),
    ("ous", 4),
    ("ious", 3),
    ("al", 4),
    ("ic", 4),
    # verb
    ("izes", 3), ("ize", 3),
    ("ises", 3), ("ise", 3),
    ("ifies", 3), ("ify", 3),
    ("ened", 3), ("ens", 3), ("en", 4),
    ("ed", 4),
    ("es", 4),
    # prefix strip (optional light pass)
]

# Common prefixes that should NOT change the family root
# (we keep these in the root rather than stripping them)
_PREFIX_NORMALISE = {
    "un": 3, "in": 3, "im": 3, "ir": 3, "il": 3,
    "dis": 3, "mis": 3, "pre": 3, "re": 3,
    "over": 4, "under": 4, "out": 4,
}


def get_family_id(lemma: str) -> Optional[str]:
    """
    Return an approximate word-family root for *lemma*.
    Returns None for very short words where grouping would be meaningless.
    """
    if not lemma or len(lemma) <= 2:
        return None

    word = lemma.lower()

    # Apply suffix stripping (one pass — longest match wins)
    for suffix, min_len in _SUFFIXES:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if len(stem) >= min_len:
                word = stem
                break

    # Normalise common consonant doublings at stem boundary  e.g. "runn" → "run"
    if len(word) >= 4 and word[-1] == word[-2]:
        word = word[:-1]

    # Very short roots are not useful as family IDs
    if len(word) < 3:
        return lemma.lower()

    return word
