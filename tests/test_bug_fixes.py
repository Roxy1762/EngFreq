"""Targeted regression tests for the bug-fix sweep.

These tests exist mainly to lock down behaviours that previously regressed
or were race-prone, so future refactors notice when something breaks.
"""
from __future__ import annotations

import asyncio

import pytest


def test_task_store_concurrent_updates_dont_lose_state(isolated_db):
    """Repeatedly mutating the same task in parallel must not drop fields."""
    from backend.models.schemas import TaskStatus
    from backend.utils.task_store import TaskStore

    store = TaskStore(max_tasks=10)
    task = TaskStatus(task_id="t1", status="pending", progress=0, message="")
    store.set("t1", task)

    # Hammer the update path from many threads.
    import threading
    def writer():
        for i in range(200):
            store.update("t1", progress=i, message=f"p{i}")

    threads = [threading.Thread(target=writer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = store.get("t1")
    assert final is not None
    # We don't care which thread's value wins, only that the object is still
    # consistent and accessible.
    assert isinstance(final.progress, int)
    assert isinstance(final.message, str)


def test_task_store_lru_eviction_respects_live_tasks(isolated_db):
    """Pending/processing tasks must survive eviction even when over the cap."""
    from backend.models.schemas import TaskStatus
    from backend.utils.task_store import TaskStore

    store = TaskStore(max_tasks=3)
    # 4 tasks: first two are live, last two are done — only `done` ones should be evicted.
    store.set("alive1", TaskStatus(task_id="alive1", status="processing", progress=10))
    store.set("alive2", TaskStatus(task_id="alive2", status="pending", progress=0))
    store.set("done1", TaskStatus(task_id="done1", status="done", progress=100))
    store.set("done2", TaskStatus(task_id="done2", status="done", progress=100))

    # Adding another live task triggers eviction.
    store.set("alive3", TaskStatus(task_id="alive3", status="processing", progress=20))

    assert store.get("alive1") is not None
    assert store.get("alive2") is not None
    assert store.get("alive3") is not None
    # At least one of the "done" tasks should be gone now.
    finished_remaining = sum(1 for tid in ("done1", "done2") if store.get(tid) is not None)
    assert finished_remaining <= 1


def test_user_owns_task_returns_false_for_unknown_task(isolated_db):
    """The ownership guard must NOT silently allow access to unknown tasks."""
    from backend.auth import hash_password
    from backend.database import User
    db = isolated_db.SessionLocal()
    try:
        u = User(username="cara", password_hash=hash_password("secret123"))
        db.add(u)
        db.commit()
        db.refresh(u)

        # Import lazily so the fixture-controlled DB binding sticks.
        from backend.main import _user_owns_task
        assert _user_owns_task("never-seen-task-id", u, db) is False
    finally:
        db.close()


def test_backup_scheduler_lazy_lock(isolated_db):
    """The scheduler's lock should be createable inside a running event loop
    without erroring."""
    from backend.services import backup_scheduler

    async def runner():
        lock = backup_scheduler._get_run_lock()
        assert lock is not None
        async with lock:
            return True

    assert asyncio.run(runner()) is True


def test_migration_lock_lazy_init(isolated_db):
    """The migration import lock should also bind lazily to the right loop."""
    from backend.services import migration_service

    async def runner():
        lock = migration_service._get_import_lock()
        async with lock:
            return True

    assert asyncio.run(runner()) is True


def test_unique_code_generation_safe_under_collision(isolated_db):
    """_unique_exam_code keeps trying until it finds a free code; ensure the
    duplicate-code path doesn't crash when called against a populated DB."""
    from backend.auth import hash_password
    from backend.database import Exam, User
    from backend.main import _unique_exam_code

    db = isolated_db.SessionLocal()
    try:
        u = User(username="dan", password_hash=hash_password("secret123"))
        db.add(u)
        db.commit()
        db.refresh(u)
        # Seed a few exams so the function has to skip at least some collisions.
        for code in ("AAAAAAAA", "BBBBBBBB", "CCCCCCCC"):
            db.add(Exam(
                user_id=u.id, task_id="t" + code, filename="x.pdf",
                exam_code=code, result_json="{}",
            ))
        db.commit()
        new_code = _unique_exam_code(db)
        assert new_code not in {"AAAAAAAA", "BBBBBBBB", "CCCCCCCC"}
        assert len(new_code) == 8
    finally:
        db.close()
