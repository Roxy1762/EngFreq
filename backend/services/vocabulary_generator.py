"""
Vocabulary generator — selects the configured provider and drives enrichment.

Pipeline:
  1. Sort lemmas by learning priority (exam score + rarity + option weight)
  2. [Optional] AI preprocess: fix OCR spelling errors + re-rank by exam importance
  3. Enrich top-N with definitions/examples via the configured provider
  4. Tag word levels (基础/高考/四六级/超纲) and CEFR levels (A1-C2)
  5. If the chosen LLM provider fails, automatically fall back to the next one
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

from backend.models.schemas import LemmaEntry, VocabEntry
from backend.prompts import get_prompt
from backend.providers.base_provider import BaseVocabProvider
from backend.services.basic_vocab import zipf_score
from backend.services.runtime_config import get_runtime_config
from backend.utils.json_parse import parse_json_object
from backend.utils.llm_client import chat, is_llm_provider, resolve_active_llm

logger = logging.getLogger(__name__)


# ── Provider registry ─────────────────────────────────────────────────────────

def _build_registry() -> dict[str, type[BaseVocabProvider]]:
    registry: dict[str, type] = {}

    _try_register(registry, "claude",          "backend.providers.claude_provider",             "ClaudeProvider")
    _try_register(registry, "deepseek",        "backend.providers.deepseek_provider",           "DeepSeekProvider")
    _try_register(registry, "openai",          "backend.providers.openai_compatible_provider",  "OpenAICompatibleProvider")
    _try_register(registry, "free_dict",       "backend.providers.free_dict_provider",          "FreeDictProvider")
    _try_register(registry, "merriam_webster", "backend.providers.merriam_webster_provider",    "MerriamWebsterProvider")
    _try_register(registry, "youdao",          "backend.providers.youdao_provider",             "YoudaoProvider")
    _try_register(registry, "ecdict",          "backend.providers.ecdict_provider",             "ECDICTProvider")

    return registry


def _try_register(registry: dict, key: str, module: str, cls: str) -> None:
    try:
        mod = __import__(module, fromlist=[cls])
        registry[key] = getattr(mod, cls)
    except Exception as e:   # noqa: BLE001
        logger.debug("%s unavailable: %s", cls, e)


_REGISTRY = _build_registry()


# ── Provider fallback chain ───────────────────────────────────────────────────
# If the user's chosen provider is an LLM and fails, we try the next configured
# LLM, then drop back to free_dict (no API key needed).
_LLM_FALLBACK_ORDER = ("claude", "deepseek", "openai")


# ── Public API ────────────────────────────────────────────────────────────────

def available_providers() -> list[str]:
    return list(_REGISTRY.keys())


def _learning_priority(entry: LemmaEntry) -> tuple[float, float, float, float]:
    """Prefer high-impact but less-basic words for vocabulary sheets."""
    rarity_bonus = max(0.0, 6.5 - zipf_score(entry.lemma))
    pos_bonus = 0.4 if entry.pos in {"VERB", "ADJ", "ADV", "NOUN"} else 0.0
    option_bonus = min(entry.option_count, 5) * 0.15
    priority = float(entry.score) + rarity_bonus + pos_bonus + option_bonus
    return (priority, entry.score, entry.total_count, entry.option_count)


async def ai_preprocess_lemmas(
    lemma_table: List[LemmaEntry],
    top_n: int = 50,
    provider_name: Optional[str] = None,
) -> List[LemmaEntry]:
    """
    Optional AI pipeline step: fix OCR spelling errors and re-rank lemmas
    by 高考 exam importance before vocabulary enrichment.

    Returns a corrected, re-ranked list of at most *top_n* lemmas.
    Falls back to the deterministic priority sort if AI call fails.
    """
    runtime = get_runtime_config()
    pname = provider_name or runtime.vocab_provider

    # Feed 3× top_n candidates so the AI has room to select and re-rank
    sorted_lemmas = sorted(lemma_table, key=_learning_priority, reverse=True)
    pool_size = min(len(sorted_lemmas), top_n * 3)
    candidates = sorted_lemmas[:pool_size]

    if not is_llm_provider(pname):
        logger.info("AI preprocess: provider '%s' is not an LLM, skipping rerank", pname)
        return sorted_lemmas[:top_n]

    word_list = [
        {
            "word": e.lemma,
            "score": round(e.score, 2),
            "option_count": e.option_count,
            "stem_count": e.stem_count,
            "body_count": e.body_count,
        }
        for e in candidates
    ]
    user_msg = (
        f"Requested output size: {top_n} words\n\n"
        f"Word list from exam ({len(word_list)} candidates):\n"
        f"{json.dumps(word_list, ensure_ascii=False, indent=2)}"
    )
    system_prompt = get_prompt("rerank", domain="gaokao", version="v2")

    try:
        provider, model = resolve_active_llm(pname)
        response = await chat(
            provider=provider,
            model=model,
            system=system_prompt,
            user=user_msg,
            max_tokens=2048,
            temperature=0.2,
            use_prompt_cache=True,
            label=f"{provider}-rerank:{len(word_list)}w",
        )
    except Exception as exc:   # noqa: BLE001
        logger.warning("AI preprocess failed (%s): %s — using priority sort", pname, exc)
        return sorted_lemmas[:top_n]

    data = parse_json_object(response.text)
    ai_words = data.get("words", []) if isinstance(data, dict) else []
    corrections = data.get("corrections", {}) if isinstance(data, dict) else {}

    if not ai_words:
        logger.warning("AI preprocess returned empty word list, falling back")
        return sorted_lemmas[:top_n]

    # Build lookup from original lemma → LemmaEntry
    lemma_map = {e.lemma: e for e in candidates}
    corrected_map: dict[str, LemmaEntry] = {}

    for orig, corrected in (corrections or {}).items():
        if orig in lemma_map and isinstance(corrected, str) and corrected:
            base = lemma_map[orig]
            corrected_map[corrected] = LemmaEntry(
                lemma=corrected,
                pos=base.pos,
                family_id=base.family_id,
                surface_forms=base.surface_forms,
                body_count=base.body_count,
                stem_count=base.stem_count,
                option_count=base.option_count,
                total_count=base.total_count,
                score=base.score,
            )
            logger.info("AI corrected spelling: '%s' → '%s'", orig, corrected)

    result: List[LemmaEntry] = []
    for word in ai_words[:top_n]:
        if not isinstance(word, str):
            continue
        if word in corrected_map:
            result.append(corrected_map[word])
        elif word in lemma_map:
            result.append(lemma_map[word])
        # unknown word → model hallucination, skip

    logger.info(
        "AI preprocess: %d candidates → %d selected, %d corrections",
        len(candidates), len(result), len(corrections),
    )
    return result or sorted_lemmas[:top_n]


# ── Fallback chain helpers ────────────────────────────────────────────────────

def _build_fallback_chain(primary: str) -> list[str]:
    """Given a primary provider, construct a sensible fallback chain."""
    chain: list[str] = [primary]

    runtime = get_runtime_config()
    llm = runtime.llm

    # Only enqueue providers that are actually configured (key present)
    configured = []
    if llm.anthropic_api_key:
        configured.append("claude")
    if llm.deepseek_api_key:
        configured.append("deepseek")
    if llm.openai_api_key:
        configured.append("openai")

    for name in _LLM_FALLBACK_ORDER:
        if name != primary and name in configured and name in _REGISTRY:
            chain.append(name)

    if "free_dict" in _REGISTRY and "free_dict" not in chain:
        chain.append("free_dict")      # always-available last resort

    return chain


async def _enrich_with_chain(
    provider_chain: list[str],
    lemmas: List[LemmaEntry],
    context_text: str,
) -> tuple[List[VocabEntry], str]:
    """Walk the fallback chain until one provider succeeds."""
    last_exc: Optional[Exception] = None
    for name in provider_chain:
        if name not in _REGISTRY:
            continue
        try:
            provider: BaseVocabProvider = _REGISTRY[name]()
        except Exception as exc:   # noqa: BLE001
            logger.info("Provider '%s' unavailable (%s), trying next", name, exc)
            last_exc = exc
            continue

        logger.info(
            "Generating vocabulary for %d words via '%s'",
            len(lemmas), name,
        )
        try:
            vocab = await provider.enrich(lemmas, context_text=context_text)
            if vocab:
                return vocab, name
            logger.warning("Provider '%s' returned empty vocab list, trying next", name)
        except Exception as exc:   # noqa: BLE001
            logger.warning("Provider '%s' raised %s, trying fallback", name, exc)
            last_exc = exc

    if last_exc:
        raise RuntimeError(f"All providers in chain {provider_chain} failed") from last_exc
    raise RuntimeError(f"No working provider in chain: {provider_chain}")


async def generate_vocabulary(
    lemma_table: List[LemmaEntry],
    context_text: str = "",
    top_n: int = 50,
    provider_name: Optional[str] = None,
    ai_preprocess: Optional[bool] = None,
) -> List[VocabEntry]:
    """
    Enrich the top-*top_n* lemmas with definitions and Chinese translations.

    Pipeline:
    1. Sort by learning priority
    2. [Optional] AI preprocess: fix OCR errors + re-rank by exam importance
    3. Enrich via the chosen provider (with automatic fallback on failure)
    4. Tag word levels + CEFR + Zipf score
    """
    runtime = get_runtime_config()
    pname = provider_name or runtime.vocab_provider

    should_preprocess = ai_preprocess
    if should_preprocess is None:
        should_preprocess = getattr(runtime, "ai_preprocess_enabled", False)

    if should_preprocess and is_llm_provider(pname):
        logger.info("Running AI vocabulary pre-processing (rerank + spell-fix)…")
        candidates = await ai_preprocess_lemmas(lemma_table, top_n=top_n, provider_name=pname)
    else:
        candidates = sorted(lemma_table, key=_learning_priority, reverse=True)[:top_n]

    if pname not in _REGISTRY:
        logger.warning(
            "Provider '%s' not in registry (%s). Falling back to free_dict.",
            pname, list(_REGISTRY.keys()),
        )
        pname = "free_dict"

    chain = _build_fallback_chain(pname)
    logger.info("Vocabulary provider chain: %s", chain)

    vocab, used = await _enrich_with_chain(chain, candidates, context_text)
    if used != pname:
        logger.warning("Primary provider '%s' failed — used fallback '%s'", pname, used)

    # Enrich metadata: word_level, cefr_level, zipf_score
    _tag_metadata(vocab)

    vocab.sort(
        key=lambda v: (
            _learning_priority(
                LemmaEntry(
                    lemma=v.lemma,
                    pos=v.pos or "",
                    family_id=v.family,
                    body_count=v.body_count,
                    stem_count=v.stem_count,
                    option_count=v.option_count,
                    total_count=v.total_count,
                    score=v.score,
                )
            ),
            v.headword,
        ),
        reverse=True,
    )
    return vocab


def _tag_metadata(vocab: List[VocabEntry]) -> None:
    """Populate zipf_score, word_level, cefr_level (if missing)."""
    try:
        from backend.services.wordlist_service import tag_vocab_entries
        tag_vocab_entries(vocab)
    except Exception as exc:   # noqa: BLE001
        logger.debug("Word level tagging skipped: %s", exc)

    for entry in vocab:
        target = (entry.headword or entry.lemma or "").strip().lower()
        if not target:
            continue
        if entry.zipf_score is None:
            entry.zipf_score = round(zipf_score(target), 2)
        if not entry.cefr_level:
            try:
                from backend.services.wordlist_service import get_cefr_level
                entry.cefr_level = get_cefr_level(target)
            except Exception as exc:   # noqa: BLE001
                logger.debug("CEFR tagging skipped for %s: %s", target, exc)
