"""
LLM-powered text cleaner for OCR output.

OCR results — especially from scanned exams — often contain noise:
broken hyphenation, garbled characters, merged lines, mangled option labels.
This service uses a configurable LLM (via the unified llm_client) to clean up
the raw OCR text before structure recognition and NLP analysis.

Supported backends (configured via runtime config):
  - claude    — Anthropic Claude (auto prompt caching for long system prompt)
  - deepseek  — DeepSeek API
  - openai    — Any OpenAI-compatible endpoint
  - none      — Pass through unchanged (default, no API call)
"""
from __future__ import annotations

import logging

from backend.prompts import get_prompt
from backend.services.runtime_config import get_runtime_config
from backend.utils.llm_client import chat
from backend.utils.retry import RetryPolicy

logger = logging.getLogger(__name__)

# Text cleaning retries less aggressively than vocab — long prompts, but single
# failure just falls back to the raw OCR text (see clean_ocr_text).
_CLEAN_RETRY = RetryPolicy(max_attempts=2, initial_delay=2.0, max_delay=10.0)


async def clean_ocr_text(raw_text: str, backend: str = "none", context: str = "") -> str:
    """
    Clean OCR text using the specified LLM backend.

    Args:
        raw_text: Raw OCR output
        backend: "claude" | "deepseek" | "openai" | "none"
        context: Optional hint (e.g. "high school English exam, multiple choice")

    Returns:
        Cleaned text (or original if backend="none" or cleaning fails).
    """
    backend = (backend or "none").strip().lower()
    if backend == "none" or not raw_text.strip():
        return raw_text
    if backend not in {"claude", "deepseek", "openai"}:
        logger.warning("Unknown text cleaner backend: %s — skipping", backend)
        return raw_text

    runtime = get_runtime_config()
    system_prompt = get_prompt("text_cleaner", domain="gaokao", version="v2")
    user_message = _build_user_message(raw_text, context)

    if backend == "claude":
        model = runtime.ai_model
    elif backend == "deepseek":
        model = runtime.llm.deepseek_model
    else:
        model = runtime.llm.openai_model

    try:
        response = await chat(
            provider=backend,
            model=model,
            system=system_prompt,
            user=user_message,
            max_tokens=8192,
            temperature=0.1,
            use_prompt_cache=True,        # long system prompt, cached across calls
            retry_policy=_CLEAN_RETRY,
            label=f"text-clean:{backend}",
        )
        cleaned = response.text.strip()
        logger.info(
            "Text cleaner (%s): %d → %d chars [%s]",
            backend, len(raw_text), len(cleaned), response.usage_summary,
        )
        return cleaned
    except Exception as exc:   # noqa: BLE001
        logger.error(
            "Text cleaning failed with backend=%s: %s — returning original text",
            backend, exc,
        )
        return raw_text


def _build_user_message(raw_text: str, context: str) -> str:
    ctx_hint = context or "Chinese high school English exam (高考), multiple choice + reading"
    return (
        f"Exam type: {ctx_hint}\n\n"
        "Please restore the following raw OCR text from this exam paper. "
        "Fix OCR errors, restore exam structure, and ensure each answer option "
        "(A/B/C/D) is on its own line:\n\n"
        "===BEGIN OCR TEXT===\n"
        f"{raw_text}"
        "\n===END OCR TEXT==="
    )
