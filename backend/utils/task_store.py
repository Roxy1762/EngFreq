"""Thread-safe container for in-memory background-task state.

The original implementation used bare module-level dicts which could be
concurrently mutated from FastAPI request handlers and background workers.
Under the single-process dev server this rarely manifests, but with multiple
event-loop tasks touching the same entry (e.g. polling a task while a vocab
job writes to it) the lack of locking is a latent bug.

This module provides a small typed wrapper that guards every mutation with a
lock while keeping the public surface minimal. Callers should prefer
``TaskStore.update()`` over mutating the returned TaskStatus in place.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Iterator, Optional

from backend.models.schemas import TaskStatus


class TaskStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: Dict[str, TaskStatus] = {}
        self._texts: Dict[str, str] = {}
        self._meta: Dict[str, dict] = {}

    # ── Task status ──────────────────────────────────────────────────────────
    def set(self, task_id: str, task: TaskStatus) -> None:
        with self._lock:
            self._tasks[task_id] = task

    def get(self, task_id: str) -> Optional[TaskStatus]:
        with self._lock:
            return self._tasks.get(task_id)

    def update(self, task_id: str, **kwargs: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            for key, value in kwargs.items():
                setattr(task, key, value)

    # ── Extracted text buffer (cleared on purge) ─────────────────────────────
    def set_text(self, task_id: str, text: str) -> None:
        with self._lock:
            self._texts[task_id] = text

    def get_text(self, task_id: str) -> str:
        with self._lock:
            return self._texts.get(task_id, "")

    # ── Persistence metadata (exam_id, codes, ownership) ─────────────────────
    def set_meta(self, task_id: str, meta: dict) -> None:
        with self._lock:
            self._meta[task_id] = meta

    def get_meta(self, task_id: str) -> dict:
        with self._lock:
            return dict(self._meta.get(task_id, {}))

    def merge_meta(self, task_id: str, **updates: Any) -> dict:
        with self._lock:
            current = self._meta.setdefault(task_id, {})
            current.update(updates)
            return dict(current)

    def purge(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)
            self._texts.pop(task_id, None)
            self._meta.pop(task_id, None)

    # ── Introspection ────────────────────────────────────────────────────────
    def active_task_ids(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._tasks.keys()))

    def size(self) -> int:
        with self._lock:
            return len(self._tasks)
