"""
LLM-powered text cleaner for OCR output.

OCR results — especially from scanned exams — often contain noise:
broken hyphenation, garbled characters, merged lines, etc.
This service uses a configurable LLM to clean up the raw OCR text
before it goes through structure recognition and NLP analysis.

Supported backends (configured via runtime config):
  - claude    — Anthropic Claude
  - deepseek  — DeepSeek API
  - openai    — Any OpenAI-compatible endpoint
  - none      — Pass through unchanged (default, no API call)
"""
from __future__ import annotations

import logging
import textwrap
from typing import Optional

logger = logging.getLogger(__name__)

_CLEAN_SYSTEM_PROMPT = textwrap.dedent("""
    You are a specialized text restoration expert for Chinese high school and college entrance
    exam (高考) papers. You receive raw OCR output from scanned English exam papers.

    ## Common OCR Problems to Fix
    - Character confusion: 0↔O, 1↔l↔I, rn↔m, vv↔w, cl↔d, li↔h, ii↔u
    - Broken hyphenation: "im-portant" → "important", "be-cause" → "because"
    - Missing spaces: "Inthepast" → "In the past", "whichof" → "which of"
    - Extra spaces: "com plete" → "complete", "a ble" → "able"
    - Merged lines: two sentences run together without newline
    - Mangled option labels: "A，" / "A:" / "(A)" → normalize to "A. "
    - Stray characters from scan artifacts: isolated symbols, repeated dashes

    ## Exam Structure to Preserve and Restore
    Chinese English exams always follow this structure — restore it if broken:
    1. Part / Section headers (e.g. "Part I  Reading Comprehension")
    2. Instruction line (e.g. "Directions: Read the passage and answer the questions.")
    3. Passage / dialogue (body text — keep paragraphs)
    4. Question stems (numbered: "1.", "2.", etc.)
    5. Answer options on SEPARATE lines, each starting with "A. " "B. " "C. " "D. "
       - If all 4 options appear on one line, split them onto 4 separate lines
       - Option labels must be uppercase: A, B, C, D

    ## Rules
    1. Preserve ALL content — never delete words, sentences, or options
    2. Fix only clear OCR errors — do not rephrase or simplify
    3. Do NOT translate any English text to Chinese or vice versa
    4. Do NOT add explanations, comments, or headers
    5. Return ONLY the cleaned exam text

    Output the restored exam text directly, nothing else.
""").strip()


async def clean_ocr_text(raw_text: str, backend: str = "none", context: str = "") -> str:
    """
    Clean OCR text using the specified LLM backend.

    Args:
        raw_text: Raw OCR output
        backend: "claude" | "deepseek" | "openai" | "none"
        context: Optional hint (e.g. "high school English exam, multiple choice")

    Returns:
        Cleaned text (or original if backend="none" or cleaning fails)
    """
    if backend == "none" or not raw_text.strip():
        return raw_text

    try:
        if backend == "claude":
            return await _clean_with_claude(raw_text, context)
        elif backend == "deepseek":
            return await _clean_with_deepseek(raw_text, context)
        elif backend == "openai":
            return await _clean_with_openai(raw_text, context)
        else:
            logger.warning("Unknown text cleaner backend: %s, skipping", backend)
            return raw_text
    except Exception as exc:
        logger.error("Text cleaning failed with backend=%s: %s — returning original text", backend, exc)
        return raw_text


def _build_user_message(raw_text: str, context: str) -> str:
    ctx_hint = context or "Chinese high school English exam (高考), multiple choice + reading"
    msg = (
        f"Exam type: {ctx_hint}\n\n"
        "Please restore the following raw OCR text from this exam paper. "
        "Fix OCR errors, restore exam structure, and ensure each answer option "
        "(A/B/C/D) is on its own line:\n\n"
        "===BEGIN OCR TEXT===\n"
        + raw_text +
        "\n===END OCR TEXT==="
    )
    return msg


async def _clean_with_claude(raw_text: str, context: str) -> str:
    from backend.services.runtime_config import get_runtime_config
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed")

    runtime = get_runtime_config()
    llm = runtime.llm
    client = anthropic.AsyncAnthropic(api_key=llm.anthropic_api_key)
    response = await client.messages.create(
        model=runtime.ai_model,
        max_tokens=8192,
        system=_CLEAN_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(raw_text, context)}],
    )
    cleaned = response.content[0].text.strip()
    logger.info("Claude text cleaner: %d → %d chars", len(raw_text), len(cleaned))
    return cleaned


async def _clean_with_deepseek(raw_text: str, context: str) -> str:
    from backend.services.runtime_config import get_runtime_config
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError("openai package not installed: pip install openai")

    llm = get_runtime_config().llm
    client = AsyncOpenAI(api_key=llm.deepseek_api_key, base_url=llm.deepseek_base_url)
    response = await client.chat.completions.create(
        model=llm.deepseek_model,
        messages=[
            {"role": "system", "content": _CLEAN_SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(raw_text, context)},
        ],
        max_tokens=8192,
        temperature=0.1,
    )
    cleaned = response.choices[0].message.content.strip()
    logger.info("DeepSeek text cleaner: %d → %d chars", len(raw_text), len(cleaned))
    return cleaned


async def _clean_with_openai(raw_text: str, context: str) -> str:
    from backend.services.runtime_config import get_runtime_config
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise RuntimeError("openai package not installed: pip install openai")

    llm = get_runtime_config().llm
    kwargs: dict = {"api_key": llm.openai_api_key}
    if llm.openai_base_url:
        kwargs["base_url"] = llm.openai_base_url

    client = AsyncOpenAI(**kwargs)
    response = await client.chat.completions.create(
        model=llm.openai_model,
        messages=[
            {"role": "system", "content": _CLEAN_SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_message(raw_text, context)},
        ],
        max_tokens=8192,
        temperature=0.1,
    )
    cleaned = response.choices[0].message.content.strip()
    logger.info("OpenAI text cleaner: %d → %d chars", len(raw_text), len(cleaned))
    return cleaned
