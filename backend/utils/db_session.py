"""
Helpers for using the SQLAlchemy session from async code.

SQLAlchemy's sync session is, well, sync — calling ``.query()`` from inside
an async endpoint blocks the event loop for the full duration of the SQL
round-trip. For request handlers FastAPI's ``Depends(get_db)`` handles
session lifecycle and the calls are short enough that blocking is fine.
Background tasks are different: they may run for many seconds and execute a
dozen DB statements, each one stalling every other request on the same loop.

This module gives those background paths two small helpers:

* :func:`run_in_session` — runs a synchronous callable on a worker thread
  with a fresh ``SessionLocal()`` instance, ensuring the session is closed
  even when the callable raises.
* :func:`async_session_scope` — an ``async with`` context manager that
  yields the same session-managed handle.

Neither helper changes the SQL semantics; they just push the blocking work
off the event loop. Existing endpoint handlers can keep using
``Depends(get_db)`` unchanged.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Awaitable, Callable, TypeVar

from sqlalchemy.orm import Session

from backend.database import SessionLocal

T = TypeVar("T")


async def run_in_session(fn: Callable[[Session], T]) -> T:
    """Run ``fn(session)`` on a worker thread, handling session lifecycle.

    Example::

        async def background_save(payload):
            await run_in_session(lambda s: _save_payload(s, payload))

    The callable receives a freshly-opened :class:`sqlalchemy.orm.Session`.
    It must complete its work (commit or rollback) before returning — the
    helper unconditionally closes the session afterwards.
    """
    def _wrapper() -> T:
        session = SessionLocal()
        try:
            return fn(session)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    return await asyncio.to_thread(_wrapper)


@contextlib.asynccontextmanager
async def async_session_scope():
    """Async context manager: yield a session, run inside a worker thread.

    Useful when the surrounding async code needs to interleave multiple
    awaits with DB work — keeps the session open between awaits without
    forcing the entire block onto a worker thread::

        async with async_session_scope() as session:
            result = await run_in_session(lambda s: s.query(...).first())

    For one-shot reads or writes :func:`run_in_session` is simpler.
    """
    loop = asyncio.get_running_loop()
    session: Session = await loop.run_in_executor(None, SessionLocal)
    try:
        yield session
    finally:
        await loop.run_in_executor(None, session.close)
