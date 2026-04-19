"""
Vocabulary generator — selects the configured provider and drives enrichment.

Pipeline:
  1. Sort lemmas by learning priority
  2. [Optional] AI preprocess: fix OCR spelling errors + re-rank by exam importance
  3. Enrich top-N with definitions/examples via the configured provider
"""
from __future__ import annotations

import json
import logging
import textwrap
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


# ── AI pre-processing prompt ──────────────────────────────────────────────────

_RERANK_SYSTEM = textwrap.dedent("""
    You are a 高考 (Chinese college entrance exam) English vocabulary expert.

    You will receive a list of English words extracted from an exam paper via NLP,
    possibly containing OCR spelling errors or irrelevant fragments.

    Your tasks:
    1. CORRECT obvious OCR spelling errors (e.g. "becorne"→"become", "irnportant"→"important",
       "cornplete"→"complete", "sornething"→"something"). Use context (exam vocabulary) to decide.
    2. REMOVE entries that are clearly invalid: gibberish, single stray letters, numbers,
       punctuation fragments, or words that cannot exist in English.
    3. RE-RANK the cleaned list by exam study priority:
       - First: 高考核心词汇 (core 高考 vocab) — unfamiliar but highly testable words
       - Second: 超纲词汇 appearing frequently in this exam (worth noting)
       - Last: 基础词汇 (basic words everyone knows: the, go, big, year, make, good)
    4. Return at most the requested number of words.

    Return ONLY a valid JSON object — no markdown, no prose:
    {
      "words": ["corrected_word1", "corrected_word2", ...],
      "corrections": {"original": "corrected", ...}
    }

    The "corrections" map is optional — only include genuinely corrected spellings.
    The "words" array is the final ordered list (most important first).
""").strip()


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
    provider_name: str | None = None,
) -> List[LemmaEntry]:
    """
    Optional AI pipeline step: fix OCR spelling errors and re-rank lemmas
    by 高考 exam importance before vocabulary enrichment.

    Returns a corrected, re-ranked list of at most *top_n* lemmas.
    Falls back to the original priority sort if AI call fails.
    """
    runtime = get_runtime_config()
    pname = provider_name or runtime.vocab_provider

    # We feed 3× top_n candidates so AI has room to select and re-rank
    sorted_lemmas = sorted(lemma_table, key=_learning_priority, reverse=True)
    pool_size = min(len(sorted_lemmas), top_n * 3)
    candidates = sorted_lemmas[:pool_size]

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

    raw_response = None
    try:
        llm = runtime.llm
        if pname == "claude" and llm.anthropic_api_key:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=llm.anthropic_api_key)
            resp = await client.messages.create(
                model=runtime.ai_model,
                max_tokens=2048,
                system=_RERANK_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw_response = resp.content[0].text.strip()

        elif pname in ("deepseek", "openai"):
            try:
                from openai import AsyncOpenAI
            except ImportError:
                raise RuntimeError("openai package not installed")
            if pname == "deepseek" and llm.deepseek_api_key:
                client = AsyncOpenAI(api_key=llm.deepseek_api_key, base_url=llm.deepseek_base_url)
                model = llm.deepseek_model
            elif pname == "openai" and llm.openai_api_key:
                kwargs: dict = {"api_key": llm.openai_api_key}
                if llm.openai_base_url:
                    kwargs["base_url"] = llm.openai_base_url
                client = AsyncOpenAI(**kwargs)
                model = llm.openai_model
            else:
                raise RuntimeError(f"No API key configured for provider '{pname}'")
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _RERANK_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=2048,
                temperature=0.2,
            )
            raw_response = resp.choices[0].message.content.strip()

        else:
            logger.info("AI preprocess: provider '%s' not available, skipping rerank", pname)
            return sorted_lemmas[:top_n]

    except Exception as exc:
        logger.warning("AI preprocess failed (%s), using priority sort: %s", pname, exc)
        return sorted_lemmas[:top_n]

    # Parse AI response
    try:
        if raw_response.startswith("```"):
            raw_response = raw_response.split("```", 2)[1]
            if raw_response.startswith("json"):
                raw_response = raw_response[4:]
            raw_response = raw_response.rsplit("```", 1)[0].strip()

        data = json.loads(raw_response)
        ai_words: list[str] = data.get("words", [])
        corrections: dict[str, str] = data.get("corrections", {})

        if not ai_words:
            logger.warning("AI preprocess returned empty word list, falling back")
            return sorted_lemmas[:top_n]

        # Build lookup from original lemma → LemmaEntry
        lemma_map = {e.lemma: e for e in candidates}

        # Apply corrections: update lemma field in entries
        corrected_map: dict[str, LemmaEntry] = {}
        for orig, corrected in corrections.items():
            if orig in lemma_map:
                entry = lemma_map[orig]
                # Create a new entry with corrected lemma
                updated = LemmaEntry(
                    lemma=corrected,
                    pos=entry.pos,
                    family_id=entry.family_id,
                    surface_forms=entry.surface_forms,
                    body_count=entry.body_count,
                    stem_count=entry.stem_count,
                    option_count=entry.option_count,
                    total_count=entry.total_count,
                    score=entry.score,
                )
                corrected_map[corrected] = updated
                logger.info("AI corrected spelling: '%s' → '%s'", orig, corrected)

        # Build result in AI-specified order
        result: List[LemmaEntry] = []
        for word in ai_words[:top_n]:
            if word in corrected_map:
                result.append(corrected_map[word])
            elif word in lemma_map:
                result.append(lemma_map[word])
            # skip unknown words (AI hallucinations)

        logger.info(
            "AI preprocess: %d candidates → %d selected, %d corrections",
            len(candidates), len(result), len(corrections),
        )
        return result

    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("AI preprocess response parse error (%s), falling back: %s", exc, raw_response[:200])
        return sorted_lemmas[:top_n]


async def generate_vocabulary(
    lemma_table: List[LemmaEntry],
    context_text: str = "",
    top_n: int = 50,
    provider_name: str | None = None,
    ai_preprocess: bool | None = None,
) -> List[VocabEntry]:
    """
    Enrich the top-*top_n* lemmas with definitions and Chinese translations.

    Pipeline:
    1. Sort by learning priority
    2. [Optional] AI preprocess: fix OCR errors + re-rank by exam importance
    3. Enrich via the configured vocabulary provider

    Args:
        lemma_table:   Full lemma table from frequency analysis.
        context_text:  Raw exam text for example sentence generation.
        top_n:         How many words to enrich.
        provider_name: Override the default provider from config.
        ai_preprocess: Whether to run AI re-ranking step. None = use runtime config.

    Returns:
        List of VocabEntry sorted by learning priority descending.
    """
    runtime = get_runtime_config()
    pname = provider_name or runtime.vocab_provider

    # Determine whether to run AI preprocessing
    should_preprocess = ai_preprocess
    if should_preprocess is None:
        should_preprocess = getattr(runtime, "ai_preprocess_enabled", False)

    if should_preprocess:
        logger.info("Running AI vocabulary pre-processing (rerank + spell-fix)…")
        candidates = await ai_preprocess_lemmas(lemma_table, top_n=top_n, provider_name=pname)
    else:
        sorted_lemmas = sorted(lemma_table, key=_learning_priority, reverse=True)
        candidates = sorted_lemmas[:top_n]

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

    logger.info(f"Generating vocabulary for {len(candidates)} words via '{pname}'")

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
