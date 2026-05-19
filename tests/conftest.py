"""Shared pytest fixtures.

We isolate every test by pointing the DB and storage paths at a fresh
``tmp_path`` before importing application modules. Using ``pytest.fixture``
at function-scope keeps tests independent — no leftover users / library rows
across runs.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Spin up a fresh SQLite DB under tmp_path and reload backend modules so
    they bind to the new file.

    Tests that import any backend.* module should request this fixture so the
    in-process state from a previous test doesn't bleed in.
    """
    db_file = tmp_path / "test.db"
    upload_dir = tmp_path / "uploads"
    file_store_dir = tmp_path / "files"
    ocr_cache_dir = tmp_path / "ocr_cache"
    for d in (upload_dir, file_store_dir, ocr_cache_dir):
        d.mkdir()

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("UPLOAD_DIR", str(upload_dir))
    monkeypatch.setenv("FILE_STORE_DIR", str(file_store_dir))
    monkeypatch.setenv("OCR_CACHE_DIR", str(ocr_cache_dir))
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-pass-1234")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://localhost")

    # Drop any cached backend modules so the next import reads the new env.
    for name in list(sys.modules):
        if name.startswith("backend"):
            del sys.modules[name]

    # Reload config first so subsequent modules see the right paths.
    from backend import config, database  # noqa: F401
    importlib.reload(config)
    importlib.reload(database)
    database.init_db()
    yield database

    # Cleanup: drop cached modules so the next test gets a clean slate
    for name in list(sys.modules):
        if name.startswith("backend"):
            del sys.modules[name]
