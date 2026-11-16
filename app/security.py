import hashlib
import hmac
import secrets
import time

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request, Response

SESSION_COOKIE = "ll_session"
CSRF_COOKIE = "ll_csrf"

_hasher = PasswordHasher()
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_token(user_id: int, secret: str, ttl_seconds: int) -> str:
    now = int(time.time())
    claims = {"sub": str(user_id), "iat": now, "exp": now + ttl_seconds}
    return jwt.encode(claims, secret, algorithm="HS256")


def decode_token(token: str, secret: str) -> int | None:
    try:
        claims = jwt.decode(
            token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]}
        )
        return int(claims["sub"])
    except (jwt.PyJWTError, ValueError):
        return None


def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def session_csrf_token(secret: str, session_token: str) -> str:
    """CSRF token for a logged-in session: bound to the session JWT, so a token
    from one session is useless in another."""
    return _sign(secret, f"csrf:{session_token}")


def new_anonymous_csrf_token(secret: str) -> str:
    value = secrets.token_urlsafe(32)
    return f"{value}.{_sign(secret, f'anon:{value}')}"


def is_valid_anonymous_csrf_token(secret: str, token: str) -> bool:
    value, _, signature = token.partition(".")
    return bool(value) and hmac.compare_digest(signature, _sign(secret, f"anon:{value}"))


def tokens_match(expected: str, submitted: str) -> bool:
    return bool(expected) and hmac.compare_digest(expected, submitted)


def set_cookie(response: Response, request: Request, key: str, value: str, max_age: int) -> None:
    # Secure is skipped only for local development over plain http.
    response.set_cookie(
        key,
        value,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        secure=request.url.hostname not in _LOCAL_HOSTS,
        path="/",
    )
