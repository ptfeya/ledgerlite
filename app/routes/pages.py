from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from psycopg_pool import ConnectionPool

from app import ledger
from app.auth import AuthenticationUnavailable, CurrentUser, authenticate, get_page_user, get_pool
from app.config import get_settings
from app.security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    create_token,
    is_valid_anonymous_csrf_token,
    new_anonymous_csrf_token,
    session_csrf_token,
    set_cookie,
    tokens_match,
)

router = APIRouter(prefix="/ui")
root_router = APIRouter()

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{remainder:02d}"


templates.env.filters["money"] = format_money


def parse_amount_cents(raw: str) -> int | None:
    """'12.34' -> 1234. None for anything that is not a positive amount with at
    most two decimals."""
    try:
        amount = Decimal(raw.strip())
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -2:
        return None
    cents = int(amount * 100)
    return cents if cents <= ledger.MAX_AMOUNT_CENTS else None


def parse_id(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw.isascii() and raw.isdigit() and len(raw) < 18 else None


def render(
    request: Request,
    name: str,
    user: CurrentUser | None = None,
    status_code: int = 200,
    **context,
) -> Response:
    if user is not None:
        context["csrf_token"] = session_csrf_token(get_settings().jwt_secret, user.token)
    context["user"] = user
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def require_csrf(user: CurrentUser, submitted: str) -> None:
    expected = session_csrf_token(get_settings().jwt_secret, user.token)
    if not tokens_match(expected, submitted):
        raise HTTPException(403, "Invalid or missing CSRF token")


@root_router.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    return RedirectResponse("/ui/accounts", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> Response:
    # No session yet, so the login form uses a double-submit token: a signed
    # random value that must match the cookie that came with the request.
    secret = get_settings().jwt_secret
    token = request.cookies.get(CSRF_COOKIE, "")
    fresh = not is_valid_anonymous_csrf_token(secret, token)
    if fresh:
        token = new_anonymous_csrf_token(secret)
    response = render(request, "login.html", csrf_token=token)
    if fresh:
        set_cookie(response, request, CSRF_COOKIE, token, max_age=3600)
    return response


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    csrf_token: str = Form(""),
    pool: ConnectionPool = Depends(get_pool),
) -> Response:
    secret = get_settings().jwt_secret
    cookie_token = request.cookies.get(CSRF_COOKIE, "")
    if not (
        tokens_match(cookie_token, csrf_token)
        and is_valid_anonymous_csrf_token(secret, cookie_token)
    ):
        raise HTTPException(403, "Invalid or missing CSRF token")

    if not email.strip() or not password:
        return render(
            request,
            "login.html",
            status_code=422,
            csrf_token=cookie_token,
            error="Enter your email and password.",
        )

    try:
        user_id = authenticate(pool, email, password)
    except AuthenticationUnavailable:
        return render(
            request,
            "login.html",
            status_code=503,
            csrf_token=cookie_token,
            error="Service temporarily unavailable",
        )
    if user_id is None:
        return render(
            request,
            "login.html",
            status_code=401,
            csrf_token=cookie_token,
            error="Invalid credentials",
        )

    settings = get_settings()
    session = create_token(user_id, settings.jwt_secret, settings.jwt_ttl_seconds)
    response = RedirectResponse("/ui/accounts", status_code=303)
    set_cookie(response, request, SESSION_COOKIE, session, max_age=settings.jwt_ttl_seconds)
    response.delete_cookie(CSRF_COOKIE, path="/")
    return response


@router.post("/logout")
def logout(
    csrf_token: str = Form(""), user: CurrentUser = Depends(get_page_user)
) -> RedirectResponse:
    require_csrf(user, csrf_token)
    response = RedirectResponse("/ui/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/accounts", response_class=HTMLResponse)
def accounts_page(
    request: Request,
    user: CurrentUser = Depends(get_page_user),
    pool: ConnectionPool = Depends(get_pool),
) -> Response:
    return render(request, "accounts.html", user, accounts=ledger.list_accounts(pool, user.id))


@router.get("/transfer", response_class=HTMLResponse)
def transfer_page(
    request: Request,
    user: CurrentUser = Depends(get_page_user),
    pool: ConnectionPool = Depends(get_pool),
) -> Response:
    return render(
        request,
        "transfer.html",
        user,
        accounts=ledger.list_accounts(pool, user.id),
        idempotency_key=str(uuid4()),
    )


@router.post("/transfer", response_class=HTMLResponse)
def transfer_submit(
    request: Request,
    from_account_id: str = Form(""),
    to_email: str = Form(""),
    amount: str = Form(""),
    idempotency_key: str = Form(""),
    csrf_token: str = Form(""),
    user: CurrentUser = Depends(get_page_user),
    pool: ConnectionPool = Depends(get_pool),
) -> Response:
    require_csrf(user, csrf_token)

    status_code = 200
    ok = False
    message = ""
    source_id = parse_id(from_account_id)
    cents = parse_amount_cents(amount)
    key = idempotency_key.strip()

    if source_id is None:
        status_code, message = 422, "Choose a valid source account."
    elif cents is None:
        status_code, message = 422, "Enter a positive amount with at most two decimals."
    elif not 0 < len(key) <= 255:
        status_code, message = 422, "Missing idempotency key. Reload the form."
    else:
        try:
            destination_id = ledger.resolve_recipient_account(pool, user_id=user.id, email=to_email)
            result = ledger.execute_transfer(
                pool,
                user_id=user.id,
                idempotency_key=key,
                from_account_id=source_id,
                to_account_id=destination_id,
                amount_cents=cents,
            )
            ok = True
            note = " (already processed)" if result.replayed else ""
            message = f"Sent {format_money(result.amount_cents)}{note}."
            # A fresh key for the next transfer; reusing it would just replay this one.
            key = str(uuid4())
        except ledger.TransferError as exc:
            status_code, message = exc.status_code, str(exc).capitalize() + "."

    context = {"ok": ok, "message": message, "idempotency_key": key}
    if request.headers.get("HX-Request"):
        return render(request, "_transfer_result.html", user, status_code, oob=True, **context)
    return render(
        request,
        "transfer.html",
        user,
        status_code,
        accounts=ledger.list_accounts(pool, user.id),
        **context,
    )


@router.get("/history", response_class=HTMLResponse)
def history_page(
    request: Request,
    account_id: int | None = None,
    user: CurrentUser = Depends(get_page_user),
    pool: ConnectionPool = Depends(get_pool),
) -> Response:
    accounts = ledger.list_accounts(pool, user.id)
    selected = account_id if account_id is not None else (accounts[0]["id"] if accounts else None)
    entries = []
    if selected is not None:
        rows = ledger.list_entries(pool, user.id, selected, limit=200)
        if rows is None:
            raise HTTPException(404, "Account not found")
        entries = rows
    return render(
        request, "history.html", user, accounts=accounts, selected=selected, entries=entries
    )
