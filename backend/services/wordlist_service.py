"""
Word-level classification service.

Two orthogonal classifications are produced:

1. **Chinese exam levels** (for the 高考 / CET / 四六级 study workflow):
      基础   — Very common, Zipf ≥ 6.0 (students likely know already)
      高考   — In the official gaokao 3500-word list (prime study targets)
      四六级 — CET-level vocabulary (above gaokao)
      超纲   — Too obscure for 高考 (Zipf < 3.8, not in any reference list)

2. **CEFR levels** (for international comparability):
      A1, A2, B1, B2, C1, C2
   Uses the `cefrpy` library if available (high quality — dataset-backed), with
   a Zipf-based heuristic fallback so the feature degrades gracefully if cefrpy
   isn't installed.
"""
from __future__ import annotations

import logging
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


# ── Wordlist loaders (lru_cached, files loaded once) ──────────────────────────

@lru_cache(maxsize=1)
def _load_gaokao_words() -> Set[str]:
    return _read_wordlist("gaokao_3500.txt", log_name="gaokao")


@lru_cache(maxsize=1)
def _load_cet4_words() -> Set[str]:
    return _read_wordlist("cet4.txt", log_name="cet4")


def _read_wordlist(filename: str, *, log_name: str) -> Set[str]:
    path = _WORDLIST_DIR / filename
    if not path.exists():
        logger.debug("%s word list not found: %s", log_name, path)
        return set()
    words: Set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                words.add(line.lower())
    logger.info("Loaded %d %s words from %s", len(words), log_name, path)
    return words


# ── Frequency helpers ─────────────────────────────────────────────────────────

def _zipf(word: str) -> float:
    """Zipf frequency (0-8 scale). 0 = unknown/very rare; 7+ = ubiquitous."""
    try:
        from wordfreq import zipf_frequency
        return zipf_frequency(word, "en")
    except ImportError:
        return 5.0


# ── Chinese exam level ────────────────────────────────────────────────────────

def get_word_level(word: str) -> str:
    """Classify a word into: 基础 | 高考 | 四六级 | 超纲"""
    w = (word or "").lower().strip()
    if not w:
        return _LEVEL_GAOKAO

    z = _zipf(w)

    if z >= _ZIPF_BASIC:
        return _LEVEL_BASIC

    if w in _load_gaokao_words():
        return _LEVEL_GAOKAO

    cet4 = _load_cet4_words()
    if cet4 and w in cet4:
        return _LEVEL_CET

    if 0 < z < _ZIPF_MIN:
        return _LEVEL_ADVANCED

    return _LEVEL_GAOKAO


def is_gaokao_word(word: str) -> bool:
    return (word or "").lower().strip() in _load_gaokao_words()


# ── CEFR level ────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _cefr_analyzer():
    """Load cefrpy's analyzer; return None if not installed."""
    try:
        from cefrpy import CEFRAnalyzer
        return CEFRAnalyzer()
    except Exception as exc:   # noqa: BLE001
        logger.info("cefrpy not available (%s) — using Zipf-based CEFR fallback", exc)
        return None


def _cefr_from_zipf(word: str) -> str:
    """
    Zipf-based heuristic when cefrpy isn't installed.

    Thresholds chosen to roughly match CEFR vocabulary coverage on major lists:
      Zipf >= 6.3  → A1   (thousands: the, go, big, have)
      Zipf >= 5.7  → A2   (everyday: happy, travel, movie)
      Zipf >= 5.0  → B1   (intermediate: persist, surround, reveal)
      Zipf >= 4.2  → B2   (upper-intermediate: diverse, implement, sustain)
      Zipf >= 3.4  → C1   (advanced: elaborate, mitigate, coherent)
      else         → C2   (very rare / academic)
    """
    z = _zipf(word)
    if z >= 6.3:
        return "A1"
    if z >= 5.7:
        return "A2"
    if z >= 5.0:
        return "B1"
    if z >= 4.2:
        return "B2"
    if z >= 3.4:
        return "C1"
    return "C2"


@lru_cache(maxsize=8192)
def get_cefr_level(word: str) -> str:
    """
    Return CEFR level (A1–C2) for a word.

    Uses cefrpy if installed; otherwise falls back to a Zipf-based heuristic.
    """
    w = (word or "").strip().lower()
    if not w:
        return "B1"

    analyzer = _cefr_analyzer()
    if analyzer is None:
        return _cefr_from_zipf(w)

    # cefrpy API: get_average_word_level_CEFR(word) → "A1".."C2" or None
    try:
        level = analyzer.get_average_word_level_CEFR(w)
        if level:
            return str(level).upper()
    except Exception as exc:   # noqa: BLE001
        logger.debug("cefrpy failed for %s: %s — using fallback", w, exc)

    return _cefr_from_zipf(w)


# ── Batch tagging for vocab entries ───────────────────────────────────────────

def tag_vocab_entries(vocab: list) -> list:
    """Add word_level + cefr_level to each VocabEntry in-place."""
    for entry in vocab:
        target = (getattr(entry, "headword", "") or getattr(entry, "lemma", "") or "").strip()
        if not target:
            continue
        if not getattr(entry, "word_level", None):
            entry.word_level = get_word_level(target)
        if not getattr(entry, "cefr_level", None):
            entry.cefr_level = get_cefr_level(target)
    return vocab


def get_gaokao_words() -> Set[str]:
    return _load_gaokao_words()


def gaokao_word_count() -> int:
    return len(_load_gaokao_words())


def cefr_available() -> bool:
    """Is cefrpy installed (high-quality CEFR) or are we on heuristic mode?"""
    return _cefr_analyzer() is not None
