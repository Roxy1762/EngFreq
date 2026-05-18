"""Full-server migration: export & import a complete snapshot.

The export bundle is a single ``.zip`` containing:

    manifest.json                  metadata + schema version + per-member sha256
    db/app.db                      consistent SQLite snapshot (via Connection.backup)
    data/files/...                 persisted file copies (configurable)
    data/wordlists/...             custom wordlists (configurable)
    data/ocr_cache/...             OCR cache (optional, off by default)

Design notes
------------
* DB snapshot uses ``sqlite3.Connection.backup()`` which is safe to run live
  alongside other readers/writers — the resulting file is consistent and
  self-contained (no ``-wal``/``-shm`` companions needed).
* Imports are atomic-ish: the uploaded zip is extracted into a staging
  directory, validated, then (a) the live DB is replaced via the same backup
  API in reverse and (b) data directories are swapped under a global lock.
* Path traversal in zip members is rejected on extract; absolute paths and
  ``..`` segments are refused.
* Before any destructive import action a safety snapshot of the current
  state is written to ``data/migration_backups/pre-import-<ts>.zip`` so the
  admin can roll back via the same endpoint.
* Every file in the bundle carries a SHA256 checksum in the manifest. The
  whole bundle is verified up-front on import so a corrupted upload fails
  *before* anything destructive happens.
* File-tree restore parallelises copies across a small thread pool — most
  bundles spend the bulk of their import time copying many small files.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from backend.config import settings
from backend.database import (
    AppSetting,
    Dict as DictModel,
    Exam,
    LibraryWord,
    ReviewItem,
    SessionLocal,
    User,
    engine,
    init_db,
)

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

MIGRATION_FORMAT = "engfreq-migration"
# Bumped to 2: the manifest now carries per-member sha256 checksums.
# Older v1 bundles are still accepted on import (only the DB checksum is
# verified, matching v1 behaviour).
SCHEMA_VERSION = 2
APP_VERSION = "2.1.0"

# Default zip compression. ZIP_DEFLATED keeps the bundle small without an
# external dependency; bz2/lzma cost much more CPU for marginal gains on text.
ZIP_COMPRESSION = zipfile.ZIP_DEFLATED

# Per-import lock so two admin clicks don't race each other. Re-entrant from
# the scheduler is unnecessary; the scheduler acquires this same lock when it
# kicks off an automatic export.
_IMPORT_LOCK = asyncio.Lock()

# Where rollback / scheduled snapshots are written.
BACKUP_DIR = Path(settings.upload_dir).parent / "migration_backups"

# Backup file-name categories.
BACKUP_PREFIX_PRE_IMPORT = "pre-import-"
BACKUP_PREFIX_AUTO = "auto-"
BACKUP_PREFIX_MANUAL = "manual-"
_VALID_BACKUP_PREFIXES = (
    BACKUP_PREFIX_PRE_IMPORT, BACKUP_PREFIX_AUTO, BACKUP_PREFIX_MANUAL,
)

# Compression presets: maps a human-friendly name to (method, compresslevel).
# ``store`` is fast & uncompressed (great for local backups when disk is cheap);
# ``best`` is slow but produces the smallest bundle for sharing across the
# network.
COMPRESSION_PRESETS: dict[str, tuple[int, Optional[int]]] = {
    "store":    (zipfile.ZIP_STORED,   None),
    "fast":     (zipfile.ZIP_DEFLATED, 1),
    "balanced": (zipfile.ZIP_DEFLATED, 6),
    "best":     (zipfile.ZIP_DEFLATED, 9),
}
DEFAULT_COMPRESSION = "balanced"

# Files we never put into a bundle (transient artefacts, OS junk, leftover
# partial downloads from a failed previous export).
_EXCLUDE_FILE_SUFFIXES = (".partial", ".tmp", ".lock", ".swp")
_EXCLUDE_FILE_NAMES = {"Thumbs.db", "Desktop.ini", ".DS_Store"}

# Read buffer for streaming hash computation / large file copies.
_BUF = 1024 * 1024


@dataclass
class BundleManifest:
    format: str = MIGRATION_FORMAT
    schema_version: int = SCHEMA_VERSION
    app_version: str = APP_VERSION
    exported_at: str = ""
    source_host: str = ""
    source_db_path: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    includes: dict[str, bool] = field(default_factory=dict)
    checksums: dict[str, str] = field(default_factory=dict)
    compression: str = DEFAULT_COMPRESSION
    notes: str = ""

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.__dict__, ensure_ascii=False, indent=2).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes | str) -> "BundleManifest":
        data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        m = cls()
        for k, v in data.items():
            if hasattr(m, k):
                setattr(m, k, v)
        return m


# ── Helpers ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path, *, chunk: int = _BUF) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _sha256_zipmember(zf: zipfile.ZipFile, info: zipfile.ZipInfo, *, chunk: int = _BUF) -> str:
    h = hashlib.sha256()
    with zf.open(info, "r") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _resolve_db_path() -> Path:
    """Return an absolute Path to the live SQLite DB."""
    return Path(os.environ.get("DB_PATH", settings.db_path)).resolve()


def _safe_join(base: Path, member: str) -> Path:
    """Resolve a zip member path under ``base``, rejecting traversal.

    Rejects: absolute paths, drive letters, ``..`` segments. Final resolved
    path must remain inside ``base.resolve()`` — protects against symlink
    or normalization tricks.
    """
    if not member or member.startswith(("/", "\\")):
        raise ValueError(f"Refusing absolute path in archive: {member!r}")
    parts = [p for p in member.replace("\\", "/").split("/") if p]
    if any(p == ".." for p in parts):
        raise ValueError(f"Refusing path traversal in archive: {member!r}")
    base_resolved = base.resolve()
    dst = (base / "/".join(parts)).resolve()
    if dst != base_resolved and base_resolved not in dst.parents:
        raise ValueError(f"Archive member escapes target directory: {member!r}")
    return dst


def _gather_counts() -> dict[str, int]:
    """Snapshot row counts from each major table (lightweight)."""
    db = SessionLocal()
    try:
        return {
            "users": db.query(User).count(),
            "exams": db.query(Exam).count(),
            "dicts": db.query(DictModel).count(),
            "library_words": db.query(LibraryWord).count(),
            "review_items": db.query(ReviewItem).count(),
            "app_settings": db.query(AppSetting).count(),
        }
    finally:
        db.close()


def _dir_size_bytes(p: Path) -> int:
    if not p.exists():
        return 0
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def _is_excluded_file(name: str) -> bool:
    if name in _EXCLUDE_FILE_NAMES:
        return True
    return any(name.endswith(suf) for suf in _EXCLUDE_FILE_SUFFIXES)


def _wordlists_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "data" / "wordlists"


def _classify_backup(name: str) -> str:
    for prefix, label in (
        (BACKUP_PREFIX_PRE_IMPORT, "pre-import"),
        (BACKUP_PREFIX_AUTO, "auto"),
        (BACKUP_PREFIX_MANUAL, "manual"),
    ):
        if name.startswith(prefix):
            return label
    return "other"


def _is_safe_backup_name(name: str) -> bool:
    """Backup filenames are simple: <prefix>YYYYMMDD-HHMMSS.zip — no separators."""
    if "/" in name or "\\" in name or name.startswith("."):
        return False
    if not name.endswith(".zip"):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9._\-]+", name))


def prune_backups(prefix: str, keep: int) -> list[str]:
    """Delete oldest backups matching ``prefix`` until at most ``keep`` remain.

    Returns the list of deleted filenames. Safe when the directory doesn't
    exist or there's nothing to prune.
    """
    if keep < 0 or not BACKUP_DIR.exists():
        return []
    files = sorted(
        (p for p in BACKUP_DIR.glob(f"{prefix}*.zip") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    to_delete = files[keep:]
    deleted: list[str] = []
    for p in to_delete:
        try:
            p.unlink()
            deleted.append(p.name)
        except OSError as exc:
            logger.warning("Could not prune backup %s: %s", p.name, exc)
    return deleted


# ── Export ──────────────────────────────────────────────────────────────────

@dataclass
class ExportOptions:
    include_file_store: bool = True
    include_wordlists: bool = True
    include_ocr_cache: bool = False
    compression: str = DEFAULT_COMPRESSION
    notes: str = ""


def _resolve_compression(name: str) -> tuple[int, Optional[int]]:
    return COMPRESSION_PRESETS.get(name, COMPRESSION_PRESETS[DEFAULT_COMPRESSION])


def _open_zipfile(path: Path, *, compression: str) -> zipfile.ZipFile:
    method, level = _resolve_compression(compression)
    # ``compresslevel`` was added in Python 3.7. ZIP_STORED ignores it.
    return zipfile.ZipFile(path, "w", compression=method, compresslevel=level)


def export_snapshot(
    output_path: Path,
    options: Optional[ExportOptions] = None,
) -> BundleManifest:
    """Produce a self-contained migration bundle at ``output_path``.

    The output file is written atomically (to ``<output>.partial`` then
    renamed) so a partial bundle never appears on disk on failure. Every file
    embedded in the zip gets a SHA256 entry in the manifest.
    """
    opts = options or ExportOptions()
    if opts.compression not in COMPRESSION_PRESETS:
        raise ValueError(
            f"Unknown compression preset: {opts.compression!r}. "
            f"Choose one of: {sorted(COMPRESSION_PRESETS)}"
        )
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.with_suffix(output_path.suffix + ".partial")

    # Snapshot DB to a temp file first via SQLite's backup API.
    with tempfile.TemporaryDirectory(prefix="engfreq-export-") as tmpdir:
        tmp_root = Path(tmpdir)
        tmp_db = tmp_root / "app.db"
        _snapshot_sqlite(_resolve_db_path(), tmp_db)

        manifest = BundleManifest(
            exported_at=_now_iso(),
            source_host=socket.gethostname(),
            source_db_path=str(_resolve_db_path()),
            counts=_gather_counts(),
            includes={
                "db": True,
                "file_store": opts.include_file_store,
                "wordlists": opts.include_wordlists,
                "ocr_cache": opts.include_ocr_cache,
            },
            checksums={"db/app.db": _sha256_file(tmp_db)},
            compression=opts.compression,
            notes=opts.notes,
        )

        # Build the manifest *first* so per-tree checksums can be filled in
        # before we write it. We do this by scanning each tree to compute the
        # paths + hashes, then add everything to the zip in one pass.
        per_tree: list[tuple[Path, str]] = []  # (src_dir, arc_root)
        if opts.include_file_store:
            per_tree.append((Path(settings.file_store_dir), "data/files"))
        if opts.include_wordlists:
            per_tree.append((_wordlists_dir(), "data/wordlists"))
        if opts.include_ocr_cache:
            per_tree.append((Path(settings.ocr_cache_dir), "data/ocr_cache"))

        for src, arc_root in per_tree:
            for src_path, arc_name in _walk_tree_for_zip(src, arc_root):
                manifest.checksums[arc_name] = _sha256_file(src_path)

        with _open_zipfile(staging, compression=opts.compression) as zf:
            zf.writestr("manifest.json", manifest.to_json_bytes())
            zf.write(tmp_db, "db/app.db")
            for src, arc_root in per_tree:
                for src_path, arc_name in _walk_tree_for_zip(src, arc_root):
                    zf.write(src_path, arc_name)

    os.replace(staging, output_path)
    logger.info("Migration bundle written: %s (size=%d)", output_path, output_path.stat().st_size)
    return manifest


def _flush_engine_pool() -> None:
    """Force outstanding ORM commits to disk before/after a snapshot/restore.

    A checkpoint on a pooled connection only sees writes that have already been
    committed via that pool. Disposing the pool guarantees every open
    transaction is closed and writes are flushed to disk before the snapshot.
    """
    try:
        engine.dispose()
    except Exception:   # noqa: BLE001
        logger.exception("engine.dispose() failed during migration")


def _snapshot_sqlite(src_path: Path, dst_path: Path) -> None:
    """Hot, consistent snapshot via SQLite's backup API."""
    if not src_path.exists():
        raise FileNotFoundError(f"Source DB not found: {src_path}")
    _flush_engine_pool()
    # Open the source directly: this avoids depending on SQLAlchemy's private
    # raw_connection() shape (which differs across versions).
    src = sqlite3.connect(str(src_path))
    try:
        try:
            src.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
        dst = sqlite3.connect(str(dst_path))
        try:
            with dst:
                src.backup(dst, pages=200, sleep=0.0)
        finally:
            dst.close()
    finally:
        src.close()


