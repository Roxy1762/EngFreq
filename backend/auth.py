"""Authentication utilities: JWT tokens, password hashing, share-code generation."""
from __future__ import annotations

import logging
import os
import pathlib
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

logger = logging.getLogger(__name__)

# ── Secret key: read from env, or persist in a local file ────────────────────
# Anchor to the project root so the same key is used regardless of cwd.
_KEY_FILE = str(pathlib.Path(__file__).parent.parent / "secret.key")


def _bootstrap_secret_key() -> str:
    """Read or create the JWT signing key, race-safe across multiple workers.

    Multi-worker setups (e.g. ``--prod``) can spawn 4+ processes that all hit
    this code at startup. Using ``O_CREAT|O_EXCL`` racing with a file-existence
    check would crash one worker with FileExistsError. Catching the race lets
    the late starter fall through to reading the file the winner created.
    """
    if not os.path.exists(_KEY_FILE):
        generated = secrets.token_hex(32)
        try:
            # 0600 so the signing key isn't world-readable when the default
            # umask is permissive (e.g. running as root in a container).
            fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, generated.encode("ascii"))
            finally:
                os.close(fd)
        except FileExistsError:
            # Another worker won the race — its key is what we'll read below.
            logger.debug("secret.key already created by another process")

    try:
        os.chmod(_KEY_FILE, 0o600)
    except OSError:
        pass

    with open(_KEY_FILE) as f:
        return f.read().strip()


_FILE_SECRET = _bootstrap_secret_key()
SECRET_KEY: str = os.environ.get("SECRET_KEY", _FILE_SECRET)
ALGORITHM = "HS256"
TOKEN_HOURS = 24 * 7  # tokens valid for 1 week

# ── Password hashing ──────────────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(pw: str) -> str:
    return pwd_context.hash(pw)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT ───────────────────────────────────────────────────────────────────────

def create_token(user_id: int, username: str, is_admin: bool) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(hours=TOKEN_HOURS)
    return jwt.encode(
        {
            "sub": str(user_id),
            "username": username,
            "is_admin": is_admin,
            "iat": now,
            "exp": expire,
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def decode_token(token: str) -> Optional[dict]:
    if not token or not isinstance(token, str):
        return None
    try:
        # require_exp + verify_exp protect against forged tokens missing
        # an expiry claim or replaying an expired one.
        return jwt.decode(
            token,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "sub"], "verify_exp": True},
        )
    except JWTError:
        return None
    except Exception as exc:   # noqa: BLE001
        logger.debug("decode_token unexpected error: %s", exc)
        return None


# ── Password complexity ───────────────────────────────────────────────────────

MIN_PASSWORD_LENGTH = 6
MAX_PASSWORD_LENGTH = 128


def password_strength_error(password: str) -> Optional[str]:
    """Validate a password. Returns Chinese-language error message or None."""
    if not isinstance(password, str):
        return "密码格式无效"
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符"
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"密码不能超过 {MAX_PASSWORD_LENGTH} 个字符"
    if password.strip() != password:
        return "密码不能以空白字符开头或结尾"
    return None


# ── Share-code generation ─────────────────────────────────────────────────────
_CHARS = string.ascii_uppercase + string.digits


def generate_code(length: int = 8) -> str:
    """Return a random uppercase-alphanumeric code of given length."""
    return "".join(secrets.choice(_CHARS) for _ in range(length))
