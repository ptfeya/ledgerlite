from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from app import ledger
from app.auth import CurrentUser, authenticate, get_api_user, get_pool
from app.config import get_settings
from app.security import create_token

router = APIRouter()


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class AccountOut(BaseModel):
    id: int
    name: str
    currency: str
    balance_cents: int


class TransferRequest(BaseModel):
    from_account_id: int
    to_account_id: int
    amount_cents: int = Field(gt=0, le=ledger.MAX_AMOUNT_CENTS)


class TransferOut(BaseModel):
    id: UUID
    from_account_id: int
    to_account_id: int
    amount_cents: int
    created_at: datetime


class EntryOut(BaseModel):
    id: int
    transfer_id: UUID
    account_id: int
    amount_cents: int
    created_at: datetime


@router.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, pool: ConnectionPool = Depends(get_pool)) -> TokenResponse:
    user_id = authenticate(pool, body.email, body.password)
    if user_id is None:
        raise HTTPException(401, "Invalid credentials")
    settings = get_settings()
    return TokenResponse(
        access_token=create_token(user_id, settings.jwt_secret, settings.jwt_ttl_seconds)
    )


@router.get("/accounts", response_model=list[AccountOut])
def accounts(
    user: CurrentUser = Depends(get_api_user), pool: ConnectionPool = Depends(get_pool)
) -> list[dict]:
    return ledger.list_accounts(pool, user.id)


@router.post("/transfers", response_model=TransferOut, status_code=201)
def create_transfer(
    body: TransferRequest,
    response: Response,
    idempotency_key: str = Header(min_length=1, max_length=255),
    user: CurrentUser = Depends(get_api_user),
    pool: ConnectionPool = Depends(get_pool),
) -> ledger.TransferResult:
    try:
        result = ledger.execute_transfer(
            pool,
            user_id=user.id,
            idempotency_key=idempotency_key,
            from_account_id=body.from_account_id,
            to_account_id=body.to_account_id,
            amount_cents=body.amount_cents,
        )
    except ledger.TransferError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if result.replayed:
        response.headers["Idempotent-Replay"] = "true"
    return result


@router.get("/accounts/{account_id}/entries", response_model=list[EntryOut])
def entries(
    account_id: int,
    limit: int = Query(100, ge=1, le=500),
    user: CurrentUser = Depends(get_api_user),
    pool: ConnectionPool = Depends(get_pool),
) -> list[dict]:
    rows = ledger.list_entries(pool, user.id, account_id, limit)
    if rows is None:
        raise HTTPException(404, "Account not found")
    return rows


@router.post("/reconcile")
def reconcile(
    _: CurrentUser = Depends(get_api_user), pool: ConnectionPool = Depends(get_pool)
) -> dict:
    imbalance = ledger.ledger_imbalance(pool)
    if imbalance == 0:
        return {"status": "ok"}
    return {"status": "imbalance", "imbalance_cents": imbalance}
