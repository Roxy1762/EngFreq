"""Helpers for identifying very common/basic English vocabulary."""
from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

_FALLBACK_BASIC_WORDS = {
    "a", "about", "after", "again", "all", "also", "always", "am", "an", "and", "any",
    "are", "around", "as", "ask", "at", "away", "back", "be", "because", "been",
    "before", "begin", "between", "big", "book", "both", "boy", "bring", "brother",
    "but", "buy", "by", "call", "can", "carry", "change", "child", "city", "class",
    "close", "come", "company", "country", "day", "do", "door", "down", "drink",
    "drive", "during", "each", "early", "eat", "end", "enough", "even", "every",
    "example", "eye", "face", "family", "far", "father", "feel", "few", "find",
    "first", "for", "friend", "from", "game", "get", "girl", "give", "go", "good",
    "great", "group", "grow", "hand", "have", "he", "head", "help", "her", "here",
    "high", "him", "his", "home", "how", "house", "i", "if", "important", "in",
    "interest", "into", "is", "it", "its", "job", "just", "keep", "kind", "know",
    "large", "last", "late", "learn", "leave", "left", "let", "life", "like", "line",
    "little", "live", "long", "look", "lot", "love", "make", "man", "many", "may",
    "me", "mean", "meet", "might", "miss", "more", "most", "mother", "move", "much",
    "music", "must", "my", "name", "need", "never", "new", "next", "night", "no",
    "not", "now", "number", "of", "off", "old", "on", "one", "only", "open", "or",
    "other", "our", "out", "over", "own", "part", "people", "place", "play", "point",
    "put", "question", "read", "really", "right", "room", "run", "same", "say", "school",
    "see", "seem", "she", "should", "show", "small", "so", "some", "sometimes", "son",
    "start", "state", "still", "story", "student", "such", "take", "talk", "teacher",
    "tell", "than", "that", "the", "their", "them", "then", "there", "these", "they",
    "thing", "think", "this", "those", "time", "to", "today", "together", "too", "try",
    "turn", "two", "under", "up", "us", "use", "very", "want", "way", "we", "well",
    "what", "when", "where", "which", "who", "why", "will", "with", "without", "woman",
    "word", "work", "world", "would", "write", "year", "you", "young", "your",
}


@lru_cache(maxsize=4096)
def zipf_score(word: str) -> float:
    word = (word or "").strip().lower()
    if not word:
        return 0.0

    try:
        from wordfreq import zipf_frequency

        return float(zipf_frequency(word, "en"))
    except Exception as exc:
        logger.debug("wordfreq unavailable for %s: %s", word, exc)
        return 7.0 if word in _FALLBACK_BASIC_WORDS else 0.0


def is_basic_word(word: str, threshold: float = 5.7) -> bool:
    word = (word or "").strip().lower()
    if not word:
        return False
    if word in _FALLBACK_BASIC_WORDS:
        return True
    return zipf_score(word) >= threshold
