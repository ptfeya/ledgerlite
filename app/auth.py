import logging
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import InterfaceError, OperationalError
from psycopg_pool import ConnectionPool, PoolTimeout

from app.config import get_settings
from app.security import SESSION_COOKIE, decode_token, verify_password

_bearer = HTTPBearer(auto_error=False)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CurrentUser:
    id: int
    email: str
    token: str


class LoginRequired(Exception):
    """Raised by page routes when there is no valid session cookie."""


class AuthenticationUnavailable(Exception):
    """Raised when the login database or connection pool is unavailable."""


def get_pool(request: Request) -> ConnectionPool:
    return request.app.state.pool


def authenticate(pool: ConnectionPool, email: str, password: str) -> int | None:
    try:
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT id, password_hash FROM users WHERE email = %s", (email.strip().lower(),)
            ).fetchone()
    except (OperationalError, InterfaceError, PoolTimeout) as exc:
        # Exception messages and tracebacks can contain credentials.
        logger.error("Authentication database unavailable (%s)", type(exc).__name__)
        raise AuthenticationUnavailable from None
    if row is None or not verify_password(row["password_hash"], password):
        return None
    return row["id"]


def _user_for_token(pool: ConnectionPool, token: str) -> CurrentUser | None:
    user_id = decode_token(token, get_settings().jwt_secret)
    if user_id is None:
        return None
    with pool.connection() as conn:
        row = conn.execute("SELECT id, email FROM users WHERE id = %s", (user_id,)).fetchone()
    return CurrentUser(row["id"], row["email"], token) if row else None


def get_api_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    pool: ConnectionPool = Depends(get_pool),
) -> CurrentUser:
    """JSON API auth: Authorization header only. Cookies are deliberately not
    accepted here, so cookie-borne CSRF cannot reach the API."""
    user = _user_for_token(pool, credentials.credentials) if credentials else None
    if user is None:
        raise HTTPException(401, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    return user


def get_page_user(request: Request, pool: ConnectionPool = Depends(get_pool)) -> CurrentUser:
    token = request.cookies.get(SESSION_COOKIE)
    user = _user_for_token(pool, token) if token else None
    if user is None:
        raise LoginRequired
    return user
