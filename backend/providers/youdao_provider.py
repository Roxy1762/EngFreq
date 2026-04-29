"""
有道词典 (Youdao Dictionary) vocabulary provider.

Uses the Youdao Text Translation API to fetch definitions.
Requires: YOUDAO_APP_KEY and YOUDAO_APP_SECRET in environment.

Sign up at: https://ai.youdao.com/
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from typing import List, Optional

from backend.config import settings
from backend.models.schemas import LemmaEntry, VocabEntry
from backend.providers.base_provider import BaseVocabProvider

logger = logging.getLogger(__name__)

_YOUDAO_API_URL = "https://openapi.youdao.com/api"
_CONCURRENCY = 4   # Youdao tolerates a small concurrent burst per app key

_POS_MAP = {
    "n.": "noun", "v.": "verb", "adj.": "adj", "adv.": "adv",
    "prep.": "other", "conj.": "other", "pron.": "other",
    "vt.": "verb", "vi.": "verb", "num.": "other",
}


def _get_youdao_creds() -> tuple[str, str]:
    try:
        from backend.services.runtime_config import get_runtime_config
        dp = get_runtime_config().dict_providers
        key = dp.youdao_app_key or settings.youdao_app_key or ""
        secret = dp.youdao_app_secret or settings.youdao_app_secret or ""
        return key, secret
    except Exception:
        return settings.youdao_app_key or "", settings.youdao_app_secret or ""


class YoudaoProvider(BaseVocabProvider):
    name = "youdao"

    def __init__(self):
        app_key, app_secret = _get_youdao_creds()
        self._app_key = app_key
        self._app_secret = app_secret
        if not self._app_key or not self._app_secret:
            raise RuntimeError(
                "Youdao credentials not configured. "
                "Set them in the admin panel under '词典工具密钥' or via YOUDAO_APP_KEY/YOUDAO_APP_SECRET in .env"
            )

        try:
            import httpx
            self._httpx = httpx
        except ImportError:
            raise RuntimeError("httpx not installed: pip install httpx")

    def _sign(self, word: str, salt: str, curtime: str) -> str:
        input_str = word if len(word) <= 20 else word[:10] + str(len(word)) + word[-10:]
        sign_str = self._app_key + input_str + salt + curtime + self._app_secret
        return hashlib.sha256(sign_str.encode("utf-8")).hexdigest()

    async def _lookup(self, client, word: str) -> Optional[dict]:
        salt = str(uuid.uuid4())
        curtime = str(int(time.time()))
        sign = self._sign(word, salt, curtime)

        params = {
            "q": word,
            "from": "en",
            "to": "zh-CHS",
            "appKey": self._app_key,
            "salt": salt,
            "sign": sign,
            "signType": "v3",
            "curtime": curtime,
        }

        try:
            resp = await client.post(_YOUDAO_API_URL, data=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Youdao API error for '%s': %s", word, exc)
            return None

        error_code = data.get("errorCode", "0")
        if error_code != "0":
            logger.warning("Youdao error code %s for word '%s'", error_code, word)
            return None

        return data

    def _parse_response(self, word: str, data: dict, base: LemmaEntry) -> VocabEntry:
        # Basic translation
        translation = ""
        trans_list = data.get("translation", [])
        if trans_list:
            translation = "；".join(trans_list)

        # Web explanation (richer)
        web_def = ""
        for web in data.get("web", []):
            if web.get("key", "").lower() == word.lower():
                web_def = "；".join(web.get("value", []))
                break

        # Dictionary entries (most detailed)
        ec = data.get("basic", {})
        explains = ec.get("explains", [])
        pos = ""
        meanings = []
        english_def = ""

        for exp in explains[:3]:
            exp = exp.strip()
            # Try to extract POS prefix like "n. " or "v. "
            for pos_key, pos_val in _POS_MAP.items():
                if exp.startswith(pos_key):
                    if not pos:
                        pos = pos_val
                    meanings.append(exp[len(pos_key):].strip())
                    break
            else:
                meanings.append(exp)

        if not pos:
            pos = base.pos.lower() if base.pos else "other"

        chinese_meaning = "；".join(meanings) if meanings else (translation or web_def)
        if not english_def and explains:
            english_def = " / ".join(explains[:2])

        # Phonetic
        phonetic = ec.get("phonetic", "") or ec.get("us-phonetic", "")
        notes = f"/{phonetic}/" if phonetic else ""

        return VocabEntry(
            headword=word,
            lemma=base.lemma,
            family=base.family_id,
            pos=pos,
            chinese_meaning=chinese_meaning or "—",
            english_definition=english_def or f"See: {word}",
            example_sentence="",
            notes=notes,
            body_count=base.body_count,
            stem_count=base.stem_count,
            option_count=base.option_count,
            total_count=base.total_count,
            score=base.score,
            source=self.name,
        )

    async def enrich(self, entries: List[LemmaEntry], context_text: str = "") -> List[VocabEntry]:
        if not entries:
            return []

        sem = asyncio.Semaphore(_CONCURRENCY)

        async def _one(client, entry: LemmaEntry) -> VocabEntry:
            async with sem:
                data = await self._lookup(client, entry.lemma)
            if data:
                return self._parse_response(entry.lemma, data, entry)
            return VocabEntry(
                headword=entry.lemma,
                lemma=entry.lemma,
                family=entry.family_id,
                pos=entry.pos,
                chinese_meaning="",
                english_definition="",
                body_count=entry.body_count,
                stem_count=entry.stem_count,
                option_count=entry.option_count,
                total_count=entry.total_count,
                score=entry.score,
                source="youdao_error",
            )

        # Reuse a single AsyncClient so we get HTTP/1.1 keep-alive and TLS
        # session caching across requests instead of paying handshake cost
        # per word. Worst-case wall time drops from N×8s to roughly
        # ceil(N/_CONCURRENCY) × 8s.
        async with self._httpx.AsyncClient(timeout=8.0) as client:
            return list(await asyncio.gather(*(_one(client, e) for e in entries)))
