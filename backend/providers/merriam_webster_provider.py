"""
Merriam-Webster Collegiate Dictionary API provider.
Requires a free API key from https://dictionaryapi.com/
Set MERRIAM_WEBSTER_KEY in .env
"""
from __future__ import annotations

import asyncio
import logging
from typing import List

import httpx

from backend.config import settings
from backend.models.schemas import LemmaEntry, VocabEntry
from backend.providers.base_provider import BaseVocabProvider

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.dictionaryapi.com/api/v3/references/collegiate/json/{word}"
_CONCURRENCY = 5


def _get_mw_key() -> str:
    try:
        from backend.services.runtime_config import get_runtime_config
        key = get_runtime_config().dict_providers.merriam_webster_key
        if key:
            return key
    except Exception:
        pass
    return settings.merriam_webster_key or ""


class MerriamWebsterProvider(BaseVocabProvider):
    name = "merriam_webster"

    def __init__(self):
        if not _get_mw_key():
            raise RuntimeError(
                "Merriam-Webster API key not configured. "
                "Set it in the admin panel under '词典工具密钥' or via MERRIAM_WEBSTER_KEY in .env"
            )

    async def enrich(
        self,
        entries: List[LemmaEntry],
        context_text: str = "",
    ) -> List[VocabEntry]:
        sem = asyncio.Semaphore(_CONCURRENCY)
        async with httpx.AsyncClient(timeout=10.0) as client:
            tasks = [self._fetch_one(client, sem, e) for e in entries]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        vocab: List[VocabEntry] = []
        for entry, result in zip(entries, results):
            if isinstance(result, VocabEntry):
                vocab.append(result)
            else:
                logger.warning(f"MW failed for '{entry.lemma}': {result}")
                vocab.append(self._stub(entry))
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
                resp = await client.get(
                    url,
                    params={"key": _get_mw_key()},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.debug(f"MW HTTP error for '{entry.lemma}': {e}")
                return self._stub(entry)

        return self._parse(entry, data)

    def _parse(self, entry: LemmaEntry, data: list) -> VocabEntry:
        pos = ""
        en_def = ""
        example = ""

        try:
            if not data or isinstance(data[0], str):
                # MW returns suggestions as strings when not found
                return self._stub(entry)

            first = data[0]
            pos = first.get("fl", "")   # functional label e.g. "noun"

            # shortdef is the easiest array to parse
            shortdefs = first.get("shortdef", [])
            if shortdefs:
                en_def = shortdefs[0]

            # Try to pull a usage example from def block
            def_sections = first.get("def", [])
            for section in def_sections:
                for sseq in section.get("sseq", []):
                    for sense in sseq:
                        if not isinstance(sense, list) or len(sense) < 2:
                            continue
                        dt = sense[1].get("dt", [])
                        for dt_item in dt:
                            if isinstance(dt_item, list) and dt_item[0] == "vis":
                                for vis in dt_item[1]:
                                    t = vis.get("t", "")
                                    if t:
                                        # Strip MW markup like {it}...{/it}
                                        import re
                                        t = re.sub(r"\{[^}]+\}", "", t)
                                        example = t.strip()
                                        break
                            if example:
                                break
                        if example:
                            break
                    if example:
                        break
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
    def _stub(entry: LemmaEntry) -> VocabEntry:
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
            source="mw_miss",
        )
