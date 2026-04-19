"""
Generic OpenAI-compatible vocabulary provider.

Works with any API that uses the OpenAI chat completions format:
  - OpenAI (GPT-4o, GPT-4, etc.)
  - Ollama (local models)
  - LM Studio
  - Azure OpenAI
  - Groq
  - Together AI
  - Any other OpenAI-compatible endpoint

Configure via environment variables:
  OPENAI_API_KEY      — API key (use "ollama" for local Ollama)
  OPENAI_BASE_URL     — Base URL (default: https://api.openai.com/v1)
  OPENAI_MODEL        — Model name (default: gpt-4o-mini)
"""
from __future__ import annotations

import json
import logging
import textwrap
from typing import List

from backend.config import settings
from backend.models.schemas import LemmaEntry, VocabEntry
from backend.providers.base_provider import BaseVocabProvider
from backend.services.runtime_config import get_runtime_config

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = textwrap.dedent("""
    You are an expert English lexicographer and EFL teacher.
    Given a list of English words found in a Chinese high-school exam paper,
    produce a vocabulary reference for Chinese students.

    Return ONLY a valid JSON array — no prose, no markdown fences.
    Each element must conform exactly to this schema:
    {
      "headword": "<the word as given>",
      "pos": "<noun|verb|adj|adv|phrase|other>",
      "chinese_meaning": "<concise Chinese translation, separate senses with ；>",
      "english_definition": "<one-sentence English definition>",
      "example_sentence": "<one natural example sentence — prefer exam context if provided>",
      "notes": "<optional: common collocations, confusable words, or usage tips — empty string if none>"
    }
    Respond with exactly as many objects as words given.
""").strip()


class OpenAICompatibleProvider(BaseVocabProvider):
    name = "openai"

    def __init__(self):
        runtime = get_runtime_config()
        llm = runtime.llm
        try:
            from openai import AsyncOpenAI
        except ImportError:
            raise RuntimeError("openai package not installed: pip install openai")

        api_key = llm.openai_api_key
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not configured")

        kwargs: dict = {"api_key": api_key}
        if llm.openai_base_url:
            kwargs["base_url"] = llm.openai_base_url

        self._client = AsyncOpenAI(**kwargs)
        self._model = llm.openai_model
        self._batch_size = runtime.ai_batch_size

    async def enrich(self, entries: List[LemmaEntry], context_text: str = "") -> List[VocabEntry]:
        results: List[VocabEntry] = []
        batch_size = self._batch_size

        for i in range(0, len(entries), batch_size):
            batch = entries[i : i + batch_size]
            results.extend(await self._enrich_batch(batch, context_text))

        return results

    async def _enrich_batch(self, batch: List[LemmaEntry], context_text: str) -> List[VocabEntry]:
        words = [e.lemma for e in batch]
        context_snippet = context_text[:3000] if context_text else ""

        user_content = f"Words: {json.dumps(words, ensure_ascii=False)}"
        if context_snippet:
            user_content += f"\n\nExam context:\n{context_snippet}"

        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=4096,
                temperature=0.3,
            )
            raw = response.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error("OpenAI-compatible provider returned invalid JSON: %s", e)
            return [self._fallback(entry) for entry in batch]
        except Exception as e:
            logger.error("OpenAI-compatible API error: %s", e)
            return [self._fallback(entry) for entry in batch]

        entry_map = {e.lemma: e for e in batch}
        vocab_entries: List[VocabEntry] = []

        for item in data:
            hw = item.get("headword", "")
            base = entry_map.get(hw)
            vocab_entries.append(
                VocabEntry(
                    headword=hw,
                    lemma=hw,
                    family=base.family_id if base else None,
                    pos=item.get("pos", ""),
                    chinese_meaning=item.get("chinese_meaning", ""),
                    english_definition=item.get("english_definition", ""),
                    example_sentence=item.get("example_sentence", ""),
                    notes=item.get("notes", ""),
                    body_count=base.body_count if base else 0,
                    stem_count=base.stem_count if base else 0,
                    option_count=base.option_count if base else 0,
                    total_count=base.total_count if base else 0,
                    score=base.score if base else 0.0,
                    source=self.name,
                )
            )

        return vocab_entries

    @staticmethod
    def _fallback(entry: LemmaEntry) -> VocabEntry:
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
            source="openai_error",
        )
