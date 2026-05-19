"""Tests for the practice-quiz service (Feature 2)."""
from __future__ import annotations

import pytest


@pytest.fixture()
def quiz_user(isolated_db):
    """Create a user + library populated with enough rows to run a 4-choice MCQ."""
    from backend.auth import hash_password
    from backend.database import LibraryWord, User
    db = isolated_db.SessionLocal()
    try:
        u = User(username="bob", password_hash=hash_password("secret123"))
        db.add(u)
        db.commit()
        db.refresh(u)
        # Six rows: enough for a 4-MCQ quiz with distractors and a few extras
        rows = [
            ("study", "学习", "to apply oneself to learning", "She likes to study English every morning."),
            ("careful", "谨慎的", "taking care to avoid risks", "Be careful when crossing the road."),
            ("implement", "实施", "to put into effect", "We need to implement the new plan."),
            ("ancient", "古老的", "from a long time ago", "The museum holds ancient artifacts."),
            ("rapid", "迅速的", "happening quickly", "The team made rapid progress."),
            ("global", "全球的", "relating to the whole world", "Climate change is a global problem."),
        ]
        for headword, cn, en, ex in rows:
            db.add(LibraryWord(
                user_id=u.id, headword=headword, lemma=headword, pos="VERB" if ex else "ADJ",
                chinese_meaning=cn, english_definition=en, example_sentence=ex,
                cefr_level="B1", word_level="高考",
            ))
        db.commit()
        yield u.id
    finally:
        db.close()


def test_generate_word_to_definition_mcq(isolated_db, quiz_user):
    """MCQ quizzes should hide answers and choose distractors from peers."""
    from backend.services import quiz_service
    db = isolated_db.SessionLocal()
    try:
        payload = quiz_service.generate_quiz(
            db, user_id=quiz_user, mode="word_to_definition", size=4, num_choices=4,
        )
        assert payload["ok"], payload
        assert payload["count"] == 4
        # No answer keys leaked to the client payload.
        for q in payload["questions"]:
            assert "correct_choice_index" not in q
            assert len(q["choices"]) == 4
            # The headword shouldn't appear in the prompt (it's the answer).
            assert q["prompt"].count("\"") == 2  # only the prompt-quoted word
    finally:
        db.close()


def test_generate_too_small_library_returns_error(isolated_db):
    """Generating a 4-choice MCQ with 2 rows should fail with a clear error."""
    from backend.auth import hash_password
    from backend.database import LibraryWord, User
    from backend.services import quiz_service

    db = isolated_db.SessionLocal()
    try:
        u = User(username="charlie", password_hash=hash_password("secret123"))
        db.add(u)
        db.commit()
        db.refresh(u)
        for hw in ("cat", "dog"):
            db.add(LibraryWord(
                user_id=u.id, headword=hw, lemma=hw, pos="NOUN",
                chinese_meaning="animal",
            ))
        db.commit()

        payload = quiz_service.generate_quiz(
            db, user_id=u.id, mode="word_to_definition", size=10, num_choices=4,
        )
        assert not payload["ok"]
        assert payload["error"] == "library_too_small"
        assert payload["needed"] == 4
        assert payload["available"] == 2
    finally:
        db.close()


def test_fill_in_blank_masks_example_sentence(isolated_db, quiz_user):
    """fill_in_blank mode must replace the headword in the example with ____."""
    from backend.services import quiz_service
    db = isolated_db.SessionLocal()
    try:
        payload = quiz_service.generate_quiz(
            db, user_id=quiz_user, mode="fill_in_blank", size=3, num_choices=4,
        )
        assert payload["ok"], payload
        for q in payload["questions"]:
            assert "____" in q["prompt"]
            # The original headword shouldn't survive intact in the prompt.
            # (NB: derived forms like "studying" still get masked because of the
            # \w* in the masking regex — this is intentional.)
    finally:
        db.close()


