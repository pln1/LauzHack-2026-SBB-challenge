"""FastAPI application entry point."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import health, incidents, map as map_routes, scenarios, sessions
from app.core.config import PROJECT_ROOT, get_settings
from app.core.geo import get_station_index
from app.core.logging import setup_logging
from app.db.session import dispose_engine, get_sessionmaker, init_schema
from app.services.gemini_service import get_gemini_service
from app.services.session_service import cleanup_expired

log = logging.getLogger("app")
STATIC_DIR = PROJECT_ROOT / "static"


async def _auto_ingest() -> None:
    settings = get_settings()
    from app.etl.pipeline import incident_count, run_ingest

    async with get_sessionmaker()() as db:
        if await incident_count(db) > 0:
            return
        if not Path(settings.data_dir).exists():
            log.warning("database empty and DATA_DIR missing; run scripts/ingest_data.py", extra={"data_dir": str(settings.data_dir)})
            return
        log.info("database empty: running ETL", extra={"data_dir": str(settings.data_dir)})
        await run_ingest(db, settings, get_station_index())


async def _cleanup_loop() -> None:
    interval = get_settings().session_cleanup_interval_seconds
    while True:
        try:
            async with get_sessionmaker()() as db:
                n = await cleanup_expired(db)
                if n:
                    log.info("expired sessions deleted", extra={"count": n})
        except Exception:  # noqa: BLE001
            log.exception("session cleanup failed")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    await init_schema()
    if settings.auto_ingest:
        try:
            await _auto_ingest()
        except Exception:  # noqa: BLE001 - the API should still come up
            log.exception("auto-ingest failed")
    gem = get_gemini_service().status()
    log.info("startup", extra={"gemini_configured": gem["configured"], "gemini_model": gem["model"],
                               "gemini_timeout_s": gem["timeoutSeconds"], "gemini_thinking": gem["thinkingLevel"]})
    task = asyncio.create_task(_cleanup_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await dispose_engine()


app = FastAPI(
    title="SBB Disruption Companion API",
    version="1.0.0",
    description="Turns SBB disruption data (public live ticker, internal KULT ticker, VIA) into a passenger's next move.",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in get_settings().cors_origins.split(",")],
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    if request.url.path.startswith("/api"):
        log.info("request", extra={"method": request.method, "path": request.url.path, "status": response.status_code,
                                   "ms": round((time.perf_counter() - start) * 1000, 1)})
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error", extra={"path": request.url.path})
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


for r in (health.router, incidents.router, map_routes.router, scenarios.router, sessions.router):
    app.include_router(r)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")
