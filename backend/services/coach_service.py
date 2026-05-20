"""
AI Vocabulary Coach service.

Stateful, multi-turn chat between a student and the LLM, persisted per
user. Threads can be focused on specific words (the LLM auto-receives the
saved definitions, recent quiz performance, and CEFR/Zipf metadata) so
replies are anchored on real exam data rather than generic textbook
answers.

Design choices:

* The persisted ``CoachThread`` rows store only **messages**, never any
  ephemeral system prompt. The system prompt is rebuilt per request from
  the latest library/quiz state so users always get current context.
* Token usage is recorded on every assistant reply. The thread row
  carries running totals for cheap "how much budget did this thread eat"
  queries.
* Context window: the most recent 12 messages are sent to the LLM. This
  is a good balance between continuity and prompt-cache friendliness — the
  long, mostly-static system prompt is the cacheable part.
* The provider chain falls back to free_dict only if the LLM is missing
  credentials, since coaching needs natural-language output a dictionary
  can't produce. We surface a clear 400 in that case.

Public functions are all sync (they accept a Session). The LLM call is
async, so endpoint handlers in main.py wrap the persistence steps in
:func:`backend.utils.db_session.run_in_session` around the awaited LLM
call.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from backend.database import (
    CoachMessage,
    CoachThread,
    LibraryWord,
    ReviewEvent,
    ReviewItem,
    User,
)
from backend.utils.llm_client import chat, is_llm_provider, resolve_active_llm

logger = logging.getLogger(__name__)


# Hard cap on how many messages are replayed to the LLM per turn. Longer
# threads can still hold thousands of messages — we just don't pay for
# all of them on every reply.
HISTORY_WINDOW = 12

# Threads beyond this age are auto-archived from the active list.
ARCHIVE_AFTER_DAYS = 60

# Anchor the system prompt at the top so prompt caching (Anthropic) reuses
# the long, mostly-static block across turns.
_SYSTEM_PROMPT = """
You are 词汇导师 — a patient, expert English vocabulary coach for Chinese
high-school students preparing for the 高考 (gaokao) exam.

Your job is to help the student understand, remember, and use words from
their personal vocabulary library. The student typically wants:

  - the exact 中文 meaning + part of speech
  - a clear English definition in simple language
  - a memorable example sentence in *their* level (CEFR A2–B2 by default)
  - common collocations (动词搭配 / 名词搭配 / 介词搭配)
  - usage warnings (confusable words, formality, register)
  - mnemonic / etymology hints when they help retention

Hard rules:

  - Answer in 中文 by default. Switch to English only if the student writes
    in English first, or asks explicitly.
  - When you give an example sentence, KEEP IT SHORT (8–14 words). End with
    a punctuation mark. Put the target word in **bold** with **asterisks**.
  - When the student asks about a specific word, lean on the metadata in
    the system context (CEFR level, Zipf score, current Leitner box, recent
    quiz outcome). Adapt your depth to the student's track record.
  - When the student gets a word wrong frequently, lead with a memory hook
    BEFORE the dictionary definition. They've already seen the definition;
    they need a different angle.
  - Never invent definitions. If you genuinely don't know a word, say so
    plainly: "我不太确定，可以问老师确认。"
  - Keep replies SHORT. 200 Chinese characters or fewer for most turns. Use
    line breaks for visual scanning, never code fences unless the student
    asks for code.
