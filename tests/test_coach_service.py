"""Tests for the AI Vocabulary Coach service.

We can't reliably call a real LLM in CI, so the tests cover:
  * thread CRUD + persistence
  * system-prompt context assembly (library snapshot + recent reviews)
  * graceful error path when no LLM provider is configured

The endpoint that calls the LLM is verified to return a clear 400 (not a
500) when no API key is set.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def coach_user(isolated_db):
    """User with a small library + a couple of review events."""
    from backend.auth import hash_password
    from backend.database import LibraryWord, ReviewEvent, User

    db = isolated_db.SessionLocal()
    try:
        u = User(username="mei", password_hash=hash_password("secret123"))
        db.add(u)
        db.commit()
        db.refresh(u)

        rows = [
            ("eloquent", "雄辩的", "fluent and persuasive"),
            ("ambiguous", "模糊的", "open to multiple interpretations"),
            ("resilient", "有韧性的", "able to recover quickly"),
        ]
        for hw, cn, en in rows:
            db.add(LibraryWord(
                user_id=u.id, headword=hw, lemma=hw, pos="ADJ",
                chinese_meaning=cn, english_definition=en,
                example_sentence=f"She was {hw} during the debate.",
                cefr_level="B2", word_level="高考",
            ))
        # Two recent forgots for "eloquent" so the snapshot picks it up.
        for _ in range(2):
            db.add(ReviewEvent(
                user_id=u.id, headword="eloquent", quality="forgot",
                box_before=0, box_after=0,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2),
            ))
        db.commit()
        yield u.id
    finally:
        db.close()


def test_create_and_list_threads(isolated_db, coach_user):
    from backend.services import coach_service
    db = isolated_db.SessionLocal()
    try:
        t = coach_service.create_thread(
            db, user_id=coach_user, title="高难形容词",
            focus_words=["eloquent", "resilient"], provider="claude",
        )
        assert t.id is not None
        threads = coach_service.list_threads(db, user_id=coach_user)
        assert any(row["id"] == t.id for row in threads)
        assert threads[0]["focus_words"] == ["eloquent", "resilient"]
    finally:
        db.close()


def test_system_prompt_includes_focus_word_context(isolated_db, coach_user):
    from backend.database import User
    from backend.services import coach_service
    db = isolated_db.SessionLocal()
    try:
        user = db.query(User).filter_by(id=coach_user).first()
        prompt = coach_service._build_system_prompt(
            db, user=user, focus_words=["eloquent"], include_library_context=True,
        )
        assert "eloquent" in prompt
        # Recent forgets surface in the dynamic context
        assert "雄辩" in prompt or "fluent" in prompt
        assert "forgot" in prompt
    finally:
        db.close()


def test_system_prompt_without_context(isolated_db, coach_user):
    from backend.database import User
    from backend.services import coach_service
    db = isolated_db.SessionLocal()
    try:
        user = db.query(User).filter_by(id=coach_user).first()
        prompt = coach_service._build_system_prompt(
            db, user=user, focus_words=[], include_library_context=False,
        )
        # No dynamic block → no JSON snapshot
        assert "学生状态" not in prompt
        assert "词汇导师" in prompt
    finally:
        db.close()


def test_update_thread(isolated_db, coach_user):
    from backend.services import coach_service
    db = isolated_db.SessionLocal()
    try:
        t = coach_service.create_thread(
            db, user_id=coach_user, title="test", focus_words=[], provider=None,
        )
        updated = coach_service.update_thread(
            db, user_id=coach_user, thread_id=t.id,
            fields={"title": "renamed", "pinned": True},
        )
        assert updated is not None
        assert updated.title == "renamed"
        assert updated.pinned is True
    finally:
        db.close()


def test_delete_thread_removes_messages(isolated_db, coach_user):
    from backend.database import CoachMessage
    from backend.services import coach_service
    db = isolated_db.SessionLocal()
    try:
        t = coach_service.create_thread(
            db, user_id=coach_user, title="t", focus_words=[], provider=None,
        )
        coach_service.append_message(db, thread=t, role="user", content="hi")
        coach_service.append_message(db, thread=t, role="assistant", content="hello")
        assert coach_service.delete_thread(db, user_id=coach_user, thread_id=t.id) is True
        remaining = db.query(CoachMessage).filter_by(thread_id=t.id).count()
        assert remaining == 0
    finally:
        db.close()


def test_get_stats(isolated_db, coach_user):
    from backend.services import coach_service
    db = isolated_db.SessionLocal()
    try:
        t = coach_service.create_thread(
            db, user_id=coach_user, title="t", focus_words=[], provider=None,
        )
        coach_service.append_message(
            db, thread=t, role="assistant", content="reply",
            provider="claude", input_tokens=10, output_tokens=5,
        )
        stats = coach_service.user_coach_stats(db, user_id=coach_user)
        assert stats["threads"] == 1
        assert stats["messages"] == 1
        assert stats["total_input_tokens"] == 10
        assert stats["total_output_tokens"] == 5
    finally:
        db.close()


def test_coach_ask_returns_400_without_llm_key(isolated_db, coach_user, monkeypatch):
    """When no LLM provider is configured, /api/coach/ask should 400 not 500."""
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.database import User
    from backend.main import app

    db = isolated_db.SessionLocal()
    try:
        user = db.query(User).filter_by(id=coach_user).first()
        token = create_token(user.id, user.username, user.is_admin)
    finally:
        db.close()

    client = TestClient(app)
    # The default config picks `vocab_provider="claude"` but there's no key
    # in the test env — so we expect a 400 with a friendly message.
    resp = client.post(
        "/api/coach/ask",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "question": "What does 'eloquent' mean?",
            "focus_words": ["eloquent"],
            "include_library_context": True,
        },
    )
    # 400 (LLM unavailable) or 502 (provider key missing under the hood) —
    # never 500 (uncaught crash).
    assert resp.status_code in (400, 502), resp.text


def test_coach_endpoints_require_auth(isolated_db):
    from fastapi.testclient import TestClient
    from backend.main import app
    client = TestClient(app)
    for method, path in (
        ("GET", "/api/coach/threads"),
        ("POST", "/api/coach/threads"),
        ("GET", "/api/coach/threads/1"),
        ("POST", "/api/coach/threads/1/messages"),
        ("POST", "/api/coach/ask"),
        ("GET", "/api/coach/stats"),
    ):
        if method == "GET":
            resp = client.get(path)
        else:
            resp = client.post(path, json={"question": "x", "content": "x"})
        assert resp.status_code == 401, (path, resp.status_code, resp.text)
