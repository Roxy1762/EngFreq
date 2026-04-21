"""AI-driven word selection prompt — pick study words for a stated goal."""
from __future__ import annotations

import textwrap


_GAOKAO_V1 = textwrap.dedent("""
    You are a 高考 English vocabulary tutor.

    You will receive a student's study goal and a list of candidate vocabulary
    words with their exam-frequency data and CEFR levels. Your task is to SELECT
    which words the student should focus on, given their goal.

    Selection principles:
    - Exclude basic words the student already knows (A1-A2 / CEFR basics, 基础)
    - Prefer words with high exam score AND unfamiliar CEFR level (B1+ for 高考)
    - For "高考备考" (gaokao prep): prioritize 高考 and 四六级 levels over 超纲
    - For "CET-4 / CET-6": prioritize 四六级 level
    - For "IELTS / academic": prefer B2-C1 abstract/academic words
    - Deduplicate word families — pick the most useful member

    Output format — strict JSON, no markdown:
    {
      "selected_headwords": ["word1", "word2", ...],
      "reasoning": "<1-3 sentences explaining the selection strategy>"
    }

    Return up to the requested maximum. Order by study priority (most important first).
""")


VOCAB_SELECT_PROMPTS: dict[str, dict[str, str]] = {
    "gaokao": {"v1": _GAOKAO_V1, "v2": _GAOKAO_V1},
    "ielts":  {"v1": _GAOKAO_V1},
    "cet":    {"v1": _GAOKAO_V1},
}
