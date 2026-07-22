"""App factory — the composition root (docs/07-api.md)."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from chess_coach.api.routes import router
from chess_coach.config import AppConfig, load_config
from chess_coach.ingestion import UnknownUserError
from chess_coach.storage import open_db

_WEB_DIST = Path(__file__).resolve().parents[4] / "web" / "dist"


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Build the FastAPI app; pass a config to skip load_config()."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        cfg = load_config() if config is None else config
        app.state.cfg = cfg
        app.state.db = open_db(cfg.storage.db_path)
        yield
        app.state.db.close()

    app = FastAPI(title="AI Chess Coach", version="0.1.0", lifespan=lifespan)
    app.include_router(router, prefix="/api")
    app.add_exception_handler(UnknownUserError, _unknown_user)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    if _WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=_WEB_DIST, html=True))
    return app


async def _unknown_user(_: Request, exc: Exception) -> JSONResponse:
    return _error(404, "unknown_user", str(exc))


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    detail = exc.detail if isinstance(exc, StarletteHTTPException) else "error"
    return _error(status, f"http_{status}", str(detail))


def _error(status: int, code: str, message: str) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    return JSONResponse(status_code=status, content=body)
