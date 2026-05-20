"""Regression tests for library bulk enroll + word_family memoisation."""
from __future__ import annotations


def test_enroll_many_dedups_and_skips_existing(isolated_db):
    """enroll_many should be idempotent and skip blanks/dupes in one transaction."""
    from backend.database import LibraryWord, ReviewItem, SessionLocal, User
    from backend.services import library_service

    with SessionLocal() as db:
        user = User(username="lib-batch", password_hash="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        db.add(LibraryWord(user_id=user.id, headword="bittersweet", lemma="bittersweet"))
        db.commit()

        # First batch: includes dupes, blanks, and one already-enrolled headword.
        count = library_service.enroll_many(
            db, user_id=user.id,
            headwords=["alpha", " ", "beta", "alpha", "", "gamma"],
        )
        assert count == 3  # alpha, beta, gamma after dedup + empty filter

        rows = db.query(ReviewItem).filter_by(user_id=user.id).all()
        assert sorted(r.headword for r in rows) == ["alpha", "beta", "gamma"]

        # Re-enroll should be a no-op for already-queued words.
        count2 = library_service.enroll_many(
            db, user_id=user.id, headwords=["alpha", "delta", "beta"],
        )
        assert count2 == 3   # returned dedup count, even if some skipped
        rows2 = db.query(ReviewItem).filter_by(user_id=user.id).all()
        assert sorted(r.headword for r in rows2) == ["alpha", "beta", "delta", "gamma"]

        # bittersweet is in library but not yet enrolled; enrolling links to it.
        library_service.enroll_many(db, user_id=user.id, headwords=["bittersweet"])
        linked = (
            db.query(ReviewItem)
            .filter_by(user_id=user.id, headword="bittersweet")
            .first()
        )
        assert linked is not None
        assert linked.library_word_id is not None


def test_get_family_id_is_memoised(isolated_db):
    """The LRU cache exposes a cache_info attribute and short-circuits repeated calls."""
    from backend.services.word_family import get_family_id

    get_family_id.cache_clear()
    # First call: miss
    assert get_family_id("studies") == get_family_id("studies")
    info = get_family_id.cache_info()
    assert info.hits >= 1
    assert info.misses >= 1

    # Repeated calls collapse to cache hits, not recomputation.
    for _ in range(20):
        get_family_id("studies")
    final = get_family_id.cache_info()
    assert final.hits >= 20
