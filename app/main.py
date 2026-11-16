from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.auth import LoginRequired
from app.config import get_settings
from app.db import make_pool
from app.routes import api, pages


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = make_pool(get_settings().database_url)
    pool.open(wait=True, timeout=10)
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="LedgerLite", lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
    name="static",
)
app.include_router(api.router)
app.include_router(pages.router)
app.include_router(pages.root_router)


@app.exception_handler(LoginRequired)
async def redirect_to_login(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse("/ui/login", status_code=303)
