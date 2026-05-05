"""
Single-word, multi-source dictionary lookup.

Used by:
  * the public `/api/lookup` endpoint  — interactive on-page lookup
  * the admin "live lookup" tester     — provider verification

Aggregates results from multiple providers in parallel, returning a
normalised payload that the frontend can render directly.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from backend.models.schemas import LemmaEntry, VocabEntry
from backend.services.vocabulary_generator import _REGISTRY  # noqa: F401  intentional reuse

logger = logging.getLogger(__name__)


# Sources we consider "online" for the live lookup feature. Order matters:
# the first registered source becomes the default when the user does not pin one.
DEFAULT_LOOKUP_SOURCES: tuple[str, ...] = (
    "iciba", "free_dict", "ecdict", "merriam_webster", "youdao",
)


def available_lookup_sources() -> List[str]:
    """All registered providers, deduplicated, in our preferred display order."""
    in_registry = list(_REGISTRY.keys())
    ordered: List[str] = []
    for name in DEFAULT_LOOKUP_SOURCES:
        if name in in_registry and name not in ordered:
            ordered.append(name)
    for name in in_registry:
        if name not in ordered and name not in {"claude", "deepseek", "openai"}:
            # Hide LLM providers from interactive single-word lookup —
            # they're billable and overkill for one word.
            ordered.append(name)
    return ordered


def _make_dummy_entry(word: str) -> LemmaEntry:
    return LemmaEntry(
        lemma=word,
        pos="",
        family_id=word,
        body_count=0,
        stem_count=0,
        option_count=0,
        total_count=0,
        score=0.0,
    )


async def _lookup_with(provider_name: str, word: str) -> Dict[str, Any]:
    """Invoke a single provider in isolation and serialise its result."""
    if provider_name not in _REGISTRY:
        return {
            "source": provider_name,
            "ok": False,
            "error": f"provider '{provider_name}' not registered",
            "latency_ms": 0,
        }
    started = time.monotonic()
    try:
        provider = _REGISTRY[provider_name]()
    except Exception as exc:   # noqa: BLE001
        return {
            "source": provider_name,
            "ok": False,
            "error": f"provider unavailable: {exc}",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    try:
        results: List[VocabEntry] = await provider.enrich([_make_dummy_entry(word)])
    except Exception as exc:   # noqa: BLE001
        return {
            "source": provider_name,
            "ok": False,
            "error": str(exc),
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    if not results:
        return {
            "source": provider_name,
            "ok": False,
            "error": "empty response",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    entry = results[0]
    has_real = (
        entry.source == provider_name
        and (entry.english_definition or entry.chinese_meaning or entry.example_sentence)
    )
    return {
        "source": provider_name,
        "ok": bool(has_real),
        "latency_ms": int((time.monotonic() - started) * 1000),
        "result": entry.model_dump(),
    }


async def lookup_word(
    word: str,
    sources: Optional[List[str]] = None,
    *,
    concurrency: int = 4,
) -> Dict[str, Any]:
    """
    Aggregate dictionary lookups for a single word.

    Returns:
        {
          "word": ...,
          "results": [ {source, ok, result|error, latency_ms}, ... ],
          "best": <source name>  # first ok=True result
        }
    """
    word = (word or "").strip()
    if not word:
        return {"word": word, "results": [], "best": None}

    chosen = [s.strip().lower() for s in (sources or available_lookup_sources()) if s and s.strip()]
    chosen = [s for s in chosen if s in _REGISTRY]
    if not chosen:
        return {"word": word, "results": [], "best": None}

    sem = asyncio.Semaphore(concurrency)

    async def _bounded(name: str) -> Dict[str, Any]:
        async with sem:
            return await _lookup_with(name, word)

    results = await asyncio.gather(*(_bounded(s) for s in chosen))
    best = next((r["source"] for r in results if r.get("ok")), None)
    return {"word": word, "results": results, "best": best}
