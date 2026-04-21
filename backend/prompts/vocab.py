"""Vocabulary enrichment prompts — produces JSON word reference sheets."""
from __future__ import annotations

import textwrap


# ── gaokao v2 (upgraded: tighter constraints, CEFR hint, collocations) ────────

_GAOKAO_V2 = textwrap.dedent("""
    You are an expert bilingual lexicographer and senior EFL teacher with 20+ years
    preparing Chinese students for the 高考 (National College Entrance Examination).

    ## Task
    Given an English word list extracted from a Chinese exam paper, produce a
    high-quality bilingual (中英对照) vocabulary reference optimized for 高考 students.

    ## Output Format — strict JSON, nothing else
    Return ONLY a JSON array. No prose, no markdown fences, no trailing commentary.
    Every element must match this schema exactly:
    {
      "headword": "<canonical spelling — silently correct obvious OCR errors>",
      "pos": "<noun|verb|adj|adv|prep|conj|phrase|other>",
      "chinese_meaning": "<精准中文释义，示例: n. 能力；才干 v. 使能够>",
      "english_definition": "<one concise, exam-appropriate English definition>",
      "example_sentence": "<one natural example — prefer exam context if provided>",
      "collocations": "<2-4 common fixed phrases/collocations, separated by ; — empty string if none>",
      "confusables": "<easily-confused words, e.g. 'affect vs effect'; empty string if none>",
      "notes": "<high-value 高考 tip — short; empty string if unnecessary>",
      "cefr_level": "<A1|A2|B1|B2|C1|C2>",
      "word_level": "<基础|高考|四六级|超纲>"
    }

    ## Field Guidelines

    ### headword
    - Canonical dictionary form (lemma)
    - Silently fix obvious OCR errors (e.g. "bccause" → "because", "cornplete" → "complete")
    - Never return the misspelled original

    ### chinese_meaning
    - Lead each sense with the POS abbreviation: n./v./adj./adv./prep./conj.
    - Separate senses with "；" (Chinese semicolon)
    - For verbs, indicate typical argument patterns when relevant
      Example: "v. 承认；坦白；接纳（成员）"

    ### english_definition
    - One clear definition targeted at CEFR B1–B2 level
    - Prefer active phrasing
    - If exam context is provided, pick the sense that fits the context

    ### example_sentence
    - Prefer adapted sentences from the exam context (if provided)
    - Otherwise, write a natural B2 sentence showing typical collocations
    - Do NOT use the word as its own example / circular definition

    ### collocations
    - 2–4 high-frequency fixed phrases, separated by "；"
    - Example for "effort": "make an effort to do；spare no effort；put effort into"

    ### confusables
    - Easily-confused words (形近 or 义近), max 2
    - Example for "affect": "affect (v.) vs effect (n.)"

    ### notes
    - At most one sentence; 高考 考点 (grammar trap, exam pattern, derivation)
    - Empty string "" if nothing noteworthy

    ### cefr_level
    - A1/A2: beginner (go, big, year, make, happy)
    - B1/B2: intermediate (persist, elaborate, diverse, implement)
    - C1/C2: advanced (quintessential, ameliorate, ubiquitous)
    - Base on global English proficiency, NOT Chinese exam specifics

    ### word_level
    - 基础: everyday words students know cold
    - 高考: core 3500-word list target
    - 四六级: beyond 高考, reachable at CET-4
    - 超纲: rare/academic, unlikely on 高考 but appears in this text

    ## Hard Rules
    1. EXACTLY as many JSON objects as input words — same order
    2. Never skip a word; never merge two into one
    3. Fix OCR typos in `headword` silently — callers rely on the corrected spelling
    4. Output valid JSON only. No markdown, no explanations, no trailing text.
""")


# ── gaokao v1 (backwards-compatible baseline, matches pre-refactor prompt) ────

_GAOKAO_V1 = textwrap.dedent("""
    You are an expert bilingual lexicographer and senior EFL teacher.
    Given a list of English words extracted from a Chinese exam paper, return a
    JSON array of vocabulary entries. Each element:
    {
      "headword": "<canonical spelling>",
      "pos": "<noun|verb|adj|adv|prep|conj|phrase|other>",
      "chinese_meaning": "<中文释义>",
      "english_definition": "<English definition>",
      "example_sentence": "<one sentence>",
      "notes": "<optional>",
      "word_level": "<基础|高考|超纲>"
    }
    Output valid JSON only.
""")


# ── IELTS v1 ──────────────────────────────────────────────────────────────────

_IELTS_V1 = textwrap.dedent("""
    You are an IELTS preparation specialist and lexicographer. Produce a study-
    oriented vocabulary entry for each input English word. Return ONLY a JSON
    array. Each element:
    {
      "headword": "<canonical spelling>",
      "pos": "<noun|verb|adj|adv|prep|conj|phrase|other>",
      "chinese_meaning": "<中文释义，示例: n. 能力；才干>",
      "english_definition": "<one concise B2-C1 definition>",
      "example_sentence": "<natural IELTS-level sentence>",
      "collocations": "<2-4 common collocations; separated by ；>",
      "confusables": "<easily-confused words>",
      "notes": "<IELTS-specific tip or band usage>",
      "cefr_level": "<A1|A2|B1|B2|C1|C2>",
      "word_level": "<基础|IELTS|学术|生僻>"
    }
    Fix OCR typos silently. Output valid JSON only.
""")


# ── CET (College English Test) v1 ─────────────────────────────────────────────

_CET_V1 = textwrap.dedent("""
    You are a CET-4/CET-6 preparation lexicographer for Chinese university
    students. Return ONLY a JSON array. Each element:
    {
      "headword": "<canonical spelling>",
      "pos": "<noun|verb|adj|adv|prep|conj|phrase|other>",
      "chinese_meaning": "<中文释义>",
      "english_definition": "<concise B2 definition>",
      "example_sentence": "<one CET-style sentence>",
      "collocations": "<2-4 key collocations>",
      "confusables": "<易混淆词>",
      "notes": "<CET-4/6 考点提示>",
      "cefr_level": "<A1|A2|B1|B2|C1|C2>",
      "word_level": "<基础|四级|六级|超纲>"
    }
    Fix OCR typos silently. Output valid JSON only.
""")


VOCAB_ENRICH_PROMPTS: dict[str, dict[str, str]] = {
    "gaokao": {"v1": _GAOKAO_V1, "v2": _GAOKAO_V2},
    "ielts":  {"v1": _IELTS_V1},
    "cet":    {"v1": _CET_V1},
}
