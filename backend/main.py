"""
FastAPI application entry point.

Auth endpoints:
  POST /auth/register         Register a new user (username + password)
  POST /auth/login            Login → JWT token
  GET  /auth/me               Current user info

Analysis (requires login):
  POST /api/analyze           Upload file + config → task_id, saves Exam to DB
  GET  /api/tasks/{id}        Poll task status / result (also returns exam_code/dict_code)
  POST /api/tasks/{id}/vocab  Generate vocabulary, saves Dict to DB
  GET  /api/tasks/{id}/export/{fmt}  Download CSV or XLSX

User data:
  GET  /api/codes             List current user's exam codes + dict codes
  GET  /api/providers         Available vocab providers

Public share (no auth needed):
  GET  /api/share/exam/{code} View full analysis result by exam code
  GET  /api/share/dict/{code} View vocabulary by dict code

Admin (requires admin account):
  GET  /admin/users                        List all users
  POST /admin/users/{id}/reset-password    Reset a user's password
  GET  /admin/codes                        List all exam/dict codes with owner info

Frontend:
  GET  /                      Serve frontend SPA
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.auth import (
    create_token,
    decode_token,
    generate_code,
    hash_password,
    verify_password,
)
from backend.config import settings
from backend.database import Dict as DictModel
from backend.database import Exam, SessionLocal, User, get_db, init_db
from backend.models.schemas import (
    AISelectRequest,
    AnalysisResult,
    AnalyzeRequest,
    ChangePasswordRequest,
    FilterConfig,
    LemmaEntry,
    MultiExamRequest,
    TaskStatus,
    UserProfileUpdate,
    VocabEntry,
    VocabSelectionRequest,
    WeightConfig,
)
from backend.services.export_service import to_csv, to_xlsx
from backend.services.file_handler import extract_text
from backend.services.frequency_analyzer import analyse
from backend.services.runtime_config import frontend_config_payload, get_runtime_config, save_runtime_config
from backend.services.structure_recognizer import recognize_structure
from backend.services.vocabulary_generator import available_providers, generate_vocabulary
from backend.utils.datetime_compat import iso_z
from backend.utils.rate_limit import SlidingWindowLimiter
from backend.utils.security import SecurityHeadersMiddleware, client_identifier, sanitize_filename
from backend.utils.task_store import TaskStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
logger = logging.getLogger(__name__)

# ── Tunables (previously magic numbers scattered through the file) ──────────
OCR_TEST_PREVIEW_CHARS = 2000
COMBINE_EXAMS_FILENAME_CAP = 3
EXPORT_FILENAME_TASK_SLICE = 8
MAX_UNIQUE_CODE_ATTEMPTS = 20

# Admin list endpoints default/upper bounds for pagination.
ADMIN_LIST_DEFAULT_LIMIT = 100
ADMIN_LIST_MAX_LIMIT = 500

# ── Bootstrap DB + default admin ─────────────────────────────────────────────
init_db()

_ADMIN_USER = os.environ.get("ADMIN_USERNAME", "admin")
_ADMIN_PASS = os.environ.get("ADMIN_PASSWORD", "admin123")
_ADMIN_PASS_IS_DEFAULT = (
    "ADMIN_PASSWORD" not in os.environ or _ADMIN_PASS == "admin123"
)


def _ensure_admin() -> None:
    db = SessionLocal()
    try:
        if not db.query(User).filter_by(username=_ADMIN_USER).first():
            db.add(User(username=_ADMIN_USER, password_hash=hash_password(_ADMIN_PASS), is_admin=True))
            db.commit()
            if _ADMIN_PASS_IS_DEFAULT:
                logger.warning(
                    "Default admin '%s' created with the built-in fallback password. "
                    "Set ADMIN_PASSWORD in the environment and restart — never run this way in production.",
                    _ADMIN_USER,
                )
            else:
                logger.info("Admin account '%s' created from ADMIN_PASSWORD env var.", _ADMIN_USER)
        elif _ADMIN_PASS_IS_DEFAULT:
            logger.warning(
                "ADMIN_PASSWORD is unset or still the built-in default. "
                "Anyone who can reach this service can log in — rotate via the admin panel."
            )
    finally:
        db.close()


_ensure_admin()

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="English Exam Word Analyzer", version="2.0.0")

_cors_origins = settings.cors_origins_list
_cors_allow_credentials = _cors_origins != ["*"]
if _cors_origins == ["*"]:
    logger.warning(
        "CORS is configured with the wildcard origin. Set CORS_ALLOW_ORIGINS to a "
        "comma-separated list of domains before exposing the service publicly."
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=_cors_allow_credentials,
)
if settings.security_headers_enabled:
    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=settings.hsts_enabled,
    )

# ── Rate limiting (auth endpoints) ────────────────────────────────────────────
_auth_limiter = SlidingWindowLimiter(
    max_hits=settings.auth_rate_limit_attempts,
    window_seconds=settings.auth_rate_limit_window_seconds,
)


def _enforce_rate_limit(request: Request, bucket: str) -> None:
    key = f"{bucket}:{client_identifier(request)}"
    if not _auth_limiter.check(key):
        retry = int(_auth_limiter.retry_after(key)) + 1
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后再试",
            headers={"Retry-After": str(retry)},
        )


_ASSETS_DIR = Path(__file__).parent.parent / "frontend" / "assets"
_ASSETS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(_ASSETS_DIR)), name="assets")


@app.on_event("shutdown")
async def _graceful_shutdown() -> None:
    """Best-effort cleanup on process exit: drop in-memory task state and
    cancel any lingering asyncio tasks so background workers don't block
    SIGTERM handling. Disk-backed resources (uploads, OCR cache) survive
    restarts by design.
    """
    size = _task_store.size()
    if size:
        logger.info("Shutdown: clearing %d in-flight task(s) from memory", size)
    for task_id in list(_task_store.active_task_ids()):
        _task_store.purge(task_id)

    pending = [t for t in asyncio.all_tasks() if not t.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@app.get("/healthz", tags=["health"])
async def healthz() -> dict:
    """Lightweight liveness probe — no DB access, no external calls."""
    return {"status": "ok", "version": app.version}


@app.get("/readyz", tags=["health"])
async def readyz() -> dict:
    """Readiness probe: verifies DB, runtime config, and the configured vocab provider.

    The provider check is a soft signal — if the configured LLM provider lacks
    an API key the probe downgrades to ``degraded`` rather than failing, so
    the deployment stays serveable for users who only use dictionary lookups.
    """
    from sqlalchemy import text as sql_text
    try:
        with SessionLocal() as session:
            session.execute(sql_text("SELECT 1"))
        runtime = get_runtime_config()
    except Exception as exc:   # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"not ready: {exc}") from exc

    providers = set(available_providers())
    default_provider = runtime.vocab_provider
    llm_cfg = runtime.llm
    provider_keys = {
        "claude": bool(llm_cfg.anthropic_api_key),
        "deepseek": bool(llm_cfg.deepseek_api_key),
        "openai": bool(llm_cfg.openai_api_key),
        "merriam_webster": bool(settings.merriam_webster_key),
        "youdao": bool(settings.youdao_app_key),
    }

    status = "ready"
    notes: list[str] = []
    if default_provider not in providers:
        status = "degraded"
        notes.append(f"default provider '{default_provider}' is not registered")
    elif default_provider in provider_keys and not provider_keys[default_provider]:
        status = "degraded"
        notes.append(f"default provider '{default_provider}' is missing its API key")

    return {
        "status": status,
        "providers": sorted(providers),
        "default_provider": default_provider,
        "provider_keys": provider_keys,
        "notes": notes,
    }


# ── In-memory task store ──────────────────────────────────────────────────────
# Wrapped in a TaskStore for thread-safety; module-level helpers below keep the
# original call-sites readable without re-plumbing the whole file.
_task_store = TaskStore()


def _update_task(task_id: str, **kwargs: Any) -> None:
    _task_store.update(task_id, **kwargs)


def _purge_task_cache(task_id: str) -> None:
    _task_store.purge(task_id)


def _load_analysis_result(raw: str) -> AnalysisResult:
    return AnalysisResult.model_validate_json(raw)


def _persist_exam_result(db: Session, exam: Exam, result: AnalysisResult) -> None:
    exam.result_json = result.model_dump_json()
    db.add(exam)
    db.commit()
    db.refresh(exam)


def _persist_exam_parse_meta(
    db: Session,
    exam: Exam,
    *,
    backend: str,
    raw_result: dict | None,
) -> None:
    exam.raw_parse_backend = backend
    exam.raw_parse_result_json = (
        json.dumps(raw_result, ensure_ascii=False)
        if raw_result is not None
        else None
    )
    db.add(exam)
    db.commit()
    db.refresh(exam)


def _upsert_dict_record(
    db: Session,
    *,
    user_id: int,
    exam: Exam,
    task_id: Optional[str],
    filename: str,
    vocab: list,
) -> DictModel:
    record = None
    if task_id:
        record = db.query(DictModel).filter_by(task_id=task_id).first()
    if record is None:
        record = db.query(DictModel).filter_by(exam_id=exam.id).order_by(DictModel.created_at.desc()).first()

    payload = json.dumps([v.model_dump() for v in vocab])
    if record is None:
        record = DictModel(
            user_id=user_id,
            exam_id=exam.id,
            task_id=task_id,
            filename=filename,
            dict_code=_unique_dict_code(db),
            vocab_json=payload,
        )
        db.add(record)
    else:
        record.user_id = user_id
        record.exam_id = exam.id
        record.task_id = task_id
        record.filename = filename
        record.vocab_json = payload

    db.commit()
    db.refresh(record)
    return record


def _get_exam_for_user_or_404(db: Session, exam_code: str, user: User) -> Exam:
    exam = db.query(Exam).filter_by(exam_code=exam_code.upper()).first()
    if not exam:
        raise HTTPException(404, "试卷不存在")
    if not user.is_admin and exam.user_id != user.id:
        raise HTTPException(403, "没有权限操作这份试卷")
    return exam


def _get_dict_for_user_or_404(db: Session, dict_code: str, user: User) -> DictModel:
    record = db.query(DictModel).filter_by(dict_code=dict_code.upper()).first()
    if not record:
        raise HTTPException(404, "词汇表不存在")
    if not user.is_admin and record.user_id != user.id:
        raise HTTPException(403, "没有权限操作这份词汇表")
    return record


def _sync_task_result(task_id: str, result: AnalysisResult, dict_code: Optional[str] = None) -> None:
    task = _task_store.get(task_id)
    if task:
        task.result = result
        if dict_code:
            task.dict_code = dict_code


def _admin_overview_payload(db: Session) -> dict:
    users = db.query(User).all()
    exams = db.query(Exam).all()
    dicts = db.query(DictModel).all()
    config = get_runtime_config(db)
    return {
        "users": len(users),
        "admins": sum(1 for user in users if user.is_admin),
        "exams": len(exams),
        "dicts": len(dicts),
        "raw_parse_count": sum(1 for exam in exams if exam.raw_parse_result_json),
        "providers": available_providers(),
        "runtime_config": config.model_dump(),
        "ocr_capabilities": frontend_config_payload()["ocr_capabilities"],
        "parse_backends": frontend_config_payload()["parse_backends"],
    }


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _bearer_payload(request: Request) -> Optional[dict]:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return decode_token(auth[7:])
    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    payload = _bearer_payload(request)
    if not payload:
        raise HTTPException(401, "未登录或 token 已过期，请先登录")
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if not user:
        raise HTTPException(401, "用户不存在")
    return user


def get_admin_user(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "需要管理员权限")
    return user


def _user_owns_task(task_id: str, user: User, db: Session) -> bool:
    """Return True if `user` may read/manipulate the task. Admins always pass.

    Ownership is resolved in two layers — the in-memory task store (set when
    the upload starts) and the persisted Exam row (set after analysis
    completes). We accept either signal so that polling works during the
    short window before the Exam row is written.
    """
    if user.is_admin:
        return True
    meta = _task_store.get_meta(task_id)
    owner = meta.get("user_id")
    if owner is not None:
        return owner == user.id
    exam = db.query(Exam).filter_by(task_id=task_id).first()
    if exam is None:
        return False
    return exam.user_id == user.id


# ── Code generation helpers ───────────────────────────────────────────────────

def _unique_exam_code(db: Session) -> str:
    for _ in range(MAX_UNIQUE_CODE_ATTEMPTS):
        code = generate_code(8)
        if not db.query(Exam).filter_by(exam_code=code).first():
            return code
    raise RuntimeError("无法生成唯一试卷码")


def _unique_dict_code(db: Session) -> str:
    for _ in range(MAX_UNIQUE_CODE_ATTEMPTS):
        code = "D" + generate_code(7)
        if not db.query(DictModel).filter_by(dict_code=code).first():
            return code
    raise RuntimeError("无法生成唯一词典码")


# ── Background analysis pipeline ─────────────────────────────────────────────

async def _run_analysis(
    task_id: str,
    file_path: Path,
    filename: str,
    req: AnalyzeRequest,
    user_id: int,
) -> None:
    try:
        _update_task(task_id, status="processing", progress=5, message="Extracting text…")

        loop = asyncio.get_event_loop()
        runtime = get_runtime_config()
        extracted = await loop.run_in_executor(None, extract_text, file_path)
        text = extracted.text
        used_ocr = extracted.used_ocr

        # Optional LLM text cleaning for OCR output
        if used_ocr and runtime.text_cleaner.enabled and runtime.text_cleaner.backend != "none":
            _update_task(task_id, progress=25, message="Cleaning OCR text with AI…")
            try:
                from backend.services.text_cleaner import clean_ocr_text
                text = await clean_ocr_text(
                    text,
                    backend=runtime.text_cleaner.backend,
                    context=runtime.text_cleaner.context_hint,
                )
            except Exception as exc:
                logger.warning("Text cleaning failed, using raw OCR: %s", exc)

        _task_store.set_text(task_id, text)
        _update_task(
            task_id, progress=30,
            message=f"Text extracted ({'OCR' if used_ocr else 'direct'}). Analysing structure…",
        )

        structured = await loop.run_in_executor(None, recognize_structure, text)
        _update_task(task_id, progress=50, message="Counting word frequencies…")

        word_table, lemma_table, family_table, stats = await loop.run_in_executor(
            None, analyse, structured, req.filters, req.weights
        )
        _update_task(task_id, progress=80, message="Building result…")

        vocab_table = []
        if req.generate_vocab:
            _update_task(task_id, progress=85, message="Generating vocabulary…")
            vocab_table = await generate_vocabulary(lemma_table, context_text=text, top_n=req.top_n)

        should_store_raw = bool(runtime.save_raw_parse_result and extracted.raw_result is not None)
        result = AnalysisResult(
            task_id=task_id, filename=filename, parse_backend=extracted.backend,
            raw_parse_stored=should_store_raw, structure_stats=stats,
            word_table=word_table, lemma_table=lemma_table,
            family_table=family_table, vocab_table=vocab_table,
        )
        _update_task(task_id, status="done", progress=100, message="Analysis complete.", result=result)

        # Persist to DB
        db = SessionLocal()
        try:
            exam_code = _unique_exam_code(db)
            exam = Exam(
                user_id=user_id, task_id=task_id, filename=filename,
                exam_code=exam_code, result_json=result.model_dump_json(),
                raw_parse_backend=extracted.backend,
                raw_parse_result_json=(
                    json.dumps(extracted.raw_result, ensure_ascii=False)
                    if should_store_raw else None
                ),
            )
            db.add(exam)
            db.commit()
            db.refresh(exam)
            meta: dict = {"user_id": user_id, "exam_id": exam.id, "exam_code": exam_code, "dict_code": None}
            _task_store.set_meta(task_id, meta)
            _update_task(task_id, exam_code=exam_code)

            if vocab_table:
                record = _upsert_dict_record(
                    db,
                    user_id=user_id,
                    exam=exam,
                    task_id=task_id,
                    filename=filename,
                    vocab=vocab_table,
                )
                meta["dict_code"] = record.dict_code
                _task_store.merge_meta(task_id, dict_code=record.dict_code)
                _update_task(task_id, dict_code=record.dict_code)
        finally:
            db.close()

    except Exception as exc:
        logger.exception("Task %s failed", task_id)
        _update_task(task_id, status="error", progress=0, message="Analysis failed.", error=str(exc))
    finally:
        try:
            file_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── Pydantic request bodies ───────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


class ResetPasswordRequest(BaseModel):
    new_password: str


# ── Auth endpoints ────────────────────────────────────────────────────────────

@app.post("/auth/register")
async def register(body: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    _enforce_rate_limit(request, "register")
    runtime = get_runtime_config(db)
    if not runtime.registration_enabled:
        raise HTTPException(403, "注册功能已关闭，请联系管理员")
    if len(body.username) < 2 or len(body.username) > 32:
        raise HTTPException(400, "用户名长度须在 2–32 字符之间")
    if len(body.password) < 6:
        raise HTTPException(400, "密码至少需要 6 个字符")
    if not body.username.replace("_", "").replace("-", "").isalnum():
        raise HTTPException(400, "用户名只能包含字母、数字、下划线和连字符")
    if db.query(User).filter_by(username=body.username).first():
        raise HTTPException(400, "该用户名已被注册")
    user = User(username=body.username, password_hash=hash_password(body.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_token(user.id, user.username, user.is_admin)
    return {
        "token": token,
        "access_token": token,
        "username": user.username,
        "is_admin": user.is_admin,
    }


@app.post("/auth/login")
async def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    _enforce_rate_limit(request, "login")
    user = db.query(User).filter_by(username=body.username).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    token = create_token(user.id, user.username, user.is_admin)
    return {
        "token": token,
        "access_token": token,
        "username": user.username,
        "is_admin": user.is_admin,
    }


@app.get("/auth/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": user.is_admin,
        "email": user.email,
        "display_name": user.display_name,
        "preferred_provider": user.preferred_provider,
    }


@app.put("/auth/profile")
async def update_profile(
    body: UserProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_user = db.query(User).filter_by(id=user.id).first()
    if body.email is not None:
        db_user.email = body.email
    if body.display_name is not None:
        db_user.display_name = body.display_name
    db.commit()
    return {
        "ok": True,
        "email": db_user.email,
        "display_name": db_user.display_name,
    }


@app.post("/auth/change-password")
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db_user = db.query(User).filter_by(id=user.id).first()
    if not verify_password(body.old_password, db_user.password_hash):
        raise HTTPException(400, "原密码不正确")
    db_user.password_hash = hash_password(body.new_password)
    db.commit()
    return {"ok": True, "message": "密码修改成功"}


# ── Analysis endpoints (auth required) ───────────────────────────────────────

@app.post("/api/analyze", response_model=TaskStatus)
async def analyze(
    background_tasks: BackgroundTasks,
    request: Request,
    file: UploadFile = File(...),
    min_word_length: Optional[int] = Form(None),
    filter_stopwords: Optional[bool] = Form(None),
    keep_proper_nouns: Optional[bool] = Form(None),
    filter_numbers: Optional[bool] = Form(None),
    filter_basic_words: Optional[bool] = Form(None),
    basic_words_threshold: Optional[float] = Form(None),
    weight_body: Optional[float] = Form(None),
    weight_stem: Optional[float] = Form(None),
    weight_option: Optional[float] = Form(None),
    top_n: Optional[int] = Form(None),
    generate_vocab: Optional[bool] = Form(None),
    db: Session = Depends(get_db),
):
    payload = _bearer_payload(request)
    if not payload:
        raise HTTPException(401, "请先登录后再上传试卷")
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if not user:
        raise HTTPException(401, "用户不存在")

    task_id = str(uuid.uuid4())
    safe_name = sanitize_filename(file.filename, fallback=f"upload-{task_id[:8]}")
    suffix = Path(safe_name).suffix or ".txt"
    upload_path = Path(settings.upload_dir) / f"{task_id}{suffix}"

    # Stream the upload to disk so we can reject oversize files before they
    # consume the full configured limit of memory.
    max_bytes = settings.max_upload_mb * 1024 * 1024
    total = 0
    try:
        with upload_path.open("wb") as sink:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    sink.close()
                    upload_path.unlink(missing_ok=True)
                    raise HTTPException(
                        413,
                        f"上传文件过大（超过 {settings.max_upload_mb} MB）",
                    )
                sink.write(chunk)
    except HTTPException:
        raise
    except Exception:
        upload_path.unlink(missing_ok=True)
        raise

    runtime = get_runtime_config(db)
    defaults = runtime.analysis

    req = AnalyzeRequest(
        filters=FilterConfig(
            min_word_length=min_word_length if min_word_length is not None else defaults.min_word_length,
            filter_stopwords=filter_stopwords if filter_stopwords is not None else defaults.filter_stopwords,
            keep_proper_nouns=keep_proper_nouns if keep_proper_nouns is not None else defaults.keep_proper_nouns,
            filter_numbers=filter_numbers if filter_numbers is not None else defaults.filter_numbers,
            filter_basic_words=filter_basic_words if filter_basic_words is not None else defaults.filter_basic_words,
            basic_words_threshold=(
                basic_words_threshold if basic_words_threshold is not None else defaults.basic_words_threshold
            ),
        ),
        weights=WeightConfig(
            weight_body=weight_body if weight_body is not None else defaults.weight_body,
            weight_stem=weight_stem if weight_stem is not None else defaults.weight_stem,
            weight_option=weight_option if weight_option is not None else defaults.weight_option,
        ),
        top_n=top_n if top_n is not None else defaults.top_n,
        generate_vocab=bool(generate_vocab) if generate_vocab is not None else False,
    )
    task = TaskStatus(task_id=task_id, status="pending", progress=0, message="Queued")
    _task_store.set(task_id, task)
    display_name = file.filename or safe_name
    background_tasks.add_task(_run_analysis, task_id, upload_path, display_name, req, user.id)
    return task


@app.get("/api/tasks/{task_id}", response_model=TaskStatus)
async def get_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = _task_store.get(task_id)
    if not task:
        raise HTTPException(404, f"Task {task_id!r} not found")
    if not _user_owns_task(task_id, user, db):
        raise HTTPException(403, "无权访问该任务")
    return task


@app.post("/api/tasks/{task_id}/vocab", response_model=TaskStatus)
async def generate_vocab_endpoint(
    task_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    top_n: Optional[int] = Form(None),
    provider: str = Form(""),
    db: Session = Depends(get_db),
):
    payload = _bearer_payload(request)
    if not payload:
        raise HTTPException(401, "请先登录")
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if not user:
        raise HTTPException(401, "用户不存在")

    task = _task_store.get(task_id)
    if not task:
        raise HTTPException(404, f"Task {task_id!r} not found")
    if not _user_owns_task(task_id, user, db):
        raise HTTPException(403, "无权访问该任务")
    if task.status != "done":
        raise HTTPException(400, "任务尚未完成")

    text = _task_store.get_text(task_id)
    lemma_table = task.result.lemma_table if task.result else []
    user_id = user.id
    meta = _task_store.get_meta(task_id)
    runtime = get_runtime_config(db)
    chosen_top_n = top_n if top_n is not None else runtime.analysis.top_n

    async def _do() -> None:
        # Mark vocab as in-flight so the frontend can poll a positive signal
        # rather than length-checking the result list (which races with the
        # final assignment below and would never flip on an empty success).
        _update_task(task_id, vocab_status="processing", message="Generating vocabulary…")
        try:
            vocab = await generate_vocabulary(
                lemma_table,
                context_text=text,
                top_n=chosen_top_n,
                provider_name=provider or runtime.vocab_provider,
            )

            db2 = SessionLocal()
            try:
                exam = None
                if meta.get("exam_id"):
                    exam = db2.query(Exam).filter_by(id=meta.get("exam_id")).first()
                if exam is None:
                    exam = db2.query(Exam).filter_by(task_id=task_id).first()

                # Apply the in-memory mutation only AFTER we've prepared the DB
                # write, so a poll between vocab arrival and persistence still
                # reflects a coherent state.
                if task.result:
                    task.result.vocab_table = vocab

                if exam and task.result:
                    _persist_exam_result(db2, exam, task.result)
                    record = _upsert_dict_record(
                        db2,
                        user_id=user_id,
                        exam=exam,
                        task_id=task_id,
                        filename=(task.result.filename if task.result else "") or exam.filename,
                        vocab=vocab,
                    )
                    _task_store.merge_meta(
                        task_id,
                        exam_id=exam.id,
                        exam_code=exam.exam_code,
                        dict_code=record.dict_code,
                    )
                    _update_task(task_id, exam_code=exam.exam_code, dict_code=record.dict_code)
            except Exception:
                db2.rollback()
                raise
            finally:
                db2.close()

            _update_task(task_id, vocab_status="done", message="Vocabulary ready.")
        except Exception as exc:
            logger.exception("Vocab generation failed for task %s", task_id)
            _update_task(
                task_id,
                vocab_status="error",
                message=f"Vocab error: {exc}",
            )

    background_tasks.add_task(_do)
    return _task_store.get(task_id)


@app.get("/api/tasks/{task_id}/export/{fmt}")
async def export_results(
    task_id: str,
    fmt: str,
    request: Request,
    selected_only: bool = Query(False),
    token: Optional[str] = Query(None, description="Fallback auth for browser-driven downloads"),
    db: Session = Depends(get_db),
):
    # Browsers can't attach Authorization headers to a navigation, so we accept
    # the same JWT via the ?token= query parameter as a controlled fallback.
    payload = _bearer_payload(request)
    if payload is None and token:
        payload = decode_token(token)
    if not payload:
        raise HTTPException(401, "请先登录后再下载")
    user = db.query(User).filter_by(id=int(payload["sub"])).first()
    if not user:
        raise HTTPException(401, "用户不存在")

    if not _user_owns_task(task_id, user, db):
        raise HTTPException(403, "无权下载该任务的结果")

    task = _task_store.get(task_id)
    # Also try loading from DB if not in memory
    if not task or not task.result:
        exam = db.query(Exam).filter_by(task_id=task_id).first()
        if exam:
            if not user.is_admin and exam.user_id != user.id:
                raise HTTPException(403, "无权下载该任务的结果")
            result = _load_analysis_result(exam.result_json)
            record = db.query(DictModel).filter_by(exam_id=exam.id).order_by(DictModel.created_at.desc()).first()
            if record:
                result.vocab_table = [VocabEntry.model_validate(item) for item in json.loads(record.vocab_json)]
        else:
            raise HTTPException(400, "没有可导出的完整结果")
    else:
        if task.status != "done":
            raise HTTPException(400, "没有可导出的完整结果")
        result = task.result

    fmt = fmt.lower()
    suffix = task_id[:EXPORT_FILENAME_TASK_SLICE]
    sel_suffix = "_selected" if selected_only else ""
    if fmt == "csv":
        return Response(
            content=to_csv(result, selected_only=selected_only),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename=vocab_{suffix}{sel_suffix}.csv"},
        )
    elif fmt == "xlsx":
        return Response(
            content=to_xlsx(result, selected_only=selected_only),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=vocab_{suffix}{sel_suffix}.xlsx"},
        )
    raise HTTPException(400, f"未知格式 '{fmt}'，请使用 'csv' 或 'xlsx'")


@app.get("/api/providers")
async def list_providers(db: Session = Depends(get_db)):
    runtime = get_runtime_config(db)
    return {"providers": available_providers(), "default": runtime.vocab_provider}


@app.get("/api/config")
async def public_config(db: Session = Depends(get_db)):
    payload = frontend_config_payload()
    payload["providers"] = available_providers()
    payload["provider_default"] = get_runtime_config(db).vocab_provider
    return payload


# ── User: list own codes ──────────────────────────────────────────────────────

@app.get("/api/codes")
async def get_my_codes(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    exams = db.query(Exam).filter_by(user_id=user.id).order_by(Exam.created_at.desc()).all()
    dicts = db.query(DictModel).filter_by(user_id=user.id).order_by(DictModel.created_at.desc()).all()
    return {
        "exams": [
            {
                "exam_code": e.exam_code,
                "filename": e.filename,
                "created_at": iso_z(e.created_at),
                "dict_count": len(e.dicts),
                "dict_codes": [d.dict_code for d in e.dicts],
                "parse_backend": e.raw_parse_backend or "local",
                "raw_parse_stored": bool(e.raw_parse_result_json),
                "is_combined": bool(e.is_combined),
                "source_exam_codes": json.loads(e.source_exam_codes) if e.source_exam_codes else [],
            }
            for e in exams
        ],
        "dicts": [
            {
                "dict_code": d.dict_code,
                "filename": d.filename,
                "created_at": iso_z(d.created_at),
                "exam_code": d.exam.exam_code if d.exam else None,
            }
            for d in dicts
        ],
    }


@app.post("/api/exams/{exam_code}/vocab")
async def generate_vocab_for_exam(
    exam_code: str,
    request: Request,
    top_n: Optional[int] = Form(None),
    provider: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_current_user(request, db)
    exam = _get_exam_for_user_or_404(db, exam_code, user)
    runtime = get_runtime_config(db)
    result = _load_analysis_result(exam.result_json)
    result.parse_backend = exam.raw_parse_backend or result.parse_backend
    result.raw_parse_stored = bool(exam.raw_parse_result_json)
    vocab = await generate_vocabulary(
        result.lemma_table,
        context_text="",
        top_n=top_n if top_n is not None else runtime.analysis.top_n,
        provider_name=provider or runtime.vocab_provider,
    )
    result.vocab_table = vocab
    _persist_exam_result(db, exam, result)
    record = _upsert_dict_record(
        db,
        user_id=exam.user_id,
        exam=exam,
        task_id=exam.task_id,
        filename=exam.filename,
        vocab=vocab,
    )
    _sync_task_result(exam.task_id, result, record.dict_code)
    _task_store.merge_meta(
        exam.task_id,
        exam_id=exam.id,
        exam_code=exam.exam_code,
        dict_code=record.dict_code,
    )
    return {
        "ok": True,
        "exam_code": exam.exam_code,
        "dict_code": record.dict_code,
        "result": result.model_dump(),
    }


@app.delete("/api/tasks/{task_id}/vocab-entry")
async def delete_vocab_entry(
    task_id: str,
    headword: str = Query(..., min_length=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = _task_store.get(task_id)
    if not task or not task.result:
        raise HTTPException(404, "任务不存在")

    if not _user_owns_task(task_id, user, db):
        raise HTTPException(403, "没有权限删除该词汇")
    meta = _task_store.get_meta(task_id)
    exam = None
    if meta.get("exam_id"):
        exam = db.query(Exam).filter_by(id=meta["exam_id"]).first()
    if exam is None:
        exam = db.query(Exam).filter_by(task_id=task_id).first()

    original_len = len(task.result.vocab_table)
    task.result.vocab_table = [
        item for item in task.result.vocab_table
        if item.headword.lower() != headword.lower() and item.lemma.lower() != headword.lower()
    ]
    if len(task.result.vocab_table) == original_len:
        raise HTTPException(404, "词汇不存在")

    if exam:
        _persist_exam_result(db, exam, task.result)
        record = db.query(DictModel).filter_by(exam_id=exam.id).order_by(DictModel.created_at.desc()).first()
        if record:
            record.vocab_json = json.dumps([v.model_dump() for v in task.result.vocab_table])
            db.commit()

    return {
        "ok": True,
        "removed": headword,
        "remaining": len(task.result.vocab_table),
        "result": task.result.model_dump(),
    }


@app.post("/api/combine-exams")
async def combine_exams(
    body: MultiExamRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Aggregate word frequencies from multiple existing exams and optionally generate vocab."""
    from backend.services.wordlist_service import tag_vocab_entries

    exams = []
    for code in body.exam_codes:
        exam = db.query(Exam).filter_by(exam_code=code.upper()).first()
        if not exam:
            raise HTTPException(404, f"试卷码 {code} 不存在")
        if not user.is_admin and exam.user_id != user.id:
            raise HTTPException(403, f"无权访问试卷 {code}")
        exams.append(exam)

    # Aggregate lemma frequencies across all exams
    combined: dict[str, LemmaEntry] = {}
    for exam in exams:
        result = _load_analysis_result(exam.result_json)
        for lemma in result.lemma_table:
            key = lemma.lemma.lower()
            if key in combined:
                existing = combined[key]
                combined[key] = LemmaEntry(
                    lemma=existing.lemma,
                    pos=existing.pos or lemma.pos,
                    family_id=existing.family_id or lemma.family_id,
                    surface_forms=list(set(existing.surface_forms + lemma.surface_forms)),
                    body_count=existing.body_count + lemma.body_count,
                    stem_count=existing.stem_count + lemma.stem_count,
                    option_count=existing.option_count + lemma.option_count,
                    total_count=existing.total_count + lemma.total_count,
                    score=existing.score + lemma.score,
                )
            else:
                combined[key] = LemmaEntry(
                    lemma=lemma.lemma, pos=lemma.pos,
                    family_id=lemma.family_id,
                    surface_forms=list(lemma.surface_forms),
                    body_count=lemma.body_count, stem_count=lemma.stem_count,
                    option_count=lemma.option_count, total_count=lemma.total_count,
                    score=lemma.score,
                )

    merged_lemmas = sorted(combined.values(), key=lambda x: x.score, reverse=True)

    runtime = get_runtime_config(db)
    filenames = " + ".join(e.filename for e in exams[:COMBINE_EXAMS_FILENAME_CAP])
    if len(exams) > COMBINE_EXAMS_FILENAME_CAP:
        filenames += f" 等{len(exams)}份试卷"

    vocab_table: list[VocabEntry] = []
    if body.generate_vocab:
        vocab_table = await generate_vocabulary(
            merged_lemmas,
            context_text="",
            top_n=body.top_n,
            provider_name=body.provider or runtime.vocab_provider,
        )
        tag_vocab_entries(vocab_table)

    source_codes = [e.exam_code for e in exams]
    combined_result = AnalysisResult(
        task_id=str(uuid.uuid4()),
        filename=filenames,
        parse_backend="combined",
        lemma_table=merged_lemmas[:500],
        vocab_table=vocab_table,
        is_combined=True,
        source_exam_codes=source_codes,
    )

    # Save combined exam record
    exam_code = _unique_exam_code(db)
    import json as _json
    new_exam = Exam(
        user_id=user.id,
        task_id=combined_result.task_id,
        filename=filenames,
        exam_code=exam_code,
        result_json=combined_result.model_dump_json(),
        raw_parse_backend="combined",
        is_combined=True,
        source_exam_codes=_json.dumps(source_codes),
    )
    db.add(new_exam)
    db.commit()
    db.refresh(new_exam)

    dict_code = None
    if vocab_table:
        record = _upsert_dict_record(
            db, user_id=user.id, exam=new_exam,
            task_id=combined_result.task_id,
            filename=filenames, vocab=vocab_table,
        )
        dict_code = record.dict_code

    return {
        "ok": True,
        "exam_code": exam_code,
        "dict_code": dict_code,
        "source_exam_codes": source_codes,
        "lemma_count": len(merged_lemmas),
        "vocab_count": len(vocab_table),
        "result": combined_result.model_dump(),
    }


