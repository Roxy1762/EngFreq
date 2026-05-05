"""
Shared base class for HTTP dictionary providers (free_dict, MW, iciba, …).

Pulls out boilerplate that previously lived in each provider:
  * single shared httpx.AsyncClient with sane timeout + keep-alive
  * bounded concurrency via asyncio.Semaphore
  * automatic dict-cache hit/miss handling (`backend.services.dict_cache`)
  * graceful per-word fallback to a stub when the API misses

Subclasses implement just two things:
  * `name`        — short identifier used as the cache key + VocabEntry.source
  * `_lookup_one` — async fn(client, word) → Optional[CachedDefinition]
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import httpx

from backend.models.schemas import LemmaEntry, VocabEntry
from backend.providers.base_provider import BaseVocabProvider
from backend.services import dict_cache

logger = logging.getLogger(__name__)


class HttpDictProviderBase(BaseVocabProvider):
    """Abstract HTTP-backed dictionary provider with built-in caching."""

    # Default tuning — subclasses may override.
    concurrency: int = 5
    timeout_seconds: float = 10.0
    use_cache: bool = True
    cache_ttl_seconds: int = 60 * 60 * 24 * 30  # 30 days

    async def enrich(
        self,
        entries: List[LemmaEntry],
        context_text: str = "",
    ) -> List[VocabEntry]:
        if not entries:
            return []

        sem = asyncio.Semaphore(self.concurrency)
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            tasks = [self._lookup_with_cache(client, sem, e) for e in entries]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        vocab: List[VocabEntry] = []
        real_hits = 0
        for entry, result in zip(entries, results):
            if isinstance(result, VocabEntry):
                vocab.append(result)
                if result.source == self.name:
                    real_hits += 1
            else:
                logger.warning("%s failed for '%s': %s", self.name, entry.lemma, result)
                vocab.append(self._stub(entry, reason=f"{self.name}_error"))

        # If the network is fully down or the API rate-limited every word,
        # bubble up so the fallback chain promotes the next provider.
        if entries and real_hits == 0:
            raise RuntimeError(
                f"{self.name} returned no real definitions for any of "
                f"{len(entries)} words — treating as provider failure"
            )
        return vocab

    async def _lookup_with_cache(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        entry: LemmaEntry,
    ) -> VocabEntry:
        if self.use_cache:
            cached = dict_cache.get(self.name, entry.lemma, ttl_seconds=self.cache_ttl_seconds)
            if cached is not None:
                return self._merge(entry, cached)

        async with sem:
            try:
                cached = await self._lookup_one(client, entry.lemma)
            except Exception as exc:   # noqa: BLE001
                logger.debug("%s lookup error for '%s': %s", self.name, entry.lemma, exc)
                return self._stub(entry, reason=f"{self.name}_error")

        if cached is None:
            return self._stub(entry, reason=f"{self.name}_miss")

        if self.use_cache:
            dict_cache.put(self.name, entry.lemma, cached)
        return self._merge(entry, cached)

    # ── Subclass extension point ────────────────────────────────────────────
    async def _lookup_one(
        self,
        client: httpx.AsyncClient,
        word: str,
    ) -> Optional[dict_cache.CachedDefinition]:
        raise NotImplementedError

    # ── Helpers shared by all subclasses ────────────────────────────────────
    def _merge(self, entry: LemmaEntry, cached: dict_cache.CachedDefinition) -> VocabEntry:
        return VocabEntry(
            headword=cached.headword or entry.lemma,
            lemma=entry.lemma,
            family=entry.family_id,
            pos=cached.pos or entry.pos,
            chinese_meaning=cached.chinese_meaning or None,
            english_definition=cached.english_definition or None,
            example_sentence=cached.example_sentence or None,
            collocations=cached.collocations or None,
            confusables=cached.confusables or None,
            notes=cached.notes or None,
            body_count=entry.body_count,
            stem_count=entry.stem_count,
            option_count=entry.option_count,
            total_count=entry.total_count,
            score=entry.score,
            source=self.name,
        )

    def _stub(self, entry: LemmaEntry, *, reason: str) -> VocabEntry:
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
