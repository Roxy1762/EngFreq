"""
Free Dictionary API provider — https://dictionaryapi.dev/
No API key required.  Covers most common English words.

Rate-limited: one request per word (no batch endpoint), so we use
asyncio gather with a semaphore to avoid hammering the server.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import httpx

from backend.models.schemas import LemmaEntry, VocabEntry
from backend.providers.base_provider import BaseVocabProvider

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/{word}"
_CONCURRENCY = 5   # max simultaneous requests


class FreeDictProvider(BaseVocabProvider):
    name = "free_dict"

    async def enrich(
        self,
        entries: List[LemmaEntry],
        context_text: str = "",
    ) -> List[VocabEntry]:
        if not entries:
            return []
        sem = asyncio.Semaphore(_CONCURRENCY)
        async with httpx.AsyncClient(timeout=10.0) as client:
            tasks = [self._fetch_one(client, sem, e) for e in entries]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        vocab: List[VocabEntry] = []
        for entry, result in zip(entries, results):
            if isinstance(result, VocabEntry):
                vocab.append(result)
            else:
                logger.warning(f"free_dict failed for '{entry.lemma}': {result}")
                vocab.append(self._stub(entry, reason="free_dict_error"))

        # If literally every entry came back as a "miss"/error stub the
        # network is almost certainly down (or DNS, or the API is rate-
        # limiting us). Raise so the vocabulary fallback chain promotes the
        # next provider instead of silently delivering an empty word list.
        real_hits = sum(1 for v in vocab if not v.source.startswith("free_dict_"))
        if entries and real_hits == 0:
            raise RuntimeError(
                "free_dict returned no real definitions for any of "
                f"{len(entries)} words — treating as provider failure"
            )
        return vocab

    async def _fetch_one(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        entry: LemmaEntry,
    ) -> VocabEntry:
        async with sem:
            url = _BASE_URL.format(word=entry.lemma)
            try:
                resp = await client.get(url)
                if resp.status_code == 404:
                    return self._stub(entry, reason="free_dict_miss")
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.debug(f"HTTP error for '{entry.lemma}': {e}")
                return self._stub(entry, reason="free_dict_error")

        return self._parse(entry, data)

    def _parse(self, entry: LemmaEntry, data: list) -> VocabEntry:
        """Extract the most relevant definition and example."""
        pos = ""
        en_def = ""
        example = ""

        try:
            first = data[0]
            meanings = first.get("meanings", [])
            if meanings:
                m = meanings[0]
                pos = m.get("partOfSpeech", "")
                defs = m.get("definitions", [])
                if defs:
                    en_def = defs[0].get("definition", "")
                    example = defs[0].get("example", "")
        except (IndexError, KeyError, TypeError):
            pass

        return VocabEntry(
            headword=entry.lemma,
            lemma=entry.lemma,
            family=entry.family_id,
            pos=pos,
            english_definition=en_def,
            example_sentence=example,
            body_count=entry.body_count,
            stem_count=entry.stem_count,
            option_count=entry.option_count,
            total_count=entry.total_count,
            score=entry.score,
            source=self.name,
        )

    @staticmethod
    def _stub(entry: LemmaEntry, reason: str = "free_dict_miss") -> VocabEntry:
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
            source=reason,
        )