@app.put("/api/dicts/{dict_code}/selection")
async def update_vocab_selection(
    dict_code: str,
    body: VocabSelectionRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update which words are selected/deselected in a vocab list."""
    record = _get_dict_for_user_or_404(db, dict_code, user)
    vocab = [VocabEntry.model_validate(item) for item in json.loads(record.vocab_json)]
    for entry in vocab:
        if entry.headword in body.selections:
            entry.selected = body.selections[entry.headword]
    record.vocab_json = json.dumps([v.model_dump() for v in vocab])
    db.commit()

    # Sync to in-memory task if active
    exam = record.exam
    if exam:
        for task_id in list(_task_store.active_task_ids()):
            meta = _task_store.get_meta(task_id)
            if meta.get("exam_id") != exam.id:
                continue
            task = _task_store.get(task_id)
            if task and task.result:
                task.result.vocab_table = vocab
            break

    return {"ok": True, "updated": len(body.selections), "vocab_count": len(vocab)}


@app.post("/api/vocab-ai-select")
async def vocab_ai_select(
    body: AISelectRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Use AI to intelligently select vocabulary words suitable for a study goal."""
    from backend.services.wordlist_service import get_word_level

    # Find the exam and its vocab
    exam = db.query(Exam).filter_by(task_id=body.task_id).first()
    if not exam:
        # Try finding by exam_code (task_id might be exam_code)
        exam = db.query(Exam).filter_by(exam_code=body.task_id.upper()).first()
    if not exam:
        raise HTTPException(404, "试卷不存在")
    if not user.is_admin and exam.user_id != user.id:
        raise HTTPException(403, "无权访问该试卷")

    result = _load_analysis_result(exam.result_json)
    if not result.vocab_table:
        raise HTTPException(400, "该试卷尚未生成词汇表，请先生成词汇表")

    # Tag words with level if not already done
    for entry in result.vocab_table:
        if not entry.word_level:
            entry.word_level = get_word_level(entry.headword or entry.lemma or "")

    # Build candidate list for AI review
    word_info = [
        {
            "word": e.headword,
            "pos": e.pos,
            "chinese": e.chinese_meaning,
            "level": e.word_level,
            "score": round(e.score, 2),
        }
        for e in result.vocab_table
    ]

    runtime = get_runtime_config(db)
    provider_name = body.provider or runtime.vocab_provider

    # Use AI to select words
    try:
        import json as _json
        prompt = f"""You are a senior 高考 English vocabulary coach helping Chinese students maximize their exam performance.

Study goal: {body.goal}
Words to select: {body.max_words}

## Selection Criteria (in priority order)
1. PRIORITY: 高考 level words the student likely does NOT know — highest study value
2. INCLUDE sparingly: 超纲 words that appear very frequently in this exam (score ≥ 2.0)
3. SKIP: 基础 words (extremely common, students already know: the, go, big, year, get, make)
4. PREFER: words with high option_count (appeared in answer choices = high exam importance)
5. PREFER: words with rich collocations or usage patterns useful in 完形填空/阅读理解

## Output
Return ONLY a valid JSON object — no markdown, no extra text:
{{"selected": ["word1", "word2", ...], "reasoning": "2-3 sentence explanation of selection strategy"}}

The "selected" array must contain exactly {body.max_words} words (or fewer if the list is too small).
Preserve the original spelling from the input list.

## Word List
{_json.dumps(word_info, ensure_ascii=False, indent=2)}"""

        selected_words: list[str] = []
        reasoning = ""

        runtime = get_runtime_config(db)
        if provider_name == "claude" and runtime.llm.anthropic_api_key:
            import anthropic
            client = anthropic.AsyncAnthropic(api_key=runtime.llm.anthropic_api_key)
            resp = await client.messages.create(
                model=runtime.ai_model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```", 2)[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.rsplit("```", 1)[0].strip()
            data = _json.loads(raw)
            selected_words = data.get("selected", [])
            reasoning = data.get("reasoning", "")
        else:
            # Fallback: select by level priority and score
            gaokao_words = [e for e in result.vocab_table if e.word_level in ("高考", None)]
            gaokao_words.sort(key=lambda x: x.score, reverse=True)
            selected_words = [e.headword for e in gaokao_words[:body.max_words]]
            reasoning = "基于词汇等级和词频自动筛选（高考词汇优先）"

        # Apply selections
        selected_set = {w.lower() for w in selected_words}
        selections = {}
        for entry in result.vocab_table:
            entry.selected = entry.headword.lower() in selected_set
            selections[entry.headword] = entry.selected

        # Persist updated vocab
        _persist_exam_result(db, exam, result)
        record = db.query(DictModel).filter_by(exam_id=exam.id).order_by(DictModel.created_at.desc()).first()
        if record:
            record.vocab_json = json.dumps([v.model_dump() for v in result.vocab_table])
            db.commit()

        return {
            "ok": True,
            "selected_count": len(selected_words),
            "total_count": len(result.vocab_table),
            "selections": selections,
            "reasoning": reasoning,
            "vocab": [v.model_dump() for v in result.vocab_table],
        }
    except Exception as exc:
        logger.exception("AI word selection failed")
        raise HTTPException(500, f"AI选词失败: {exc}")


@app.get("/api/exams")
async def list_my_exams(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all exams for the current user with summary info."""
    exams = db.query(Exam).filter_by(user_id=user.id).order_by(Exam.created_at.desc()).all()
    result = []
    for e in exams:
        try:
            data = json.loads(e.result_json)
            lemma_count = len(data.get("lemma_table", []))
            vocab_count = len(data.get("vocab_table", []))
        except Exception:
            lemma_count = 0
            vocab_count = 0
        result.append({
            "exam_code": e.exam_code,
            "task_id": e.task_id,
            "filename": e.filename,
            "created_at": iso_z(e.created_at),
            "parse_backend": e.raw_parse_backend or "local",
            "is_combined": bool(e.is_combined),
            "source_exam_codes": json.loads(e.source_exam_codes) if e.source_exam_codes else [],
            "lemma_count": lemma_count,
            "vocab_count": vocab_count,
            "dict_count": len(e.dicts),
            "dict_codes": [d.dict_code for d in e.dicts],
        })
    return result


@app.delete("/api/exams/{exam_code}")
async def delete_exam(
    exam_code: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    exam = _get_exam_for_user_or_404(db, exam_code, user)
    related_dicts = db.query(DictModel).filter_by(exam_id=exam.id).all()
    task_id = exam.task_id
    for record in related_dicts:
        db.delete(record)
    db.delete(exam)
    db.commit()
    _purge_task_cache(task_id)
    return {"ok": True, "exam_code": exam_code.upper()}


@app.delete("/api/dicts/{dict_code}")
async def delete_dict(
    dict_code: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record = _get_dict_for_user_or_404(db, dict_code, user)
    task_id = record.task_id
    exam_id = record.exam_id
    db.delete(record)
    db.commit()

    if task_id:
        existing_meta = _task_store.get_meta(task_id)
        if existing_meta.get("dict_code") == dict_code.upper():
            _task_store.merge_meta(task_id, dict_code=None)
            _update_task(task_id, dict_code=None)

    if exam_id:
        exam = db.query(Exam).filter_by(id=exam_id).first()
        if exam:
            result = _load_analysis_result(exam.result_json)
            result.parse_backend = exam.raw_parse_backend or result.parse_backend
            result.raw_parse_stored = bool(exam.raw_parse_result_json)
            replacement = (
                db.query(DictModel)
                .filter_by(exam_id=exam_id)
                .order_by(DictModel.created_at.desc())
                .first()
            )
            result.vocab_table = (
                []
                if replacement is None
                else [VocabEntry.model_validate(item) for item in json.loads(replacement.vocab_json)]
            )
            _persist_exam_result(db, exam, result)
            _sync_task_result(exam.task_id, result, replacement.dict_code if replacement else None)
            _update_task(exam.task_id, dict_code=replacement.dict_code if replacement else None)

    return {"ok": True, "dict_code": dict_code.upper()}


# ── Public share endpoints ────────────────────────────────────────────────────

@app.get("/api/share/exam/{code}")
async def share_exam(code: str, db: Session = Depends(get_db)):
    exam = db.query(Exam).filter_by(exam_code=code.upper()).first()
    if not exam:
        raise HTTPException(404, "试卷码不存在")
    return {
        "exam_code": exam.exam_code,
        "filename": exam.filename,
        "created_at": iso_z(exam.created_at),
        "uploaded_by": exam.user.username,
        "parse_backend": exam.raw_parse_backend or "local",
        "raw_parse_stored": bool(exam.raw_parse_result_json),
        "result": json.loads(exam.result_json),
    }


@app.get("/api/share/dict/{code}")
async def share_dict(code: str, db: Session = Depends(get_db)):
    d = db.query(DictModel).filter_by(dict_code=code.upper()).first()
    if not d:
        raise HTTPException(404, "词典码不存在")
    return {
        "dict_code": d.dict_code,
        "filename": d.filename,
        "created_at": iso_z(d.created_at),
        "uploaded_by": d.user.username,
        "exam_code": d.exam.exam_code if d.exam else None,
        "vocab": json.loads(d.vocab_json),
    }


# ── Admin endpoints ───────────────────────────────────────────────────────────

@app.get("/admin/overview")
async def admin_overview(admin: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    return _admin_overview_payload(db)


@app.get("/admin/users")
async def admin_list_users(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
    limit: int = Query(ADMIN_LIST_DEFAULT_LIMIT, ge=1, le=ADMIN_LIST_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    # Returns an array (legacy shape) — pagination via ?limit=&offset=.
    users = (
        db.query(User).order_by(User.created_at.asc()).offset(offset).limit(limit).all()
    )
    return [
        {
            "id": u.id,
            "username": u.username,
            "is_admin": u.is_admin,
            "created_at": iso_z(u.created_at),
            "exam_count": len(u.exams),
            "dict_count": len(u.dicts),
            "latest_exam_at": (
                iso_z(max((exam.created_at for exam in u.exams), default=None))
                if u.exams else None
            ),
            "latest_dict_at": (
                iso_z(max((record.created_at for record in u.dicts), default=None))
                if u.dicts else None
            ),
        }
        for u in users
    ]


@app.post("/admin/users/{user_id}/reset-password")
async def admin_reset_password(
    user_id: int,
    body: ResetPasswordRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    if len(body.new_password) < 6:
        raise HTTPException(400, "密码至少需要 6 个字符")
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "用户不存在")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return {"ok": True, "message": f"用户 {user.username} 的密码已重置"}


@app.get("/admin/codes")
async def admin_list_codes(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
    limit: int = Query(ADMIN_LIST_DEFAULT_LIMIT, ge=1, le=ADMIN_LIST_MAX_LIMIT),
    offset: int = Query(0, ge=0),
):
    exams = (
        db.query(Exam).order_by(Exam.created_at.desc()).offset(offset).limit(limit).all()
    )
    dicts = (
        db.query(DictModel).order_by(DictModel.created_at.desc()).offset(offset).limit(limit).all()
    )
    return {
        "summary": _admin_overview_payload(db),
        "exams": [
            {
                "exam_code": e.exam_code,
                "filename": e.filename,
                "username": e.user.username,
                "created_at": iso_z(e.created_at),
                "parse_backend": e.raw_parse_backend or "local",
                "raw_parse_stored": bool(e.raw_parse_result_json),
            }
            for e in exams
        ],
        "dicts": [
            {
                "dict_code": d.dict_code,
                "filename": d.filename,
                "username": d.user.username,
                "exam_code": d.exam.exam_code if d.exam else None,
                "created_at": iso_z(d.created_at),
            }
            for d in dicts
        ],
    }


@app.get("/admin/config")
async def admin_get_config(admin: User = Depends(get_admin_user), db: Session = Depends(get_db)):
    return {
        "config": get_runtime_config(db).model_dump(),
        "providers": available_providers(),
        "ocr_capabilities": frontend_config_payload()["ocr_capabilities"],
        "parse_backends": frontend_config_payload()["parse_backends"],
    }


@app.put("/admin/config")
async def admin_update_config(
    body: dict,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    config = save_runtime_config(body, db)
    return {
        "ok": True,
        "config": config.model_dump(),
        "providers": available_providers(),
        "ocr_capabilities": frontend_config_payload()["ocr_capabilities"],
        "parse_backends": frontend_config_payload()["parse_backends"],
    }


@app.get("/admin/exams/{exam_code}/raw-parse")
async def admin_get_exam_raw_parse(
    exam_code: str,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    exam = db.query(Exam).filter_by(exam_code=exam_code.upper()).first()
    if not exam:
        raise HTTPException(404, "试卷不存在")
    if not exam.raw_parse_result_json:
        raise HTTPException(404, "该试卷未存储原始解析结果")
    return {
        "exam_code": exam.exam_code,
        "filename": exam.filename,
        "parse_backend": exam.raw_parse_backend or "local",
        "raw_parse_result": json.loads(exam.raw_parse_result_json),
    }


class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


@app.post("/admin/users")
async def admin_create_user(
    body: CreateUserRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    if len(body.username) < 2 or len(body.username) > 32:
        raise HTTPException(400, "用户名长度须在 2–32 字符之间")
    if len(body.password) < 6:
        raise HTTPException(400, "密码至少需要 6 个字符")
    if db.query(User).filter_by(username=body.username).first():
        raise HTTPException(400, "该用户名已被注册")
    user = User(username=body.username, password_hash=hash_password(body.password), is_admin=body.is_admin)
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"ok": True, "id": user.id, "username": user.username, "is_admin": user.is_admin}


@app.delete("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    if user_id == admin.id:
        raise HTTPException(400, "不能删除自己的账号")
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "用户不存在")
    if user.is_admin:
        raise HTTPException(400, "不能删除其他管理员账号")
    # Delete associated records
    for exam in user.exams:
        for d in exam.dicts:
            db.delete(d)
        db.delete(exam)
    for d in user.dicts:
        db.delete(d)
    db.delete(user)
    db.commit()
    return {"ok": True, "message": f"用户 {user.username} 已删除"}


@app.get("/admin/system")
async def admin_system_info(admin: User = Depends(get_admin_user)):
    """Return system/environment information for diagnostics."""
    import sys
    import importlib.util

    def pkg_version(name: str) -> str:
        try:
            import importlib.metadata
            return importlib.metadata.version(name)
        except Exception:
            return "unknown"

    packages = {
        "pytesseract": pkg_version("pytesseract"),
        "rapidocr": pkg_version("rapidocr"),
        "pdfplumber": pkg_version("pdfplumber"),
        "pdf2image": pkg_version("pdf2image"),
        "PyMuPDF": pkg_version("PyMuPDF"),
        "spacy": pkg_version("spacy"),
        "nltk": pkg_version("nltk"),
        "anthropic": pkg_version("anthropic"),
        "wordfreq": pkg_version("wordfreq"),
    }
    spacy_model = False
    try:
        import spacy
        spacy.load("en_core_web_sm")
        spacy_model = True
    except Exception:
        pass

    from backend.services.ocr_cache import cache_stats
    runtime = get_runtime_config()
    llm = runtime.llm
    return {
        "python_version": sys.version,
        "packages": packages,
        "spacy_model_loaded": spacy_model,
        "upload_dir": str(settings.upload_dir),
        "db_path": str(settings.db_path),
        "ai_model": runtime.ai_model,
        "has_anthropic_key": bool(llm.anthropic_api_key),
        "has_deepseek_key": bool(llm.deepseek_api_key),
        "has_openai_key": bool(llm.openai_api_key),
        "has_mw_key": bool(settings.merriam_webster_key),
        "has_youdao_key": bool(settings.youdao_app_key),
        "has_ecdict": bool(settings.ecdict_path),
        "deepseek_base_url": llm.deepseek_base_url,
        "openai_base_url": llm.openai_base_url or "",
        "ocr_cache": cache_stats(),
        "file_store_dir": str(settings.file_store_dir),
    }


@app.get("/admin/exams/{exam_code}/extracted-text")
async def admin_get_exam_text(
    exam_code: str,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    """Return the raw extracted text stored for an exam (for OCR debugging)."""
    exam = db.query(Exam).filter_by(exam_code=exam_code.upper()).first()
    if not exam:
        raise HTTPException(404, "试卷不存在")
    result_data = json.loads(exam.result_json) if exam.result_json else {}
    return {
        "exam_code": exam.exam_code,
        "filename": exam.filename,
        "parse_backend": exam.raw_parse_backend or "local",
        "word_count": len(result_data.get("word_table", [])),
        "lemma_count": len(result_data.get("lemma_table", [])),
    }


# ── OCR cache management endpoints ───────────────────────────────────────────

@app.get("/admin/ocr-cache")
async def admin_ocr_cache_stats(admin: User = Depends(get_admin_user)):
    """Return OCR cache statistics."""
    from backend.services.ocr_cache import cache_stats
    return cache_stats()


@app.delete("/admin/ocr-cache")
async def admin_clear_ocr_cache(admin: User = Depends(get_admin_user)):
    """Clear the entire OCR cache."""
    from backend.services.ocr_cache import clear_all
    deleted = clear_all()
    return {"ok": True, "deleted": deleted, "message": f"已清除 {deleted} 条OCR缓存"}


@app.post("/admin/ocr-test")
async def admin_ocr_test(
    file: UploadFile = File(...),
    use_cache: bool = Query(False, description="Whether to consult/update the OCR cache"),
    admin: User = Depends(get_admin_user),
):
    """Upload a file and return the raw OCR-extracted text for debugging.

    By default the cache is bypassed so the endpoint reflects current OCR
    behaviour; pass ?use_cache=true to reuse any cached extraction.
    """
    import tempfile
    suffix = Path(file.filename or "upload.tmp").suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        tmp_path.write_bytes(await file.read())
        extracted = extract_text(tmp_path, use_cache=use_cache)
        return {
            "filename": file.filename,
            "used_ocr": extracted.used_ocr,
            "backend": extracted.backend,
            "text_length": len(extracted.text),
            "text_preview": extracted.text[:OCR_TEST_PREVIEW_CHARS],
            "text": extracted.text,
        }
    except Exception as exc:
        raise HTTPException(500, f"文本提取失败: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)


# ── Provider test endpoints ───────────────────────────────────────────────────

@app.post("/admin/providers/test")
async def admin_test_provider(
    body: dict,
    admin: User = Depends(get_admin_user),
):
    """Test a vocabulary provider with a single word."""
    from backend.services.vocabulary_generator import _REGISTRY
    provider_name = body.get("provider", "")
    test_word = body.get("word", "eloquent")

    if provider_name not in _REGISTRY:
        raise HTTPException(400, f"Provider '{provider_name}' not available")

    try:
        from backend.models.schemas import LemmaEntry
        provider = _REGISTRY[provider_name]()
        entries = [LemmaEntry(
            lemma=test_word, pos="ADJ", family_id=test_word,
            body_count=1, stem_count=0, option_count=1, total_count=2, score=3.0
        )]
        results = await provider.enrich(entries)
        return {
            "ok": True,
            "provider": provider_name,
            "word": test_word,
            "result": results[0].model_dump() if results else None,
        }
    except Exception as exc:
        return {"ok": False, "provider": provider_name, "error": str(exc)}


# ── Admin panel SPA ───────────────────────────────────────────────────────────

_ADMIN_PANEL = Path(__file__).parent.parent / "frontend" / "admin.html"


@app.get("/admin-panel", response_class=HTMLResponse)
async def admin_panel():
    if _ADMIN_PANEL.exists():
        return _ADMIN_PANEL.read_text(encoding="utf-8")
    return HTMLResponse("<h1>Admin panel not found</h1>", status_code=404)


# ── Management SPA ────────────────────────────────────────────────────────────

_MANAGE_PANEL = Path(__file__).parent.parent / "frontend" / "manage.html"


@app.get("/manage", response_class=HTMLResponse)
async def manage_panel():
    if _MANAGE_PANEL.exists():
        return _MANAGE_PANEL.read_text(encoding="utf-8")
    return HTMLResponse("<h1>Management panel not found</h1>", status_code=404)


# ── Frontend SPA ──────────────────────────────────────────────────────────────

_FRONTEND = Path(__file__).parent.parent / "frontend" / "index.html"


@app.get("/", response_class=HTMLResponse)
async def index():
    if _FRONTEND.exists():
        return _FRONTEND.read_text(encoding="utf-8")
    return HTMLResponse("<h1>Frontend not found — place frontend/index.html</h1>", status_code=404)