""".strip()


# ── Context assembly ─────────────────────────────────────────────────────────

def _focus_word_context(db: Session, *, user_id: int, focus_words: List[str]) -> Dict[str, Any]:
    """Build a snapshot of the user's saved entries for `focus_words`.

    Returned shape is JSON-friendly; we serialise it into the system prompt
    so the LLM can answer questions about the user's notebook accurately.
    """
    if not focus_words:
        return {}
    targets = {w.strip().lower() for w in focus_words if w and w.strip()}
    if not targets:
        return {}

    rows = (
        db.query(LibraryWord)
        .filter(LibraryWord.user_id == user_id, func.lower(LibraryWord.headword).in_(targets))
        .all()
    )
    snapshot: Dict[str, Any] = {}
    for row in rows:
        snapshot[(row.headword or "").lower()] = {
            "headword": row.headword,
            "lemma": row.lemma,
            "pos": row.pos,
            "chinese_meaning": row.chinese_meaning,
            "english_definition": row.english_definition,
            "example_sentence": row.example_sentence,
            "tags": [t for t in (row.tags or "").split(",") if t],
            "word_level": row.word_level,
            "cefr_level": row.cefr_level,
            "zipf_score": row.zipf_score,
            "notes": row.notes,
            "mastered": bool(row.mastered),
        }
    return snapshot


def _recent_review_signal(db: Session, *, user_id: int, headwords: List[str]) -> Dict[str, Any]:
    """Last-7-days quiz/review accuracy for the focus words.

    Adds a small breadcrumb the LLM can use to calibrate ("you got this
    wrong twice this week — let's try a different angle").
    """
    if not headwords:
        return {}
    targets = {w.strip().lower() for w in headwords if w and w.strip()}
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    rows = (
        db.query(ReviewEvent.headword, ReviewEvent.quality)
        .filter(
            ReviewEvent.user_id == user_id,
            ReviewEvent.created_at >= cutoff,
            func.lower(ReviewEvent.headword).in_(targets),
        )
        .all()
    )
    by_word: Dict[str, Dict[str, int]] = {}
    for hw, quality in rows:
        slot = by_word.setdefault((hw or "").lower(), {"remembered": 0, "fuzzy": 0, "forgot": 0})
        if quality in slot:
            slot[quality] += 1
    return by_word


def _library_summary(db: Session, *, user_id: int) -> Dict[str, Any]:
    """Cheap aggregate so the coach knows the user's overall study posture."""
    total, mastered = db.query(
        func.count(LibraryWord.id),
        func.coalesce(func.sum(case((LibraryWord.mastered.is_(True), 1), else_=0)), 0),
    ).filter(LibraryWord.user_id == user_id).one()
    due = db.query(func.count(ReviewItem.id)).filter(
        ReviewItem.user_id == user_id,
        ReviewItem.due_at <= datetime.now(timezone.utc).replace(tzinfo=None),
    ).scalar() or 0
    return {
        "library_total": int(total or 0),
        "library_mastered": int(mastered or 0),
        "review_due": int(due),
    }


def _build_system_prompt(
    db: Session, *, user: User, focus_words: List[str], include_library_context: bool,
) -> str:
    """Compose the per-turn system prompt: static rules + dynamic snapshot."""
    base = _SYSTEM_PROMPT
    if not include_library_context:
        return base

    user_block = {
        "username": user.username,
        "display_name": user.display_name or user.username,
    }
    summary = _library_summary(db, user_id=user.id)
    focus_ctx = _focus_word_context(db, user_id=user.id, focus_words=focus_words)
    review_ctx = _recent_review_signal(db, user_id=user.id, headwords=focus_words)

    dynamic = {
        "user": user_block,
        "library_summary": summary,
        "focus_words": focus_ctx,
        "recent_7d_quiz": review_ctx,
    }

    addendum = (
        "\n\n## 学生状态（动态上下文）\n"
        "下面这段 JSON 是学生当前的学习状态，请基于它来调整答复深度与角度。"
        "不要把 JSON 原文回显给学生。\n\n"
        + "```json\n"
        + json.dumps(dynamic, ensure_ascii=False, indent=2)
        + "\n```"
    )
    return base + addendum


# ── History assembly ────────────────────────────────────────────────────────

def _history_to_user_prompt(messages: List[CoachMessage], new_user_text: str) -> str:
    """Render persisted history + the new user turn into a single user message.

    Treating the whole conversation as one user prompt (rather than the
    multi-turn API shape) keeps the system prompt cacheable across turns —
    Anthropic's prompt cache invalidates whenever the message list changes.
    A small accuracy trade-off for a measurable cost saving.
    """
    pieces: List[str] = []
    for m in messages[-HISTORY_WINDOW:]:
        if m.role == "user":
            pieces.append(f"学生: {m.content}")
        elif m.role == "assistant":
            pieces.append(f"导师: {m.content}")
    pieces.append(f"学生: {new_user_text}")
    pieces.append("导师:")
    return "\n\n".join(pieces)


# ── Persistence helpers ─────────────────────────────────────────────────────

def list_threads(db: Session, *, user_id: int, include_archived: bool = False) -> List[Dict[str, Any]]:
    q = db.query(CoachThread).filter(CoachThread.user_id == user_id)
    if not include_archived:
        q = q.filter(CoachThread.archived.is_(False))
    q = q.order_by(CoachThread.pinned.desc(), CoachThread.updated_at.desc())
    return [_thread_row(t) for t in q.all()]


def get_thread(db: Session, *, user_id: int, thread_id: int) -> Optional[CoachThread]:
    return (
        db.query(CoachThread)
        .filter(CoachThread.id == thread_id, CoachThread.user_id == user_id)
        .first()
    )


def get_thread_messages(db: Session, *, thread: CoachThread, limit: int = 100) -> List[Dict[str, Any]]:
    msgs = (
        db.query(CoachMessage)
        .filter(CoachMessage.thread_id == thread.id)
        .order_by(CoachMessage.created_at.asc())
        .limit(limit)
        .all()
    )
    return [_msg_row(m) for m in msgs]


def create_thread(
    db: Session, *, user_id: int, title: Optional[str], focus_words: Optional[List[str]],
    provider: Optional[str],
) -> CoachThread:
    thread = CoachThread(
        user_id=user_id,
        title=(title or "未命名对话").strip()[:120] or "未命名对话",
        focus_words=json.dumps(focus_words or [], ensure_ascii=False),
        provider=provider,
    )
    db.add(thread)
    db.commit()
    db.refresh(thread)
    return thread


def update_thread(
    db: Session, *, user_id: int, thread_id: int, fields: Dict[str, Any],
) -> Optional[CoachThread]:
    thread = get_thread(db, user_id=user_id, thread_id=thread_id)
    if thread is None:
        return None
    if "title" in fields and fields["title"]:
        thread.title = str(fields["title"]).strip()[:120] or thread.title
    if "pinned" in fields:
        thread.pinned = bool(fields["pinned"])
    if "archived" in fields:
        thread.archived = bool(fields["archived"])
    if "focus_words" in fields and fields["focus_words"] is not None:
        thread.focus_words = json.dumps(list(fields["focus_words"])[:20], ensure_ascii=False)
    db.commit()
    db.refresh(thread)
    return thread


def delete_thread(db: Session, *, user_id: int, thread_id: int) -> bool:
    thread = get_thread(db, user_id=user_id, thread_id=thread_id)
    if thread is None:
        return False
    db.query(CoachMessage).filter(CoachMessage.thread_id == thread.id).delete(synchronize_session=False)
    db.delete(thread)
    db.commit()
    return True


def append_message(
    db: Session, *, thread: CoachThread, role: str, content: str,
    provider: Optional[str] = None, model: Optional[str] = None,
    input_tokens: int = 0, output_tokens: int = 0, latency_ms: int = 0,
    error: Optional[str] = None,
) -> CoachMessage:
    msg = CoachMessage(
        thread_id=thread.id, user_id=thread.user_id,
        role=role, content=content,
        provider=provider, model=model,
        input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0),
        latency_ms=int(latency_ms or 0),
        error=(error or None),
    )
    db.add(msg)
    thread.message_count = (thread.message_count or 0) + 1
    if role == "assistant":
        thread.total_input_tokens = (thread.total_input_tokens or 0) + int(input_tokens or 0)
        thread.total_output_tokens = (thread.total_output_tokens or 0) + int(output_tokens or 0)
        if provider:
            thread.provider = provider
        if model:
            thread.model = model
    thread.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(msg)
    db.refresh(thread)
    return msg


