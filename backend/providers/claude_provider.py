"""
AI vocabulary provider — uses the Anthropic Claude API.

Sends words in configurable batches to keep prompt sizes manageable.
Returns structured JSON parsed into VocabEntry objects.
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
    You are an expert English lexicographer and EFL teacher specializing in
    Chinese high-school (高中) and college-entrance exam (高考) preparation.

    Given a list of English words found in a Chinese exam paper, produce a
    bilingual (中英对照) vocabulary reference optimized for 高考 students.

    Return ONLY a valid JSON array — no prose, no markdown fences.
    Each element must conform exactly to this schema:
    {
      "headword": "<the word as given>",
      "pos": "<noun|verb|adj|adv|phrase|other>",
      "chinese_meaning": "<简洁中文释义，多义项用；分隔，如: n. 能力；才干>",
      "english_definition": "<one clear English definition suitable for exam context>",
      "example_sentence": "<one natural example sentence — prefer drawing from exam context if provided>",
      "notes": "<高考考点: common collocations, confusable words, fixed phrases, or key usage tips; empty string if none>",
      "word_level": "<基础|高考|超纲>"
    }

    word_level guide:
    - 基础: very common, students likely already know (go, big, happy, eat)
    - 高考: core exam vocabulary, prime study targets
    - 超纲: advanced/rare, unlikely to be tested

    For chinese_meaning: include part of speech abbreviation (n./v./adj./adv.) before each sense.
    For notes: highlight collocations, common exam phrases, or typical 完形/阅读 traps.
    Respond with exactly as many objects as words given.
""").strip()


class ClaudeProvider(BaseVocabProvider):
    name = "claude"

    def __init__(self):
        runtime = get_runtime_config()
        llm = runtime.llm
        try:
            import anthropic
            self._client = anthropic.AsyncAnthropic(
                api_key=llm.anthropic_api_key
            )
        except ImportError:
            raise RuntimeError("anthropic package not installed: pip install anthropic")
        except Exception as e:
            raise RuntimeError(f"Failed to create Anthropic client: {e}")
        self._model = runtime.ai_model
        self._batch_size = runtime.ai_batch_size

    async def enrich(
        self,
        entries: List[LemmaEntry],
        context_text: str = "",
    ) -> List[VocabEntry]:
        results: List[VocabEntry] = []
        batch_size = self._batch_size

        for i in range(0, len(entries), batch_size):
            batch = entries[i : i + batch_size]
            vocab_batch = await self._enrich_batch(batch, context_text)
            results.extend(vocab_batch)

        return results

    async def _enrich_batch(
        self,
        batch: List[LemmaEntry],
        context_text: str,
    ) -> List[VocabEntry]:
        words = [e.lemma for e in batch]
        context_snippet = context_text[:3000] if context_text else ""

        user_content = f"Words: {json.dumps(words, ensure_ascii=False)}"
        if context_snippet:
            user_content += f"\n\n试卷原文节选（用于例句参考）:\n{context_snippet}"

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            raw = response.content[0].text.strip()
            # Strip markdown fences if model wraps in ```json ... ```
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.rsplit("```", 1)[0].strip()
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"Claude returned invalid JSON: {e}")
            return [self._fallback(entry) for entry in batch]
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return [self._fallback(entry) for entry in batch]

        entry_map = {e.lemma: e for e in batch}
        vocab_entries: List[VocabEntry] = []

        for item in data:
            hw = item.get("headword", "")
            base_entry = entry_map.get(hw, None)

            vocab_entries.append(
                VocabEntry(
                    headword=hw,
                    lemma=hw,
                    family=base_entry.family_id if base_entry else None,
                    pos=item.get("pos", ""),
                    chinese_meaning=item.get("chinese_meaning", ""),
                    english_definition=item.get("english_definition", ""),
                    example_sentence=item.get("example_sentence", ""),
                    notes=item.get("notes", ""),
                    body_count=base_entry.body_count if base_entry else 0,
                    stem_count=base_entry.stem_count if base_entry else 0,
                    option_count=base_entry.option_count if base_entry else 0,
                    total_count=base_entry.total_count if base_entry else 0,
                    score=base_entry.score if base_entry else 0.0,
                    source=self.name,
                    word_level=item.get("word_level"),
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
            source="claude_error",
        )
