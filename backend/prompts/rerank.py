"""Lemma re-rank prompts — AI pre-processing before vocab enrichment."""
from __future__ import annotations

import textwrap


_GAOKAO_V2 = textwrap.dedent("""
    You are a 高考 (Chinese college entrance exam) English vocabulary expert.

    You will receive a list of English words extracted from an exam paper via NLP.
    The list may contain:
      - OCR spelling errors (e.g. "becorne", "irnportant", "cornplete")
      - Irrelevant fragments (numbers, punctuation, single letters)
      - Valid words mixed in

    Your tasks:
    1. CORRECT obvious OCR spelling errors. Use context (exam vocabulary) to decide.
    2. REMOVE entries that are clearly invalid: gibberish, numbers, punctuation
       fragments, single letters, words that cannot exist in English.
    3. RE-RANK the cleaned list by exam study priority:
       - First: 高考核心词汇 — core, testable, yet unfamiliar
       - Second: 超纲 words that appear frequently in this exam
       - Last: 基础词汇 — everyday words (the, go, big, year, make, good)
    4. Return at most the requested number of words.

    Output format — JSON only, no markdown, no prose:
    {
      "words": ["corrected_word1", "corrected_word2", ...],
      "corrections": {"original_misspelled": "corrected", ...},
      "reasoning": "<optional 1-2 sentence explanation of the ranking approach>"
    }

    The "corrections" map is optional — include ONLY genuinely corrected spellings.
    The "words" array is the final ordered list (most important first).
    The "reasoning" field is optional.
""")


_GAOKAO_V1 = textwrap.dedent("""
    You are a 高考 English vocabulary expert. Given a word list from an exam paper:
    1. Fix OCR spelling errors
    2. Remove gibberish / irrelevant fragments
    3. Re-rank by 高考 study priority: core-high-value first, common words last
    4. Return at most N words

    Return JSON: {"words": [...], "corrections": {"orig": "fixed", ...}}
    No markdown, no prose.
""")


RERANK_PROMPTS: dict[str, dict[str, str]] = {
    "gaokao": {"v1": _GAOKAO_V1, "v2": _GAOKAO_V2},
    "ielts":  {"v2": _GAOKAO_V2},
    "cet":    {"v2": _GAOKAO_V2},
}
