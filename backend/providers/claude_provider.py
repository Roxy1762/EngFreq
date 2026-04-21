"""
Anthropic Claude vocabulary provider.

Uses the unified `llm_client.chat()` so this provider gets exponential-backoff
retry, prompt caching (for the long system prompt), and consistent observability
for free — without re-implementing the SDK boilerplate here.

The 82-line system prompt lives in `backend.prompts.vocab` and is versioned.
"""
from __future__ import annotations

import logging
from typing import List

from backend.models.schemas import LemmaEntry, VocabEntry
from backend.prompts import get_prompt
from backend.providers.base_provider import BaseVocabProvider
from backend.services.runtime_config import get_runtime_config
from backend.utils.json_parse import parse_json_array
from backend.utils.llm_client import chat
from backend.utils.model_registry import recommended_batch_size

logger = logging.getLogger(__name__)


class ClaudeProvider(BaseVocabProvider):
    name = "claude"

    def __init__(self, *, domain: str = "gaokao", prompt_version: str = "v2"):
        runtime = get_runtime_config()
        if not runtime.llm.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not configured")
        self._model = runtime.ai_model
        self._batch_size = recommended_batch_size(self._model, runtime.ai_batch_size)
        self._domain = domain
        self._prompt_version = prompt_version
        self._system_prompt = get_prompt("vocab_enrich", domain=domain, version=prompt_version)

    async def enrich(
        self,
        entries: List[LemmaEntry],
        context_text: str = "",
    ) -> List[VocabEntry]:
        results: List[VocabEntry] = []
        for i in range(0, len(entries), self._batch_size):
            batch = entries[i : i + self._batch_size]
            results.extend(await self._enrich_batch(batch, context_text))
        return results

    async def _enrich_batch(
        self,
        batch: List[LemmaEntry],
        context_text: str,
    ) -> List[VocabEntry]:
        import json
        words = [e.lemma for e in batch]
        context_snippet = context_text[:4000] if context_text else ""

        user_content = (
            f"Words to enrich ({len(words)} total):\n"
            f"{json.dumps(words, ensure_ascii=False, indent=2)}"
        )
        if context_snippet:
            user_content += (
                f"\n\n--- 试卷原文节选（用于例句参考与词义消歧）---\n{context_snippet}\n---"
            )

        try:
            response = await chat(
                provider="claude",
                model=self._model,
                system=self._system_prompt,
                user=user_content,
                max_tokens=4096,
                temperature=0.3,
                use_prompt_cache=True,          # big system prompt → cache hit after first call
                label=f"claude-vocab:{len(words)}w",
            )
        except Exception as exc:   # noqa: BLE001
            logger.error("Claude enrichment batch failed: %s", exc)
            return [self._fallback(entry) for entry in batch]

        data = parse_json_array(response.text)
        if not data:
            logger.error("Claude returned unparseable output (len=%d)", len(response.text))
            return [self._fallback(entry) for entry in batch]

        return _build_entries(data, batch, source=self.name)

    @staticmethod
    def _fallback(entry: LemmaEntry) -> VocabEntry:
        return _fallback_entry(entry, source="claude_error")


# ── Shared entry-builder used by all LLM providers ────────────────────────────

def _build_entries(
    data: list,
    batch: List[LemmaEntry],
    *,
    source: str,
) -> List[VocabEntry]:
    """Map the model's JSON objects back onto the input LemmaEntries."""
    entry_map = {e.lemma: e for e in batch}
    out: List[VocabEntry] = []

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        hw = (item.get("headword") or "").strip()
        if not hw:
            continue
        # positional fallback: if headword doesn't match any input lemma, use the
        # N-th input entry for count/score metadata (keeps order alignment)
        base = entry_map.get(hw) or (batch[idx] if idx < len(batch) else None)

        out.append(VocabEntry(
            headword=hw,
            lemma=hw,
            family=base.family_id if base else None,
            pos=item.get("pos", ""),
            chinese_meaning=item.get("chinese_meaning", ""),
            english_definition=item.get("english_definition", ""),
            example_sentence=item.get("example_sentence", ""),
            collocations=item.get("collocations") or None,
            confusables=item.get("confusables") or None,
            notes=item.get("notes", ""),
            body_count=base.body_count if base else 0,
            stem_count=base.stem_count if base else 0,
            option_count=base.option_count if base else 0,
            total_count=base.total_count if base else 0,
            score=base.score if base else 0.0,
            source=source,
            word_level=item.get("word_level"),
            cefr_level=(item.get("cefr_level") or None),
        ))

    return out


def _fallback_entry(entry: LemmaEntry, *, source: str) -> VocabEntry:
    return VocabEntry(
        headword=entry.lemma,
        lemma=entry.lemma,
        family=entry.family_id,
        pos=entry.pos,
        body_count=entry.body_count,
        stem_count=entry.stem_count,
        option_count=entry.option_count,
        total_count=entry.total_count,
        score=entry.score,
        source=source,
    )
