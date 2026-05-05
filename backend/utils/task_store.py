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
import time
from typing import Any, Dict, Iterator, List, Optional

from backend.models.schemas import TaskStatus


class TaskStore:
    """Thread-safe in-memory task registry with optional size cap for memory hygiene."""

    # Default cap: roughly bounds memory at ~a few MB even with large extracted-text buffers.
    # 0 disables eviction entirely (legacy behaviour).
    _DEFAULT_MAX_TASKS = 200

    def __init__(self, max_tasks: int = _DEFAULT_MAX_TASKS) -> None:
        self._lock = threading.RLock()
        self._tasks: Dict[str, TaskStatus] = {}
        self._texts: Dict[str, str] = {}
        self._meta: Dict[str, dict] = {}
        self._touched: Dict[str, float] = {}   # task_id → last access epoch (for LRU pruning)
        self._max_tasks = max_tasks

    # ── Task status ──────────────────────────────────────────────────────────
    def set(self, task_id: str, task: TaskStatus) -> None:
        with self._lock:
            self._tasks[task_id] = task
            self._touched[task_id] = time.time()
            self._evict_if_needed()

    def get(self, task_id: str) -> Optional[TaskStatus]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is not None:
                self._touched[task_id] = time.time()
            return task

    def update(self, task_id: str, **kwargs: Any) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            for key, value in kwargs.items():
                setattr(task, key, value)
            self._touched[task_id] = time.time()

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
            self._touched.pop(task_id, None)

    # ── Introspection ────────────────────────────────────────────────────────
    def active_task_ids(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._tasks.keys()))

    def size(self) -> int:
        with self._lock:
            return len(self._tasks)

    def snapshot(self) -> List[Dict[str, Any]]:
        """Read-only snapshot of every task's status — used by admin diagnostics."""
        with self._lock:
            now = time.time()
            return [
                {
                    "task_id": tid,
                    "status": getattr(t, "status", "unknown"),
                    "vocab_status": getattr(t, "vocab_status", None),
                    "progress": getattr(t, "progress", 0),
                    "exam_code": getattr(t, "exam_code", None),
                    "dict_code": getattr(t, "dict_code", None),
                    "user_id": self._meta.get(tid, {}).get("user_id"),
                    "idle_seconds": int(now - self._touched.get(tid, now)),
                }
                for tid, t in self._tasks.items()
            ]

    def prune_older_than(self, seconds: int) -> int:
        """Drop completed/error tasks idle for more than ``seconds``. Returns count removed.

        Tasks still in pending/processing are kept regardless of age — pruning a live job
        would orphan it and break vocab generation polling.
        """
        cutoff = time.time() - max(0, int(seconds))
        removed = 0
        with self._lock:
            for tid in list(self._tasks.keys()):
                task = self._tasks[tid]
                status = getattr(task, "status", "")
                if status in {"pending", "processing"}:
                    continue
                last = self._touched.get(tid, 0)
                if last <= cutoff:
                    self._tasks.pop(tid, None)
                    self._texts.pop(tid, None)
                    self._meta.pop(tid, None)
                    self._touched.pop(tid, None)
                    removed += 1
        return removed

    def _evict_if_needed(self) -> None:
        """LRU eviction when the size cap is exceeded. Caller must hold the lock."""
        if self._max_tasks <= 0 or len(self._tasks) <= self._max_tasks:
            return
        # Drop oldest finished tasks first; if all are live, fall back to absolute LRU.
        ordered = sorted(self._touched.items(), key=lambda kv: kv[1])
        excess = len(self._tasks) - self._max_tasks
        for tid, _ in ordered:
            if excess <= 0:
                break
            task = self._tasks.get(tid)
            if task is None:
                continue
            if getattr(task, "status", "") in {"pending", "processing"}:
                continue
            self._tasks.pop(tid, None)
            self._texts.pop(tid, None)
            self._meta.pop(tid, None)
            self._touched.pop(tid, None)
            excess -= 1