# ── Async LLM round-trip ────────────────────────────────────────────────────

@dataclass
class CoachReply:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


async def call_coach(
    *, system_prompt: str, user_prompt: str, provider_name: Optional[str],
) -> CoachReply:
    """Async LLM round-trip with retries + observability.

    Resolves the provider once: callers should make sure an LLM is
    configured before reaching this point (use :func:`is_llm_provider` + a
    runtime config check in the endpoint handler).
    """
    if not is_llm_provider(provider_name or ""):
        # `resolve_active_llm` raises ValueError; convert to a clearer
        # message for the endpoint layer.
        raise RuntimeError(
            "AI 词汇教练需要配置一个 LLM provider (Claude / DeepSeek / OpenAI)。"
            "请在管理面板中设置 API key 后重试。"
        )
    provider, model = resolve_active_llm(provider_name)
    t0 = time.monotonic()
    resp = await chat(
        provider=provider,
        model=model,
        system=system_prompt,
        user=user_prompt,
        max_tokens=1024,
        temperature=0.4,
        use_prompt_cache=True,
        label=f"coach:{provider}",
    )
    latency = int((time.monotonic() - t0) * 1000)
    return CoachReply(
        text=resp.text,
        provider=provider,
        model=model,
        input_tokens=resp.input_tokens or 0,
        output_tokens=resp.output_tokens or 0,
        latency_ms=latency,
    )


