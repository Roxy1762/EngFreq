"""
Vocabulary generator — selects the configured provider and drives enrichment.

Provider registry is a simple dict; adding a new provider only requires
registering it here, not touching any other module.
"""
from __future__ import annotations

import logging
from typing import List

from backend.config import settings
from backend.models.schemas import LemmaEntry, VocabEntry
from backend.providers.base_provider import BaseVocabProvider
from backend.services.basic_vocab import zipf_score
from backend.services.runtime_config import get_runtime_config

logger = logging.getLogger(__name__)


# ── Provider registry ─────────────────────────────────────────────────────────

def _build_registry() -> dict[str, type[BaseVocabProvider]]:
    registry: dict[str, type] = {}

    try:
        from backend.providers.claude_provider import ClaudeProvider
        registry["claude"] = ClaudeProvider
    except Exception as e:
        logger.debug(f"ClaudeProvider unavailable: {e}")

    try:
        from backend.providers.deepseek_provider import DeepSeekProvider
        registry["deepseek"] = DeepSeekProvider
    except Exception as e:
        logger.debug(f"DeepSeekProvider unavailable: {e}")

    try:
        from backend.providers.openai_compatible_provider import OpenAICompatibleProvider
        registry["openai"] = OpenAICompatibleProvider
    except Exception as e:
        logger.debug(f"OpenAICompatibleProvider unavailable: {e}")

    try:
        from backend.providers.free_dict_provider import FreeDictProvider
        registry["free_dict"] = FreeDictProvider
    except Exception as e:
        logger.debug(f"FreeDictProvider unavailable: {e}")

    try:
        from backend.providers.merriam_webster_provider import MerriamWebsterProvider
        registry["merriam_webster"] = MerriamWebsterProvider
    except Exception as e:
        logger.debug(f"MerriamWebsterProvider unavailable: {e}")

    try:
        from backend.providers.youdao_provider import YoudaoProvider
        registry["youdao"] = YoudaoProvider
    except Exception as e:
        logger.debug(f"YoudaoProvider unavailable: {e}")

    try:
        from backend.providers.ecdict_provider import ECDICTProvider
        registry["ecdict"] = ECDICTProvider
    except Exception as e:
        logger.debug(f"ECDICTProvider unavailable: {e}")

    return registry


_REGISTRY = _build_registry()


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


async def generate_vocabulary(
    lemma_table: List[LemmaEntry],
    context_text: str = "",
    top_n: int = 50,
    provider_name: str | None = None,
) -> List[VocabEntry]:
    """
    Enrich the top-*top_n* lemmas (by score) with definitions and examples.

    Args:
        lemma_table:   Full lemma table (will be sorted and sliced internally).
        context_text:  Raw exam text for example extraction.
        top_n:         How many words to enrich.
        provider_name: Override the default provider from config.

    Returns:
        List of VocabEntry sorted by score descending.
    """
    # Select and instantiate provider
    pname = provider_name or get_runtime_config().vocab_provider
    if pname not in _REGISTRY:
        logger.warning(
            f"Provider '{pname}' not in registry ({list(_REGISTRY.keys())}). "
            f"Falling back to free_dict."
        )
        pname = "free_dict"

    try:
        provider: BaseVocabProvider = _REGISTRY[pname]()
    except Exception as e:
        raise RuntimeError(f"Cannot instantiate provider '{pname}': {e}") from e

    # Sort by learning priority and take top N.
    sorted_lemmas = sorted(lemma_table, key=_learning_priority, reverse=True)
    candidates = sorted_lemmas[:top_n]

    logger.info(
        f"Generating vocabulary for {len(candidates)} words via '{pname}'"
    )

    vocab = await provider.enrich(candidates, context_text=context_text)

    # Tag word levels if not already set by provider
    try:
        from backend.services.wordlist_service import tag_vocab_entries
        tag_vocab_entries(vocab)
    except Exception as _e:
        logger.debug("Word level tagging skipped: %s", _e)

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
