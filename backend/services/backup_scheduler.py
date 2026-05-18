"""Scheduled automatic-backup service.

Runs in the background of the FastAPI app: every ``interval_hours`` (set by
the admin) it triggers ``migration_service.export_snapshot`` into
``data/migration_backups/`` and prunes older auto-backups based on the
retention policy. Configuration + last-run status are persisted in the
``app_settings`` table so the schedule survives restarts.

This module is intentionally light on dependencies — the only background
machinery it touches is :mod:`asyncio`. The scheduler tick is cheap (it just
compares timestamps); the heavy lifting happens inside
``migration_service.export_snapshot`` which runs in a worker thread.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from backend.database import AppSetting, SessionLocal

logger = logging.getLogger(__name__)


# ── Persistence keys ────────────────────────────────────────────────────────

_SCHEDULE_KEY = "backup_schedule"
_STATUS_KEY = "backup_schedule_status"

# Coarse tick. The scheduler simply checks "is it time yet?" — there's no
# benefit to checking every second. Sixty seconds is well below any sensible
# minimum interval (1h) while still giving snappy reaction to "run now".
TICK_SECONDS = 60.0


# ── Config / Status models ──────────────────────────────────────────────────

class BackupSchedule(BaseModel):
    """Admin-configurable schedule for automatic full-server backups."""
    enabled: bool = False
    interval_hours: int = Field(24, ge=1, le=720)        # 1h … 30 days
    retention_count: int = Field(7, ge=1, le=50)         # how many auto-backups to keep
    include_file_store: bool = True
    include_wordlists: bool = True
    include_ocr_cache: bool = False
    compression: str = "fast"                            # match migration presets
    notes_prefix: str = "Scheduled backup"


@dataclass
class BackupRunStatus:
    last_run_at: Optional[str] = None       # ISO-Z
    last_status: Optional[str] = None       # "success" | "error" | "skipped"
    last_error: Optional[str] = None
    last_filename: Optional[str] = None
    last_size_bytes: Optional[int] = None
    next_run_at: Optional[str] = None       # ISO-Z, derived from schedule
    runs_total: int = 0
    runs_failed: int = 0
    deleted_by_retention: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# ── In-memory state ─────────────────────────────────────────────────────────

# A single asyncio.Event we can set from outside the loop to force an immediate
# tick (used by "Run now"). The Event is created lazily inside the running
# event loop.
_wake_event: Optional[asyncio.Event] = None
_scheduler_task: Optional[asyncio.Task] = None
_run_lock = asyncio.Lock()


# ── Persistence helpers ─────────────────────────────────────────────────────

def _read_setting(key: str) -> dict[str, Any]:
    db = SessionLocal()
    try:
        rec = db.query(AppSetting).filter_by(key=key).first()
        if not rec or not rec.value_json:
            return {}
        try:
            return json.loads(rec.value_json)
        except (json.JSONDecodeError, TypeError):
            return {}
    finally:
        db.close()


def _write_setting(key: str, payload: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        rec = db.query(AppSetting).filter_by(key=key).first()
        body = json.dumps(payload, ensure_ascii=False)
        if rec is None:
            db.add(AppSetting(key=key, value_json=body))
        else:
            rec.value_json = body
        db.commit()
    finally:
        db.close()


def get_schedule() -> BackupSchedule:
    raw = _read_setting(_SCHEDULE_KEY)
    if not raw:
        return BackupSchedule()
    try:
        return BackupSchedule.model_validate(raw)
    except Exception:   # noqa: BLE001
        logger.warning("Stored backup schedule is invalid, returning defaults")
        return BackupSchedule()


def save_schedule(payload: dict[str, Any]) -> BackupSchedule:
    """Merge admin-supplied changes onto the current schedule and persist."""
    current = get_schedule().model_dump()
    current.update({k: v for k, v in payload.items() if v is not None})
    schedule = BackupSchedule.model_validate(current)
    _write_setting(_SCHEDULE_KEY, schedule.model_dump())
    # Recompute next_run_at and wake the scheduler so the new interval takes
    # effect immediately.
    status = get_status()
    status.next_run_at = _compute_next_run(schedule, status).strftime("%Y-%m-%dT%H:%M:%SZ") if schedule.enabled else None
    _save_status(status)
    _wake_scheduler()
    return schedule


def get_status() -> BackupRunStatus:
    raw = _read_setting(_STATUS_KEY)
    if not raw:
        return BackupRunStatus()
    status = BackupRunStatus()
    for k, v in raw.items():
        if hasattr(status, k):
            setattr(status, k, v)
    return status


def _save_status(status: BackupRunStatus) -> None:
    _write_setting(_STATUS_KEY, asdict(status))


# ── Schedule math ───────────────────────────────────────────────────────────

def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _compute_next_run(schedule: BackupSchedule, status: BackupRunStatus) -> datetime:
    """Next scheduled run.

    If we've never run before, schedule the first one ``interval_hours`` from
    now (so toggling the schedule on doesn't immediately fire — admins can
    explicitly press "Run now" if they want to test).
    """
    now = datetime.now(timezone.utc)
    last = _parse_iso(status.last_run_at)
    delta = timedelta(hours=schedule.interval_hours)
    if last is None:
        return now + delta
    candidate = last + delta
    # If the server was offline for a while, catch up by running immediately
    # instead of trying to run N missed backups back-to-back.
    return candidate if candidate > now else now


def _wake_scheduler() -> None:
    ev = _wake_event
    if ev is not None:
        try:
            ev.set()
        except RuntimeError:
            # Event loop already closed during shutdown
            pass


# ── Run-now & scheduled runs ────────────────────────────────────────────────

async def trigger_run_now(*, notes: str = "") -> BackupRunStatus:
    """Synchronously run a backup (awaited by the caller).

    Returns the updated status dataclass so callers can show the result
    immediately in the response.
    """
    async with _run_lock:
        schedule = get_schedule()
        return await asyncio.to_thread(_do_backup_now, schedule, notes or "Manual trigger")


def _do_backup_now(schedule: BackupSchedule, notes: str) -> BackupRunStatus:
    """The actual backup body — runs on a worker thread."""
    # Local import to avoid a circular module-load order at startup.
    from backend.services import migration_service

    status = get_status()
    status.runs_total += 1
    try:
        opts = migration_service.ExportOptions(
            include_file_store=schedule.include_file_store,
            include_wordlists=schedule.include_wordlists,
            include_ocr_cache=schedule.include_ocr_cache,
            compression=schedule.compression,
            notes=f"{schedule.notes_prefix}: {notes}".strip(": "),
        )
        out_path, _manifest = migration_service.export_to_backup_dir(
            prefix=migration_service.BACKUP_PREFIX_AUTO,
            options=opts,
        )
        size = out_path.stat().st_size
        status.last_status = "success"
        status.last_error = None
        status.last_filename = out_path.name
        status.last_size_bytes = size
        status.last_run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Retention: keep latest N auto-backups (pre-import & manual untouched).
        status.deleted_by_retention = migration_service.prune_backups(
            migration_service.BACKUP_PREFIX_AUTO, schedule.retention_count,
        )
        logger.info(
            "Auto-backup written: %s (%d bytes); pruned %d older",
            out_path.name, size, len(status.deleted_by_retention),
        )
    except Exception as exc:   # noqa: BLE001
        logger.exception("Auto-backup failed")
        status.runs_failed += 1
        status.last_status = "error"
        status.last_error = str(exc)
        status.last_run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    finally:
        status.next_run_at = (
            _compute_next_run(get_schedule(), status).strftime("%Y-%m-%dT%H:%M:%SZ")
            if schedule.enabled else None
        )
        _save_status(status)
    return status


async def _scheduler_loop(stop: asyncio.Event) -> None:
    """Main scheduler tick. Wakes on a timer or via ``_wake_event``."""
    global _wake_event
    _wake_event = asyncio.Event()
    logger.info("Backup scheduler started")

    # Make sure next_run_at is set on first boot.
    schedule = get_schedule()
    status = get_status()
    if schedule.enabled and not status.next_run_at:
        status.next_run_at = _compute_next_run(schedule, status).strftime("%Y-%m-%dT%H:%M:%SZ")
        _save_status(status)

    try:
        while not stop.is_set():
            try:
                await _scheduler_tick()
            except Exception:   # noqa: BLE001
                logger.exception("Scheduler tick crashed; continuing")

            # Sleep until next tick or external wake.
            try:
                await asyncio.wait_for(_wake_event.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
            else:
                _wake_event.clear()
    finally:
        _wake_event = None
        logger.info("Backup scheduler stopped")


async def _scheduler_tick() -> None:
    schedule = get_schedule()
    if not schedule.enabled:
        return
    status = get_status()
    next_at = _parse_iso(status.next_run_at) or _compute_next_run(schedule, status)
    if datetime.now(timezone.utc) < next_at:
        return

    async with _run_lock:
        # Re-read under the lock to avoid double-runs if another tick already
        # fired between the gate and the lock.
        status = get_status()
        next_at = _parse_iso(status.next_run_at) or _compute_next_run(schedule, status)
        if datetime.now(timezone.utc) < next_at:
            return
        logger.info("Scheduled backup due (next_run_at=%s)", status.next_run_at)
        await asyncio.to_thread(_do_backup_now, schedule, "scheduled")


# ── Lifecycle (called by FastAPI lifespan) ──────────────────────────────────

_stop_event: Optional[asyncio.Event] = None


def start_scheduler(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Start the scheduler task on the running event loop.

    Idempotent: a second call while one is already running is a no-op.
    """
    global _scheduler_task, _stop_event
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    _stop_event = asyncio.Event()
    loop = loop or asyncio.get_event_loop()
    _scheduler_task = loop.create_task(_scheduler_loop(_stop_event), name="backup-scheduler")


async def stop_scheduler() -> None:
    global _scheduler_task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    _wake_scheduler()
    task = _scheduler_task
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
    _scheduler_task = None
    _stop_event = None


# ── Convenience: combined view for the admin UI ─────────────────────────────

def serialize_schedule_view() -> dict[str, Any]:
    schedule = get_schedule()
    status = get_status()
    return {
        "schedule": schedule.model_dump(),
        "status": asdict(status),
        "tick_seconds": TICK_SECONDS,
        "now": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
