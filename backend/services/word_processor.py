"""
Word processor — tokenises, cleans, and lemmatises text.

NLP backend priority:
  1. spaCy  (en_core_web_sm)  — best lemmatisation + POS
  2. NLTK WordNetLemmatizer   — good fallback
  3. Lowercase only           — last resort if no NLP available

Returns TokenInfo objects that downstream consumers can use without
caring which backend was used.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Set

from backend.services.basic_vocab import is_basic_word

logger = logging.getLogger(__name__)

# ── NLP backend setup ─────────────────────────────────────────────────────────

_nlp = None          # spaCy model, loaded lazily
_lemmatizer = None   # NLTK fallback, loaded lazily
_stopwords: Set[str] = set()

def _load_spacy():
    global _nlp
    if _nlp is not None:
        return _nlp
    try:
        import spacy
        _nlp = spacy.load("en_core_web_sm", disable=["parser", "ner"])
        logger.info("spaCy en_core_web_sm loaded")
    except Exception as e:
        logger.warning(f"spaCy unavailable: {e}. Using NLTK fallback.")
        _nlp = False
    return _nlp


def _load_nltk():
    global _lemmatizer, _stopwords
    if _lemmatizer is not None:
        return _lemmatizer

    try:
        import nltk
        for res in ("wordnet", "omw-1.4", "averaged_perceptron_tagger", "stopwords"):
            try:
                nltk.download(res, quiet=True)
            except Exception:
                pass
        from nltk.stem import WordNetLemmatizer
        from nltk.corpus import stopwords as sw
        _lemmatizer = WordNetLemmatizer()
        _stopwords = set(sw.words("english"))
        logger.info("NLTK lemmatizer loaded")
    except Exception as e:
        logger.warning(f"NLTK unavailable: {e}. Using no-op lemmatizer.")
        _lemmatizer = False
    return _lemmatizer


# ── Token data class ──────────────────────────────────────────────────────────

@dataclass
class TokenInfo:
    surface: str       # lowercased original form
    lemma: str         # lemmatised
    pos: str           # simplified POS: NOUN VERB ADJ ADV PROP OTHER
    is_proper: bool    # True if proper noun


# ── Cleaning helpers ──────────────────────────────────────────────────────────

# Apostrophe contractions to normalise before tokenising
_CONTRACTIONS = {
    "n't": " not",
    "'re": " are",
    "'ve": " have",
    "'ll": " will",
    "'d": " would",
    "'m": " am",
    "it's": "it is",
    "that's": "that is",
    "let's": "let us",
}

_CONTRACTION_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _CONTRACTIONS) + r")\b",
    re.IGNORECASE,
)

def _expand_contractions(text: str) -> str:
    def _replace(m):
        return _CONTRACTIONS[m.group(0).lower()]
    return _CONTRACTION_RE.sub(_replace, text)


def _normalise(text: str) -> str:
    """Unicode normalise, expand contractions, keep hyphens as separators."""
    text = unicodedata.normalize("NFKC", text)
    text = _expand_contractions(text)
    # Treat hyphens in hyphenated words as spaces (break compound words)
    text = re.sub(r"(?<=\w)-(?=\w)", " ", text)
    return text


# Regex: a "word" token — only alphabetic characters (after normalisation)
_WORD_RE = re.compile(r"[A-Za-z]{1,50}")


# ── spaCy path ────────────────────────────────────────────────────────────────

_SPACY_POS_MAP = {
    "NOUN": "NOUN", "PROPN": "PROP",
    "VERB": "VERB",
    "ADJ":  "ADJ",
    "ADV":  "ADV",
}

def _tokenise_spacy(text: str) -> List[TokenInfo]:
    nlp = _load_spacy()
    tokens = []
    # spaCy's max_length guard
    if len(text) > 900_000:
        text = text[:900_000]
    doc = nlp(text)
    for tok in doc:
        if not tok.is_alpha:
            continue
        surface = tok.text.lower()
        lemma = tok.lemma_.lower()
        pos = _SPACY_POS_MAP.get(tok.pos_, "OTHER")
        is_proper = tok.pos_ == "PROPN"
        tokens.append(TokenInfo(surface=surface, lemma=lemma, pos=pos, is_proper=is_proper))
    return tokens


# ── NLTK path ─────────────────────────────────────────────────────────────────

_NLTK_POS_MAP = {
    "NN": "NOUN", "NNS": "NOUN", "NNP": "PROP", "NNPS": "PROP",
    "VB": "VERB", "VBD": "VERB", "VBG": "VERB",
    "VBN": "VERB", "VBP": "VERB", "VBZ": "VERB",
    "JJ": "ADJ", "JJR": "ADJ", "JJS": "ADJ",
    "RB": "ADV", "RBR": "ADV", "RBS": "ADV",
}
_WN_POS_MAP = {
    "VERB": "v", "ADJ": "a", "ADV": "r",
}

def _tokenise_nltk(text: str) -> List[TokenInfo]:
    lem = _load_nltk()
    tokens = []
    words = _WORD_RE.findall(text)
    if lem and lem is not False:
        try:
            from nltk import pos_tag
            tagged = pos_tag(words)
        except Exception:
            tagged = [(w, "NN") for w in words]

        for word, tag in tagged:
            surface = word.lower()
            pos = _NLTK_POS_MAP.get(tag, "OTHER")
            wn_pos = _WN_POS_MAP.get(pos, "n")
            lemma = lem.lemmatize(surface, pos=wn_pos)
            is_proper = tag in ("NNP", "NNPS")
            tokens.append(TokenInfo(surface=surface, lemma=lemma, pos=pos, is_proper=is_proper))
    else:
        # Absolute fallback: no lemmatisation
        for word in words:
            surface = word.lower()
            tokens.append(TokenInfo(surface=surface, lemma=surface, pos="OTHER", is_proper=False))

    return tokens


# ── Public API ────────────────────────────────────────────────────────────────

def tokenise(text: str) -> List[TokenInfo]:
    """
    Normalise *text* and return a list of TokenInfo.
    Automatically selects spaCy → NLTK → fallback based on availability.
    """
    text = _normalise(text)
    nlp = _load_spacy()
    if nlp:
        return _tokenise_spacy(text)
    return _tokenise_nltk(text)


def filter_tokens(
    tokens: List[TokenInfo],
    *,
    min_length: int = 2,
    filter_stopwords: bool = False,
    keep_proper_nouns: bool = True,
    filter_numbers: bool = True,
    filter_basic_words: bool = False,
    basic_words_threshold: float = 5.7,
) -> List[TokenInfo]:
    """
    Remove tokens that don't meet the requested criteria.
    Stopword list is loaded from NLTK if available.
    """
    _load_nltk()   # ensure _stopwords is populated if NLTK present

    result = []
    for tok in tokens:
        # Length filter
        if len(tok.surface) < min_length:
            continue
        # Proper noun filter
        if not keep_proper_nouns and tok.is_proper:
            continue
        # Stopword filter
        if filter_stopwords and tok.lemma in _stopwords:
            continue
        # Basic/common vocabulary filter
        if filter_basic_words and is_basic_word(tok.lemma, threshold=basic_words_threshold):
            continue
        result.append(tok)

    return result
