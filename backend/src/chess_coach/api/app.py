"""App factory — the composition root (docs/07-api.md)."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from chess_coach.api.routes import router
from chess_coach.api.runs import AnalysisRun
from chess_coach.coach import CoachProvider, create_provider
from chess_coach.config import AppConfig, load_config
from chess_coach.engine import create_pool
from chess_coach.ingestion import UnknownUserError
from chess_coach.openings import load_opening_book
from chess_coach.storage import open_db

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WEB_DIST = _REPO_ROOT / "web" / "dist"
_DEFAULT_BOOK_DIR = _REPO_ROOT / "vendor" / "chess-openings"
_DEFAULT_ENGINE_BIN = _REPO_ROOT / "engines" / "stockfish" / "src" / "stockfish"


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Build the FastAPI app; pass a config to skip load_config()."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        cfg = load_config() if config is None else config
        app.state.cfg = cfg
        app.state.db = open_db(cfg.storage.db_path)
        app.state.book = load_opening_book(cfg.openings.book_dir or _DEFAULT_BOOK_DIR)
        engine_bin = cfg.engine.bin_path or _DEFAULT_ENGINE_BIN
        # No binary is not fatal: everything but analysis still works.
        app.state.pool = (
            await create_pool(engine_bin, cfg.engine.workers)
            if engine_bin.exists()
            else None
        )
        # One provider per configured agent; a misconfigured agent
        # fails startup (fail fast, consistent with config).
        providers: dict[str, CoachProvider] = {
            agent.id: create_provider(agent, cfg.anthropic_api_key)
            for agent in cfg.coach.agents
        }
        app.state.providers = providers
        runs: dict[str, AnalysisRun] = {}
        app.state.runs = runs
        # Uvicorn's own URL line scrolls away on reloads; print a
        # fresh clickable link each time the app (re)starts.
        print(f"\nAI Chess Coach → http://localhost:{cfg.server.port}\n", flush=True)
        yield
        tasks = [run.task for run in runs.values() if run.task is not None]
        for task in tasks:
            task.cancel()
        # Await the cancellations before touching the pool/DB: a task can
        # be mid-`analyse` when cancelled, and pool.close() below calls
        # engine.quit() on the same workers — without this, teardown races
        # a still-unwinding task (GUIDELINES.md: every asyncio.Task is
        # awaited or tracked and cancelled on shutdown).
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if app.state.pool is not None:
            await app.state.pool.close()
        app.state.db.close()

    app = FastAPI(title="AI Chess Coach", version="0.1.0", lifespan=lifespan)
    app.include_router(router, prefix="/api")
    app.add_exception_handler(UnknownUserError, _unknown_user)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    if _WEB_DIST.is_dir():
        # SPA fallback: real files are served as-is, every other
        # non-API path gets index.html so client-side routes survive
        # refreshes and deep links.
        app.add_api_route("/{path:path}", _spa, include_in_schema=False)
    return app


async def _spa(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")
    candidate = (_WEB_DIST / path).resolve()
    if path and candidate.is_relative_to(_WEB_DIST) and candidate.is_file():
        # Built assets are content-hashed, so let the browser cache them.
        return FileResponse(candidate)
    # index.html is the one unhashed entry point: must-revalidate so a
    # fresh build (new asset hashes) is picked up on a normal reload
    # instead of a stale cached shell.
    return FileResponse(_WEB_DIST / "index.html", headers={"Cache-Control": "no-cache"})


async def _unknown_user(_: Request, exc: Exception) -> JSONResponse:
    return _error(404, "unknown_user", str(exc))


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    status = exc.status_code if isinstance(exc, StarletteHTTPException) else 500
    detail = exc.detail if isinstance(exc, StarletteHTTPException) else "error"
    return _error(status, f"http_{status}", str(detail))


def _error(status: int, code: str, message: str) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
    return JSONResponse(status_code=status, content=body)