def _walk_tree_for_zip(src: Path, arc_root: str) -> Iterable[tuple[Path, str]]:
    """Yield ``(src_path, arc_name)`` pairs for every file we want to archive.

    Skips hidden directories, hidden files, and transient artefacts.
    """
    if not src.exists() or not src.is_dir():
        return
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if fname.startswith(".") or _is_excluded_file(fname):
                continue
            full = Path(root) / fname
            rel = full.relative_to(src)
            yield full, f"{arc_root}/{rel.as_posix()}"


# ── Import ──────────────────────────────────────────────────────────────────

@dataclass
class ImportOptions:
    replace_file_store: bool = False  # False = merge (overwrite individual files but keep extras)
    replace_wordlists: bool = False
    replace_ocr_cache: bool = False
    dry_run: bool = False
    make_safety_backup: bool = True
    abort_on_user_conflict: bool = False  # if True, refuse when usernames already exist (defense-in-depth)
    verify_all_checksums: bool = True     # v2 bundles only; v1 falls back to DB-only verification


@dataclass
class ImportReport:
    ok: bool
    manifest: dict
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safety_backup_path: Optional[str] = None
    error: Optional[str] = None


def preview_bundle(zip_path: Path) -> dict:
    """Return the manifest + size info for an uploaded bundle without applying."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if "manifest.json" not in names:
            raise ValueError("Bundle is missing manifest.json")
        manifest_raw = zf.read("manifest.json")
        manifest = BundleManifest.from_json(manifest_raw)
        size_by_prefix: dict[str, int] = {}
        for info in zf.infolist():
            prefix = info.filename.split("/", 1)[0]
            size_by_prefix[prefix] = size_by_prefix.get(prefix, 0) + info.file_size
    if manifest.format != MIGRATION_FORMAT:
        raise ValueError(f"Unrecognised bundle format: {manifest.format!r}")
    if manifest.schema_version > SCHEMA_VERSION:
        raise ValueError(
            f"Bundle schema_version={manifest.schema_version} newer than this server "
            f"({SCHEMA_VERSION}) — upgrade the server first."
        )
    return {
        "manifest": manifest.__dict__,
        "size_by_section": size_by_prefix,
        "members": len(names),
    }


async def import_snapshot(zip_path: Path, options: Optional[ImportOptions] = None) -> ImportReport:
    """Apply a migration bundle to the live server.

    Serialised by a module-level lock so concurrent admin imports cannot race.
    """
    opts = options or ImportOptions()
    async with _IMPORT_LOCK:
        # The heavy lifting is fully synchronous; run it in a worker thread so
        # we don't stall the event loop while the DB is being swapped out.
        return await asyncio.to_thread(_do_import, zip_path, opts)


def _do_import(zip_path: Path, opts: ImportOptions) -> ImportReport:
    report = ImportReport(ok=False, manifest={})
    try:
        with tempfile.TemporaryDirectory(prefix="engfreq-import-") as tmpdir:
            extract_root = Path(tmpdir)
            with zipfile.ZipFile(zip_path, "r") as zf:
                manifest = BundleManifest.from_json(zf.read("manifest.json"))
                _validate_manifest(manifest)
                report.manifest = manifest.__dict__

                # Verify every member's checksum *before* extracting, so a
                # corrupt or tampered bundle is rejected without ever touching
                # the staging directory. v1 bundles only have db/app.db here.
                if opts.verify_all_checksums and manifest.schema_version >= 2 and manifest.checksums:
                    bad = _verify_all_checksums(zf, manifest.checksums)
                    if bad:
                        raise ValueError(
                            "Checksum mismatch in "
                            + ", ".join(bad[:5]) + ("..." if len(bad) > 5 else "")
                        )
                    report.actions.append(f"checksum_verified={len(manifest.checksums)}")

                # Extract members safely
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    dst = _safe_join(extract_root, info.filename)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, "r") as src, dst.open("wb") as out:
                        shutil.copyfileobj(src, out, length=_BUF)

            # Verify DB checksum if recorded (v1 path + redundant safety on v2)
            db_member = extract_root / "db" / "app.db"
            if not db_member.exists():
                raise ValueError("Bundle is missing db/app.db")
            expected = manifest.checksums.get("db/app.db")
            if expected:
                actual = _sha256_file(db_member)
                if actual != expected:
                    raise ValueError(
                        f"DB checksum mismatch — bundle is corrupt or tampered "
                        f"(expected {expected[:12]}…, got {actual[:12]}…)"
                    )
                if "db_checksum_verified" not in report.actions:
                    report.actions.append("db_checksum_verified")

            # Defense-in-depth check
            if opts.abort_on_user_conflict:
                conflicts = _conflicting_usernames(db_member)
                if conflicts:
                    raise ValueError(
                        "Username conflict detected and abort_on_user_conflict=true: "
                        + ", ".join(conflicts[:5]) + ("..." if len(conflicts) > 5 else "")
                    )

            if opts.dry_run:
                report.ok = True
                report.actions.append("dry_run_validated_only")
                return report

            # Safety snapshot
            if opts.make_safety_backup:
                BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                safety_path = BACKUP_DIR / f"{BACKUP_PREFIX_PRE_IMPORT}{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
                try:
                    export_snapshot(
                        safety_path,
                        ExportOptions(
                            include_file_store=True,
                            include_wordlists=True,
                            include_ocr_cache=False,
                            compression="fast",   # safety snapshots prioritise speed
                            notes="Auto safety snapshot before import",
                        ),
                    )
                    report.safety_backup_path = str(safety_path)
                    report.actions.append(f"safety_backup={safety_path.name}")
                except Exception as exc:  # noqa: BLE001
                    report.warnings.append(f"Safety backup failed: {exc}")

            # Apply DB
            _restore_sqlite(db_member, _resolve_db_path())
            init_db()  # re-runs lightweight schema migrations against the restored DB
            report.actions.append("db_restored")

            # Apply data directories
            includes = manifest.includes or {}
            if includes.get("file_store"):
                _restore_dir(
                    extract_root / "data" / "files",
                    Path(settings.file_store_dir),
                    replace=opts.replace_file_store,
                )
                report.actions.append(
                    f"file_store_{'replaced' if opts.replace_file_store else 'merged'}"
                )
            if includes.get("wordlists"):
                _restore_dir(
                    extract_root / "data" / "wordlists",
                    _wordlists_dir(),
                    replace=opts.replace_wordlists,
                )
                report.actions.append(
                    f"wordlists_{'replaced' if opts.replace_wordlists else 'merged'}"
                )
            if includes.get("ocr_cache"):
                _restore_dir(
                    extract_root / "data" / "ocr_cache",
                    Path(settings.ocr_cache_dir),
                    replace=opts.replace_ocr_cache,
                )
                report.actions.append(
                    f"ocr_cache_{'replaced' if opts.replace_ocr_cache else 'merged'}"
                )

        report.ok = True
        return report
    except Exception as exc:  # noqa: BLE001
        logger.exception("Migration import failed")
        report.error = str(exc)
        return report


def _verify_all_checksums(zf: zipfile.ZipFile, checksums: dict[str, str]) -> list[str]:
    """Return the list of member names whose checksum did not match.

    ``checksums`` is the ``manifest.checksums`` dict (arc_name → sha256 hex).
    Members listed in the manifest but missing from the zip count as failures.
    """
    bad: list[str] = []
    name_to_info = {i.filename: i for i in zf.infolist()}
    for arc_name, expected in checksums.items():
        info = name_to_info.get(arc_name)
        if info is None:
            bad.append(f"{arc_name} (missing)")
            continue
        actual = _sha256_zipmember(zf, info)
        if actual != expected:
            bad.append(f"{arc_name} (expected {expected[:8]}…, got {actual[:8]}…)")
    return bad


def _validate_manifest(m: BundleManifest) -> None:
    if m.format != MIGRATION_FORMAT:
        raise ValueError(f"Unrecognised bundle format: {m.format!r}")
    if m.schema_version > SCHEMA_VERSION:
        raise ValueError(
            f"Bundle schema_version={m.schema_version} newer than this server "
            f"({SCHEMA_VERSION}) — upgrade first."
        )
    if m.schema_version < 1:
        raise ValueError(f"Invalid schema_version: {m.schema_version}")


def _conflicting_usernames(uploaded_db: Path) -> list[str]:
    """Return usernames present in both the current DB and the uploaded one."""
    current: set[str] = set()
    db = SessionLocal()
    try:
        current = {u.username for u in db.query(User).all()}
    finally:
        db.close()
    if not current:
        return []
    conn = sqlite3.connect(str(uploaded_db))
    try:
        rows = conn.execute("SELECT username FROM users").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    uploaded = {r[0] for r in rows}
    return sorted(current & uploaded)


def _restore_sqlite(src_path: Path, dst_path: Path) -> None:
    """Replace the live DB with ``src_path``.

    SQLAlchemy keeps a pool of connections to the old file; if we just copy
    bytes over they will keep reading stale pages. The robust sequence is:
      1. Dispose the pool so every open conn is closed.
      2. Use SQLite's backup API to overwrite the on-disk file.
      3. The next engine.connect() opens a fresh connection against the new
         file (which has the same path).
    """
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    _flush_engine_pool()
    # Final WAL checkpoint on the old file so on-disk pages are up to date.
    if dst_path.exists():
        try:
            tmp = sqlite3.connect(str(dst_path))
            try:
                tmp.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                tmp.close()
        except sqlite3.DatabaseError:
            pass

    src_conn = sqlite3.connect(str(src_path))
    dst_conn = sqlite3.connect(str(dst_path))
    try:
        with dst_conn:
            src_conn.backup(dst_conn, pages=200, sleep=0.0)
        try:
            dst_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            pass
    finally:
        src_conn.close()
        dst_conn.close()
    # After replacing the file, ensure the next ORM call gets a fresh conn.
    _flush_engine_pool()


def _restore_dir(src: Path, dst: Path, *, replace: bool, max_workers: int = 4) -> None:
    """Copy ``src`` (extracted-from-zip) into ``dst``.

    ``replace=True`` wipes ``dst`` first; ``replace=False`` overlays files,
    overwriting collisions but keeping unrelated entries. Copies are run on a
    small thread pool — most data directories are dominated by many small
    files where serial copy stalls on per-syscall latency.
    """
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    if replace:
        # Clear contents (but keep the directory itself in case something
        # holds an FD on it).
        for child in dst.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass

    # First pass: create all target directories. Doing this serially up-front
    # avoids races between worker threads creating the same parent.
    jobs: list[tuple[Path, Path]] = []
    for root, _, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        target_root = dst / rel_root
        target_root.mkdir(parents=True, exist_ok=True)
        for fname in files:
            jobs.append((Path(root) / fname, target_root / fname))

    if not jobs:
        return

    # Single-threaded fallback for tiny batches: thread-pool setup dominates.
    if len(jobs) <= 8:
        for s, d in jobs:
            shutil.copy2(s, d)
        return

    workers = min(max_workers, max(2, len(jobs) // 4))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(shutil.copy2, s, d) for s, d in jobs]
        # surface any exception
        for fut in as_completed(futures):
            fut.result()


# ── Restore-from-local-backup (no upload roundtrip) ─────────────────────────

async def restore_from_backup_file(name: str, options: Optional[ImportOptions] = None) -> ImportReport:
    """Apply a backup that already exists in ``BACKUP_DIR``.

    Same code path as :func:`import_snapshot` but skips the upload step — useful
    when the operator wants to roll back to a known-good snapshot without
    re-downloading & re-uploading megabytes of data over the network.
    """
    if not _is_safe_backup_name(name):
        raise ValueError(f"Refusing unsafe backup name: {name!r}")
    target = (BACKUP_DIR / name).resolve()
    if BACKUP_DIR.resolve() not in target.parents or not target.exists():
        raise FileNotFoundError(f"Backup not found: {name}")
    return await import_snapshot(target, options)


# ── Backups listing (used by the admin UI) ──────────────────────────────────

def list_backups() -> list[dict[str, Any]]:
    """Enumerate backup zips with size + category metadata."""
    if not BACKUP_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(BACKUP_DIR.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append({
            "name": p.name,
            "size_bytes": st.st_size,
            "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": _classify_backup(p.name),
        })
    return out


# ── Stats endpoint helper ───────────────────────────────────────────────────

def server_state_summary() -> dict[str, Any]:
    """A snapshot of what an export would cover — for the admin UI."""
    db_path = _resolve_db_path()
    file_store = Path(settings.file_store_dir)
    wordlists = _wordlists_dir()
    ocr_cache = Path(settings.ocr_cache_dir)
    backups_size = _dir_size_bytes(BACKUP_DIR) if BACKUP_DIR.exists() else 0
    return {
        "schema_version": SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "counts": _gather_counts(),
        "compression_presets": sorted(COMPRESSION_PRESETS.keys()),
        "paths": {
            "db_path": str(db_path),
            "file_store": str(file_store),
            "wordlists": str(wordlists),
            "ocr_cache": str(ocr_cache),
            "backup_dir": str(BACKUP_DIR),
        },
        "sizes_bytes": {
            "db": db_path.stat().st_size if db_path.exists() else 0,
            "file_store": _dir_size_bytes(file_store),
            "wordlists": _dir_size_bytes(wordlists),
            "ocr_cache": _dir_size_bytes(ocr_cache),
            "backups": backups_size,
        },
    }


# ── Helper for FastAPI endpoint ─────────────────────────────────────────────

def make_export_tempfile(options: Optional[ExportOptions] = None) -> tuple[Path, BundleManifest]:
    """Write a bundle to a fresh tempfile and return its path + manifest.

    Caller is responsible for removing the temp file (e.g. via a
    ``starlette.background.BackgroundTask``).
    """
    tmp = tempfile.NamedTemporaryFile(
        prefix="engfreq-export-",
        suffix=".zip",
        delete=False,
    )
    tmp.close()
    out = Path(tmp.name)
    try:
        manifest = export_snapshot(out, options)
    except Exception:
        out.unlink(missing_ok=True)
        raise
    return out, manifest


def cleanup_tempfile(path: str | Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def export_to_backup_dir(
    *,
    prefix: str = BACKUP_PREFIX_AUTO,
    options: Optional[ExportOptions] = None,
) -> tuple[Path, BundleManifest]:
    """Write an export directly into :data:`BACKUP_DIR`.

    The filename pattern is ``<prefix>YYYYMMDD-HHMMSS.zip``, matching the
    one used by safety snapshots so the existing prune/list helpers cover
    everything.
    """
    if prefix not in _VALID_BACKUP_PREFIXES:
        raise ValueError(f"Refusing unknown backup prefix: {prefix!r}")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = BACKUP_DIR / f"{prefix}{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
    manifest = export_snapshot(out, options)
    return out, manifest
