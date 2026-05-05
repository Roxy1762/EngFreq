"""Authentication utilities: JWT tokens, password hashing, share-code generation."""
from __future__ import annotations

import os
import pathlib
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

# ── Secret key: read from env, or persist in a local file ────────────────────
# Anchor to the project root so the same key is used regardless of cwd.
_KEY_FILE = str(pathlib.Path(__file__).parent.parent / "secret.key")
if not os.path.exists(_KEY_FILE):
    _generated = secrets.token_hex(32)
    # Open with 0600 so the JWT signing key isn't world-readable when
    # the default umask is permissive (e.g. running as root in a container).
    _fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(_fd, _generated.encode("ascii"))
    finally:
        os.close(_fd)

try:
    os.chmod(_KEY_FILE, 0o600)
except OSError:
    pass

with open(_KEY_FILE) as _f:
    _FILE_SECRET = _f.read().strip()

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
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_HOURS)
    return jwt.encode(
        {"sub": str(user_id), "username": username, "is_admin": is_admin, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ── Share-code generation ─────────────────────────────────────────────────────
_CHARS = string.ascii_uppercase + string.digits


def generate_code(length: int = 8) -> str:
    """Return a random uppercase-alphanumeric code of given length."""
    return "".join(secrets.choice(_CHARS) for _ in range(length))
