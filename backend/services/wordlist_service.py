"""
Word-level classification service for Chinese high-school vocabulary.

Word levels:
  基础  — Very common, Zipf ≥ 6.0  (students likely know already)
  高考  — In the official gaokao 3500-word list  (prime study targets)
  四六级 — CET-level vocabulary (above gaokao)
  超纲  — Zipf < 3.8, not in any reference list (probably too advanced)
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)

_WORDLIST_DIR = Path(__file__).parent.parent.parent / "data" / "wordlists"

_LEVEL_BASIC = "基础"
_LEVEL_GAOKAO = "高考"
_LEVEL_CET = "四六级"
_LEVEL_ADVANCED = "超纲"

# Zipf thresholds
_ZIPF_BASIC = 6.0   # very high frequency → students already know it
_ZIPF_MIN = 3.8     # below this → probably too obscure for gaokao


@lru_cache(maxsize=1)
def _load_gaokao_words() -> Set[str]:
    path = _WORDLIST_DIR / "gaokao_3500.txt"
    if not path.exists():
        logger.warning("Gaokao word list not found: %s", path)
        return set()
    words: Set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line.lower())
    logger.info("Loaded %d gaokao words from %s", len(words), path)
    return words


@lru_cache(maxsize=1)
def _load_cet4_words() -> Set[str]:
    path = _WORDLIST_DIR / "cet4.txt"
    if not path.exists():
        return set()
    words: Set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line.lower())
    return words


def _zipf(word: str) -> float:
    """Return Zipf frequency score for a word (0-8 scale).
    Uses wordfreq library if available, else returns 5.0 (neutral).
    """
    try:
        from wordfreq import zipf_frequency
        return zipf_frequency(word, "en")
    except ImportError:
        return 5.0


def get_word_level(word: str) -> str:
    """Classify a word into: 基础 | 高考 | 四六级 | 超纲"""
    w = word.lower().strip()
    if not w:
        return _LEVEL_GAOKAO

    zipf_score = _zipf(w)

    # Very common words → students already know
    if zipf_score >= _ZIPF_BASIC:
        return _LEVEL_BASIC

    # In official gaokao list
    if w in _load_gaokao_words():
        return _LEVEL_GAOKAO

    # CET-4 words (if list loaded)
    cet4 = _load_cet4_words()
    if cet4 and w in cet4:
        return _LEVEL_CET

    # Too obscure for gaokao
    if zipf_score < _ZIPF_MIN and zipf_score > 0:
        return _LEVEL_ADVANCED

    # Default: treat as gaokao-level (unknown frequency = study candidate)
    return _LEVEL_GAOKAO


def is_gaokao_word(word: str) -> bool:
    return word.lower().strip() in _load_gaokao_words()


def tag_vocab_entries(vocab: list) -> list:
    """Add word_level to each VocabEntry in-place (mutates and returns the list)."""
    for entry in vocab:
        if not getattr(entry, "word_level", None):
            entry.word_level = get_word_level(entry.headword or entry.lemma or "")
    return vocab


def get_gaokao_words() -> Set[str]:
    return _load_gaokao_words()


def gaokao_word_count() -> int:
    return len(_load_gaokao_words())
