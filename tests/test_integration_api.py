"""End-to-end API integration tests for the new endpoints.

Exercises the full FastAPI stack (auth → endpoint → service → DB) so we
catch wiring mistakes that the service-level tests miss.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def api_client(isolated_db):
    """Build a TestClient against the freshly initialised app."""
    from fastapi.testclient import TestClient
    from backend.main import app
    return TestClient(app)


@pytest.fixture()
def auth_headers(api_client):
    """Register a fresh user and return Bearer headers."""
    # Registration must succeed against a clean DB.
    resp = api_client.post(
        "/auth/register",
        json={"username": "ellie", "password": "secret123"},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _seed_library(api_client, headers):
    """Add a small library so the new endpoints have something to chew on."""
    rows = [
        ("study", "学习", "to apply oneself", "She likes to study English."),
        ("careful", "谨慎的", "taking care", "Be careful when crossing."),
        ("implement", "实施", "put into effect", "We will implement the plan."),
        ("ancient", "古老的", "from long ago", "An ancient temple stood here."),
        ("rapid", "迅速的", "very fast", "The team made rapid progress."),
        ("global", "全球的", "worldwide", "Climate change is a global issue."),
    ]
    for hw, cn, en, ex in rows:
        resp = api_client.post(
            "/api/library",
            headers=headers,
            json={
                "headword": hw,
                "lemma": hw,
                "chinese_meaning": cn,
                "english_definition": en,
                "example_sentence": ex,
            },
        )
        assert resp.status_code == 200, resp.text


def test_words_related_endpoint(api_client, auth_headers):
    """GET /api/words/related returns grouped suggestions for a known word."""
    _seed_library(api_client, auth_headers)
    resp = api_client.get(
        "/api/words/related",
        headers=auth_headers,
        params={"word": "study", "limit": 10},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["word"] == "study"
    assert "groups" in data
    assert data["total"] >= 0  # at least the structure is right


def test_library_related_for_unknown_id_404s(api_client, auth_headers):
    resp = api_client.get(
        "/api/library/9999999/related",
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_library_gap_suggestions_endpoint(api_client, auth_headers):
    """Even with no exams uploaded, gap suggestions should return frequency-based items."""
    _seed_library(api_client, auth_headers)
    resp = api_client.get(
        "/api/library/suggestions/gaps",
        headers=auth_headers,
        params={"limit": 5},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["source"] in {"frequency", "exam_exposure"}
    assert isinstance(data["items"], list)
    # We seeded 6 words; gap items must not include any of them.
    in_library = {"study", "careful", "implement", "ancient", "rapid", "global"}
    gap_words = {item["word"] for item in data["items"]}
    assert not (gap_words & in_library)


def test_quiz_generate_and_submit_full_loop(api_client, auth_headers):
    """End-to-end: generate quiz → answer all questions correctly → check score."""
    _seed_library(api_client, auth_headers)

    gen = api_client.post(
        "/api/quiz/generate",
        headers=auth_headers,
        json={"mode": "word_to_definition", "size": 4, "num_choices": 4},
    )
    assert gen.status_code == 200, gen.text
    payload = gen.json()
    assert payload["ok"]
    token = payload["token"]

    # Look up the in-memory session so we know which choice indexes are right.
    from backend.services.quiz_service import _store
    sess = _store.get(token)
    answers = [
        {"question_id": q.id, "answer": str(q.correct_choice_index)}
        for q in sess.questions
    ]

    submit = api_client.post(
        "/api/quiz/submit",
        headers=auth_headers,
        json={"token": token, "answers": answers, "record_review_event": True},
    )
    assert submit.status_code == 200, submit.text
    report = submit.json()
    assert report["ok"]
    assert report["correct"] == report["total"]
    assert report["score_pct"] == 100.0
    assert report["review_events_written"] == report["total"]


def test_quiz_submit_expired_token_returns_410(api_client, auth_headers):
    resp = api_client.post(
        "/api/quiz/submit",
        headers=auth_headers,
        json={
            "token": "fake-token-that-doesnt-exist",
            "answers": [{"question_id": "q1", "answer": "0"}],
        },
    )
    assert resp.status_code == 410


def test_quiz_stats_endpoint(api_client, auth_headers):
    """Stats endpoint should return zero events before any quiz is submitted."""
    _seed_library(api_client, auth_headers)
    resp = api_client.get("/api/quiz/stats", headers=auth_headers, params={"days": 14})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["events"] == 0
    assert data["accuracy_pct"] == 0.0
    assert data["days"] == 14


def test_quiz_generate_too_small_library_returns_400(api_client, auth_headers):
    """Endpoint should surface a 400 (with explanatory message) when the
    library has fewer entries than needed for MCQ distractors."""
    # Only add 2 entries: not enough for a 4-MCQ.
    for hw in ("apple", "banana"):
        resp = api_client.post(
            "/api/library",
            headers=auth_headers,
            json={"headword": hw, "lemma": hw, "chinese_meaning": "fruit"},
        )
        assert resp.status_code == 200
    resp = api_client.post(
        "/api/quiz/generate",
        headers=auth_headers,
        json={"mode": "word_to_definition", "size": 5, "num_choices": 4},
    )
    assert resp.status_code == 400, resp.text
    assert "不足" in resp.text or "available" in resp.text.lower()


def test_endpoints_require_auth(api_client):
    """All new endpoints must reject unauthenticated callers."""
    paths = [
        ("GET", "/api/words/related?word=study"),
        ("POST", "/api/words/related"),
        ("GET", "/api/library/1/related"),
        ("GET", "/api/library/suggestions/gaps"),
        ("POST", "/api/quiz/generate"),
        ("POST", "/api/quiz/submit"),
        ("GET", "/api/quiz/stats"),
    ]
    for method, path in paths:
        if method == "GET":
            resp = api_client.get(path)
        else:
            resp = api_client.post(path, json={})
        assert resp.status_code == 401, (path, resp.status_code, resp.text)
