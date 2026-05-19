"""Tests for the word-relations service (Feature 1)."""
from __future__ import annotations

import json

import pytest


@pytest.fixture()
def sample_user(isolated_db):
    """Insert a user with a small library so the related/gap tests have data."""
    from backend.auth import hash_password
    from backend.database import LibraryWord, User
    db = isolated_db.SessionLocal()
    try:
        u = User(username="alice", password_hash=hash_password("secret123"))
        db.add(u)
        db.commit()
        db.refresh(u)
        # A small library with deliberately overlapping families.
        rows = [
            LibraryWord(
                user_id=u.id, headword="study", lemma="study", pos="VERB",
                chinese_meaning="学习", english_definition="apply oneself",
                cefr_level="A2", word_level="高考", zipf_score="5.65",
                tags="education",
                source_exam_code="EXAM001",
            ),
            LibraryWord(
                user_id=u.id, headword="careful", lemma="careful", pos="ADJ",
                chinese_meaning="谨慎的", english_definition="cautious",
                cefr_level="B1", word_level="高考", zipf_score="4.6",
                tags="adjective",
                source_exam_code="EXAM001",
            ),
            LibraryWord(
                user_id=u.id, headword="implement", lemma="implement", pos="VERB",
                chinese_meaning="实施", english_definition="put into effect",
                cefr_level="B2", word_level="四六级", zipf_score="4.6",
                tags="education,career",
                source_exam_code="EXAM002",
            ),
        ]
        for r in rows:
            db.add(r)
        db.commit()
        yield u.id
    finally:
        db.close()


def test_related_for_word_returns_groups(isolated_db, sample_user):
    """related_for_word should return at least one non-empty group for a common word."""
    from backend.services import word_relations
    db = isolated_db.SessionLocal()
    try:
        result = word_relations.related_for_word(
            db, user_id=sample_user, word="study", limit=20,
        )
        assert result["word"] == "study"
        # Three categories possible, at least one should fire on a gaokao word.
        assert any(g["items"] for g in result["groups"]), result
        # `in_library` should be True for the entry the user already has.
        # Look across all groups for "study"-derived words.
        all_items = [item for g in result["groups"] for item in g["items"]]
        # We always exclude the anchor word itself.
        assert all(item["word"] != "study" for item in all_items)
    finally:
        db.close()


def test_related_unknown_word_returns_empty(isolated_db, sample_user):
    """Truly nonsense input should return an empty payload, not crash."""
    from backend.services import word_relations
    db = isolated_db.SessionLocal()
    try:
        result = word_relations.related_for_word(
            db, user_id=sample_user, word="qwxzfoo", limit=10,
        )
        # No family match, no peers (zipf is 0). At most an empty groups list.
        assert result["total"] == 0
        assert result["groups"] == []
    finally:
        db.close()


def test_related_for_library_entry_adds_tag_siblings(isolated_db, sample_user):
    """An anchored library entry should surface tag/exam siblings."""
    from backend.database import LibraryWord
    from backend.services import word_relations
    db = isolated_db.SessionLocal()
    try:
        anchor = db.query(LibraryWord).filter_by(headword="study").first()
        result = word_relations.related_for_library_entry(
            db, user_id=sample_user, word_id=anchor.id, limit=20,
        )
        assert result["library_id"] == anchor.id
        # "implement" shares the "education" tag with "study"
        sibling_groups = [g for g in result["groups"] if g["relation"] == "tag_or_exam_sibling"]
        sibling_words = [
            i["word"] for g in sibling_groups for i in g["items"]
        ]
        assert "implement" in sibling_words, sibling_words
    finally:
        db.close()


def test_suggest_gaps_returns_words_not_in_library(isolated_db, sample_user):
    """Gap suggestions must exclude every word already saved."""
    from backend.services import word_relations
    db = isolated_db.SessionLocal()
    try:
        payload = word_relations.suggest_gaps_for_user(
            db, user_id=sample_user, limit=15,
        )
        # source might be 'frequency' since no exam history exists; either way
        # the items should be a non-empty subset of gaokao and should NOT
        # include the user's existing words.
        in_library = {"study", "careful", "implement"}
        gaps = {item["word"] for item in payload["items"]}
        assert gaps, "expected at least one gap suggestion"
        assert not (gaps & in_library), gaps & in_library
        # Every item should be a real gaokao word — sanity-check the very first.
        from backend.services.wordlist_service import get_gaokao_words
        gaokao = get_gaokao_words()
        assert all(g in gaokao for g in gaps)
    finally:
        db.close()


def test_suggest_gaps_uses_exam_exposure_when_available(isolated_db, sample_user):
    """When the user has saved exam analyses, the gap source should switch
    to ``exam_exposure`` so suggestions are biased toward what showed up
    in *their* uploaded exams."""
    from backend.database import Exam
    from backend.services import word_relations
    db = isolated_db.SessionLocal()
    try:
        # Inject a fake analysis result with a gaokao word the user hasn't saved.
        # 'travel' is in the gaokao 3500 list.
        fake_result = {
            "task_id": "t1", "filename": "fake.txt",
            "lemma_table": [
                {"lemma": "travel", "score": 12.5, "total_count": 5},
                {"lemma": "manage", "score": 9.0, "total_count": 4},
            ],
        }
        exam = Exam(
            user_id=sample_user,
            task_id="task-fake",
            filename="fake.pdf",
            exam_code="FAKECODE",
            result_json=json.dumps(fake_result),
        )
        db.add(exam)
        db.commit()

        payload = word_relations.suggest_gaps_for_user(
            db, user_id=sample_user, limit=10,
        )
        assert payload["source"] == "exam_exposure"
        words = [i["word"] for i in payload["items"]]
        # At least one of the words we just embedded should bubble up.
        assert any(w in words for w in ["travel", "manage"]), words
    finally:
        db.close()