# ── Stats ───────────────────────────────────────────────────────────────────

def user_coach_stats(db: Session, *, user_id: int) -> Dict[str, Any]:
    """Aggregate coach usage for a user — exposed to the frontend dashboard."""
    totals = db.query(
        func.count(CoachThread.id),
        func.coalesce(func.sum(CoachThread.message_count), 0),
        func.coalesce(func.sum(CoachThread.total_input_tokens), 0),
        func.coalesce(func.sum(CoachThread.total_output_tokens), 0),
    ).filter(CoachThread.user_id == user_id).one()
    threads, msgs, in_tok, out_tok = totals

    pinned = db.query(func.count(CoachThread.id)).filter(
        CoachThread.user_id == user_id,
        CoachThread.pinned.is_(True),
    ).scalar() or 0
    archived = db.query(func.count(CoachThread.id)).filter(
        CoachThread.user_id == user_id,
        CoachThread.archived.is_(True),
    ).scalar() or 0
    return {
        "threads": int(threads or 0),
        "messages": int(msgs or 0),
        "pinned": int(pinned),
        "archived": int(archived),
        "total_input_tokens": int(in_tok or 0),
        "total_output_tokens": int(out_tok or 0),
    }


# ── Row → dict helpers ──────────────────────────────────────────────────────

def _thread_row(t: CoachThread) -> Dict[str, Any]:
    try:
        focus = json.loads(t.focus_words) if t.focus_words else []
    except Exception:
        focus = []
    return {
        "id": t.id,
        "title": t.title,
        "provider": t.provider,
        "model": t.model,
        "focus_words": focus,
        "pinned": bool(t.pinned),
        "archived": bool(t.archived),
        "message_count": int(t.message_count or 0),
        "total_input_tokens": int(t.total_input_tokens or 0),
        "total_output_tokens": int(t.total_output_tokens or 0),
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


def _msg_row(m: CoachMessage) -> Dict[str, Any]:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "provider": m.provider,
        "model": m.model,
        "input_tokens": int(m.input_tokens or 0),
        "output_tokens": int(m.output_tokens or 0),
        "latency_ms": int(m.latency_ms or 0),
        "error": m.error,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }
