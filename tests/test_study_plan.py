"""Tests for the adaptive Study Plan service + API endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture()
def plan_user(isolated_db):
    """User with a populated library + review queue, ready for plan generation."""
    from backend.auth import hash_password
    from backend.database import LibraryWord, ReviewItem, User

    db = isolated_db.SessionLocal()
    try:
        u = User(username="lin", password_hash=hash_password("secret123"))
        db.add(u)
        db.commit()
        db.refresh(u)

        rows = [
            ("study", "学习", "to apply oneself"),
            ("careful", "谨慎的", "taking care"),
            ("implement", "实施", "put into effect"),
            ("ancient", "古老的", "from long ago"),
            ("rapid", "迅速的", "very fast"),
            ("global", "全球的", "worldwide"),
            ("complex", "复杂的", "intricate"),
            ("inspire", "激励", "to motivate"),
        ]
        for hw, cn, en in rows:
            db.add(LibraryWord(
                user_id=u.id, headword=hw, lemma=hw, pos="VERB",
                chinese_meaning=cn, english_definition=en,
                example_sentence=f"They {hw} every day.",
                cefr_level="B1", word_level="高考",
            ))
        db.commit()
        # Enroll the first three in the review queue and make them due now.
        for hw in ("study", "careful", "implement"):
            db.add(ReviewItem(
                user_id=u.id, headword=hw, box=0,
                due_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1),
            ))
        db.commit()
        yield u.id
    finally:
        db.close()


def test_generate_today_plan_basic(isolated_db, plan_user):
    from backend.services import study_plan_service
    db = isolated_db.SessionLocal()
    try:
        payload = study_plan_service.get_or_create_today(
            db, user_id=plan_user, review_target=3, learn_target=2, quiz_target=4,
        )
        assert payload["review_target"] >= 0
        assert payload["learn_target"] >= 0
        assert payload["quiz_target"] >= 0
        kinds = {item["kind"] for item in payload["items"]}
        # We seeded a review queue, so review items should appear.
        assert "review" in kinds
        # Quiz items are pulled from the library and should appear.
        assert "quiz" in kinds
    finally:
        db.close()


def test_today_plan_is_idempotent_within_a_day(isolated_db, plan_user):
    """Calling get_or_create_today twice returns the same plan id."""
    from backend.services import study_plan_service
    db = isolated_db.SessionLocal()
    try:
        first = study_plan_service.get_or_create_today(db, user_id=plan_user)
        second = study_plan_service.get_or_create_today(db, user_id=plan_user)
        assert first["id"] == second["id"]
        assert first["plan_date"] == second["plan_date"]
    finally:
        db.close()


def test_force_refresh_replaces_plan(isolated_db, plan_user):
    """A forced refresh must rebuild the plan, applying the new targets."""
    from backend.services import study_plan_service
    db = isolated_db.SessionLocal()
    try:
        first = study_plan_service.get_or_create_today(
            db, user_id=plan_user, review_target=5, learn_target=3, quiz_target=4,
        )
        refreshed = study_plan_service.get_or_create_today(
            db, user_id=plan_user, force=True, review_target=1, learn_target=1, quiz_target=1,
        )
        # Targets reflect the new caller-supplied values, not the stale ones.
        assert refreshed["review_target"] <= 1
        assert refreshed["learn_target"] <= 1
        assert refreshed["quiz_target"] <= 1
        # Items reflect the new caps too — never more than the requested target.
        kinds = [it["kind"] for it in refreshed["items"]]
        assert kinds.count("review") <= 1
        assert kinds.count("learn") <= 1
        assert kinds.count("quiz") <= 1
        # And the old plan's items aren't lingering: total item count is at
        # most the sum of the new targets.
        assert len(refreshed["items"]) <= 3
    finally:
        db.close()


def test_mark_item_complete_updates_counter(isolated_db, plan_user):
    from backend.services import study_plan_service
    db = isolated_db.SessionLocal()
    try:
        payload = study_plan_service.get_or_create_today(db, user_id=plan_user)
        review_items = [i for i in payload["items"] if i["kind"] == "review"]
        assert review_items, "fixture should produce review items"
        target = review_items[0]
        updated = study_plan_service.mark_item_complete(
            db, user_id=plan_user, item_id=target["id"],
        )
        assert updated is not None
        assert updated["completed_review"] >= 1
    finally:
        db.close()


def test_mark_item_complete_unknown_returns_none(isolated_db, plan_user):
    from backend.services import study_plan_service
    db = isolated_db.SessionLocal()
    try:
        assert study_plan_service.mark_item_complete(
            db, user_id=plan_user, item_id=999999,
        ) is None
    finally:
        db.close()


def test_plan_insights_endpoint(isolated_db, plan_user):
    """Insights covers the user's recent plans + completion %."""
    from backend.services import study_plan_service
    db = isolated_db.SessionLocal()
    try:
        study_plan_service.get_or_create_today(db, user_id=plan_user)
        out = study_plan_service.insights(db, user_id=plan_user, days=30)
        assert out["days"] == 30
        assert out["plans_generated"] >= 1
        # No completion → percentage is 0
        assert "completion_pct" in out
    finally:
        db.close()


def test_today_plan_via_api(isolated_db, plan_user):
    """End-to-end: /api/plan/today returns the auto-generated plan."""
    from fastapi.testclient import TestClient
    from backend.auth import create_token
    from backend.database import User
    from backend.main import app

    db = isolated_db.SessionLocal()
    try:
        user = db.query(User).filter_by(id=plan_user).first()
        token = create_token(user.id, user.username, user.is_admin)
    finally:
        db.close()
    client = TestClient(app)
    resp = client.get("/api/plan/today", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "items" in data
    assert "plan_date" in data


def test_plan_endpoints_require_auth(isolated_db):
    from fastapi.testclient import TestClient
    from backend.main import app
    client = TestClient(app)
    for path in (
        "/api/plan/today",
        "/api/plan/insights",
        "/api/plan/history",
    ):
        resp = client.get(path)
        assert resp.status_code == 401, (path, resp.status_code)
