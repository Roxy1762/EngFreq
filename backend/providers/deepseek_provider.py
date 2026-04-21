"""
DeepSeek vocabulary provider.

Uses the DeepSeek API (OpenAI-compatible) via the unified `llm_client.chat()`.
"""
from __future__ import annotations

import json
import logging
from typing import List

from backend.models.schemas import LemmaEntry, VocabEntry
from backend.prompts import get_prompt
from backend.providers.base_provider import BaseVocabProvider
from backend.providers.claude_provider import _build_entries, _fallback_entry
from backend.services.runtime_config import get_runtime_config
from backend.utils.json_parse import parse_json_array
from backend.utils.llm_client import chat
from backend.utils.model_registry import recommended_batch_size

logger = logging.getLogger(__name__)


class DeepSeekProvider(BaseVocabProvider):
    name = "deepseek"

    def __init__(self, *, domain: str = "gaokao", prompt_version: str = "v2"):
        runtime = get_runtime_config()
        if not runtime.llm.deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY not configured")
        self._model = runtime.llm.deepseek_model
        self._batch_size = recommended_batch_size(self._model, runtime.ai_batch_size)
        self._system_prompt = get_prompt("vocab_enrich", domain=domain, version=prompt_version)

    async def enrich(self, entries: List[LemmaEntry], context_text: str = "") -> List[VocabEntry]:
        results: List[VocabEntry] = []
        for i in range(0, len(entries), self._batch_size):
            batch = entries[i : i + self._batch_size]
            results.extend(await self._enrich_batch(batch, context_text))
        return results

    async def _enrich_batch(self, batch: List[LemmaEntry], context_text: str) -> List[VocabEntry]:
        words = [e.lemma for e in batch]
        context_snippet = context_text[:3000] if context_text else ""

        user_content = (
            f"Words to enrich ({len(words)} total):\n"
            f"{json.dumps(words, ensure_ascii=False, indent=2)}"
        )
        if context_snippet:
            user_content += f"\n\n--- 试卷原文节选 ---\n{context_snippet}\n---"

        try:
            response = await chat(
                provider="deepseek",
                model=self._model,
                system=self._system_prompt,
                user=user_content,
                max_tokens=4096,
                temperature=0.3,
                label=f"deepseek-vocab:{len(words)}w",
            )
        except Exception as exc:   # noqa: BLE001
            logger.error("DeepSeek enrichment batch failed: %s", exc)
            return [_fallback_entry(e, source="deepseek_error") for e in batch]

        data = parse_json_array(response.text)
        if not data:
            logger.error("DeepSeek returned unparseable output (len=%d)", len(response.text))
            return [_fallback_entry(e, source="deepseek_error") for e in batch]

        return _build_entries(data, batch, source=self.name)
