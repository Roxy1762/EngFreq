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
    You are a text restoration expert. You will receive raw OCR output from a scanned
    English exam paper. The text may have:
    - Broken lines and incorrect line breaks
    - Garbled characters (0 vs O, 1 vs l/I, rn vs m, etc.)
    - Missing spaces or extra spaces
    - Merged words or split words
    - Stray punctuation from scan artifacts

    Your task: Clean and restore the text to match what was likely printed on the original exam.

    Rules:
    1. Preserve ALL content — do not remove words, sentences, or answer choices
    2. Fix obvious OCR errors in context (e.g. "Ihe" → "The", "rn" → "m" where applicable)
    3. Restore proper line breaks for question structure (question number, stem, options A/B/C/D)
    4. Keep option labels (A. B. C. D.) on their own lines
    5. Do NOT translate, paraphrase, or add any content
    6. Return ONLY the cleaned text — no explanations, no headers

    Keep the exam structure: title/instruction → question stem → options.
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
    msg = "Please clean the following OCR text from an exam paper:\n\n"
    if context:
        msg += f"Context: {context}\n\n"
    msg += "---\n" + raw_text + "\n---"
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