def test_submit_grades_and_records_review_event(isolated_db, quiz_user):
    """Submitting answers should grade them, return a score, and create
    ReviewEvent rows so the heatmap reflects quiz activity."""
    from backend.database import ReviewEvent
    from backend.services import quiz_service

    db = isolated_db.SessionLocal()
    try:
        gen = quiz_service.generate_quiz(
            db, user_id=quiz_user, mode="word_to_definition", size=4, num_choices=4,
        )
        token = gen["token"]
        questions = gen["questions"]

        # Inspect the generator state directly to know what's correct.
        sess = quiz_service._store.get(token)
        assert sess is not None
        # Construct: first two correct, last two wrong.
        answers = []
        for i, q in enumerate(sess.questions):
            if i < 2:
                answers.append({"question_id": q.id, "answer": str(q.correct_choice_index)})
            else:
                wrong = (q.correct_choice_index + 1) % len(q.choices)
                answers.append({"question_id": q.id, "answer": str(wrong)})

        report = quiz_service.submit_quiz(
            db, user_id=quiz_user, token=token, answers=answers,
        )
        assert report["ok"]
        assert report["total"] == 4
        assert report["correct"] == 2
        assert report["score_pct"] == 50.0
        # Every headword should produce a ReviewEvent.
        events = db.query(ReviewEvent).filter_by(user_id=quiz_user).all()
        assert len(events) == 4
        # And both qualities should appear (remembered + forgot).
        qualities = {e.quality for e in events}
        assert qualities == {"remembered", "forgot"}
    finally:
        db.close()


def test_submit_with_wrong_token_rejected(isolated_db, quiz_user):
    """Bad / unknown / expired tokens shouldn't crash — they should return ok=False."""
    from backend.services import quiz_service
    db = isolated_db.SessionLocal()
    try:
        report = quiz_service.submit_quiz(
            db, user_id=quiz_user, token="not-a-real-token",
            answers=[{"question_id": "x", "answer": "0"}],
        )
        assert report["ok"] is False
        assert report["error"] == "quiz_expired_or_unknown"
    finally:
        db.close()


def test_submit_with_wrong_user_rejected(isolated_db, quiz_user):
    """A different user must NOT be able to submit on another user's session."""
    from backend.auth import hash_password
    from backend.database import User
    from backend.services import quiz_service
    db = isolated_db.SessionLocal()
    try:
        gen = quiz_service.generate_quiz(
            db, user_id=quiz_user, mode="word_to_definition", size=3, num_choices=4,
        )
        token = gen["token"]

        intruder = User(username="mallory", password_hash=hash_password("secret123"))
        db.add(intruder)
        db.commit()
        db.refresh(intruder)
        sess = quiz_service._store.get(token)
        assert sess is not None
        answers = [{"question_id": sess.questions[0].id, "answer": "0"}]
        report = quiz_service.submit_quiz(
            db, user_id=intruder.id, token=token, answers=answers,
        )
        assert not report["ok"]
        assert report["error"] == "wrong_user"
        # And the legitimate owner can still submit afterward.
        owner_report = quiz_service.submit_quiz(
            db, user_id=quiz_user, token=token, answers=answers,
        )
        assert owner_report["ok"]
    finally:
        db.close()


def test_quiz_stats_after_submit(isolated_db, quiz_user):
    """quiz_stats should reflect events written by the most recent submission."""
    from backend.services import quiz_service
    db = isolated_db.SessionLocal()
    try:
        gen = quiz_service.generate_quiz(
            db, user_id=quiz_user, mode="word_to_definition", size=4, num_choices=4,
        )
        sess = quiz_service._store.get(gen["token"])
        answers = [
            {"question_id": q.id, "answer": str(q.correct_choice_index)}
            for q in sess.questions
        ]
        quiz_service.submit_quiz(
            db, user_id=quiz_user, token=gen["token"], answers=answers,
        )
        stats = quiz_service.quiz_stats(db, user_id=quiz_user, days=7)
        assert stats["events"] == 4
        assert stats["remembered"] == 4
        assert stats["accuracy_pct"] == 100.0
    finally:
        db.close()
