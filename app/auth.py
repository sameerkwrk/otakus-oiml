"""
Authentication & authorization for the NAWI platform.

Two operational roles enforce separation of duties end-to-end:

  TESTER    Registers instruments, submits test suites, and creates
            revisions after a rejection. Cannot approve or reject
            anything -- including their own reports.
  APPROVER  Reviews submitted reports and approves or rejects them.
            Cannot register instruments or submit test data, so an
            approver can never become the source of the very numbers
            they're supposed to be checking.

ADMIN exists only to create/deactivate accounts (see /api/auth/register)
and is not meant to test or approve day-to-day work itself.

Identity is asserted by a signed JWT, never by a client-supplied name
string. `technician_name` / `approved_by` are always taken server-side
from the authenticated token in main.py -- the client can no longer
submit a report "as" someone else, or approve as someone else, just by
typing a different name into a text box.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel

# ---------------------------------------------------------------------
# Secret / token configuration
# ---------------------------------------------------------------------

SECRET_KEY = os.environ.get("NAWI_JWT_SECRET")
if not SECRET_KEY:
    # Refuse to start with a guessable secret in anything that claims to
    # be production. For local/dev use we fall back to a clearly-marked
    # placeholder so the app still runs out of the box.
    if os.environ.get("NAWI_ENV", "dev").lower() == "production":
        raise RuntimeError(
            "NAWI_JWT_SECRET environment variable must be set before running in production."
        )
    SECRET_KEY = "dev-only-insecure-secret-CHANGE-ME"

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("NAWI_TOKEN_EXPIRE_MINUTES", "480"))  # 8h shift

VALID_ROLES = ("TESTER", "APPROVER", "ADMIN")

# auto_error=False so we can raise our own 401 with a consistent message
# whether the header is missing, malformed, or the token itself is bad.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


class TokenUser(BaseModel):
    id: int
    username: str
    full_name: str
    role: str


# ---------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------
# JWT issuing / validation
# ---------------------------------------------------------------------

def create_access_token(user: TokenUser) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "full_name": user.full_name,
        "role": user.role,
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _get_db_conn() -> sqlite3.Connection:
    # Imported lazily to avoid a circular import (main.py imports this module).
    from app.main import DATABASE
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def get_current_user(token: Optional[str] = Depends(oauth2_scheme)) -> TokenUser:
    """Decode + validate the bearer token AND re-check the user's row in
    the DB on every request (not just trusting the token's claims). That
    means deactivating an account revokes access immediately, rather
    than only once that user's existing tokens happen to expire."""
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials. Please log in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_error
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError, TypeError):
        raise credentials_error

    conn = _get_db_conn()
    try:
        row = conn.execute(
            "SELECT id, username, full_name, role, active FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row or not row["active"]:
        raise credentials_error

    return TokenUser(id=row["id"], username=row["username"], full_name=row["full_name"], role=row["role"])


def require_role(*roles: str):
    """FastAPI dependency factory. `Depends(require_role("TESTER"))` only
    lets TESTER (and, for account-management convenience, ADMIN -- add it
    explicitly per-call if you want it included) through; anyone else
    gets 403. Call with no args, `Depends(require_role())`, to require
    *any* authenticated, active user regardless of role."""
    allowed = set(roles) if roles else set(VALID_ROLES)

    def _checker(user: TokenUser = Depends(get_current_user)) -> TokenUser:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires role {sorted(allowed)}, but your account is {user.role}.",
            )
        return user

    return _checker