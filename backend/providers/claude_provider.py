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
    You are an expert bilingual lexicographer and senior EFL teacher with 20+ years of
    experience preparing Chinese students for the 高考 (National College Entrance Exam).

    ## Task
    Given a list of English words extracted from a Chinese exam paper, produce a
    high-quality bilingual (中英对照) vocabulary reference sheet optimized for 高考 students.

    ## Output Format
    Return ONLY a valid JSON array — no prose, no markdown code fences, no extra text.
    Each element MUST conform exactly to this schema:
    {
      "headword": "<canonical spelling of the word — fix obvious OCR errors if any>",
      "pos": "<noun|verb|adj|adv|prep|conj|phrase|other>",
      "chinese_meaning": "<精准中文释义，格式示例: n. 能力；才干 v. 使能够>",
      "english_definition": "<one concise, exam-appropriate English definition>",
      "example_sentence": "<one natural example sentence using the word correctly>",
      "notes": "<高考考点备注 — see guidelines below; empty string if nothing important>",
      "word_level": "<基础|高考|超纲>"
    }

    ## Field Guidelines

    ### headword
    - Use the canonical dictionary form (lemma)
    - If the input word looks like an OCR error (e.g. "bccause", "cornplete"), correct it silently
    - Keep the word as a single headword (no inflections)

    ### chinese_meaning
    - Lead each sense with abbreviated POS: n.／v.／adj.／adv.／prep.／conj.
    - Separate senses with "；" (Chinese semicolon)
    - For verbs, note key complement/object patterns where relevant
    - Example: "v. 承认；坦白；接纳（成员）"

    ### english_definition
    - One clear definition targeted at upper-secondary level
    - Prefer active phrasing: "to make something happen" over "the act of making something happen"
    - Draw on the exam context snippet (if provided) to choose the most relevant sense

    ### example_sentence
    - Prefer sentences from the exam context if provided — adapt if needed for clarity
    - Otherwise write a natural sentence at B2 level that shows the word in typical collocations
    - Do NOT use the word as its own example

    ### notes (高考考点备注)
    - Collocations & fixed phrases: e.g. "make an effort to do; spare no effort"
    - Common confusables: e.g. "affect (v.) vs effect (n.)"
    - High-frequency exam patterns: typical 完形填空 / 阅读理解 usage
    - Derivational family if exam-relevant: e.g. "→ ability, able, unable, ably"
    - Leave empty string "" if nothing noteworthy

    ### word_level
    - 基础: Extremely common; students know it (the, go, big, year, make, need)
    - 高考: Core exam vocabulary — primary study target (persist, assert, elaborate, diverse)
    - 超纲: Advanced or rare; unlikely to appear on 高考 but worth knowing if it appears in this text

    ## Critical Rules
    1. Return EXACTLY as many JSON objects as words given — one per word, in the same order
    2. Never skip a word or merge two words into one object
    3. Fix OCR spelling errors in headword silently (update headword field to correct spelling)
    4. Respond with valid JSON only — no markdown, no prose before or after the array
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
        # Use a larger context window for better example sentences
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
