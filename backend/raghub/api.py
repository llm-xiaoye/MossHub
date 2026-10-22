"""Loopback business API and live dependency status."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing

import httpx
import redis
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse
from starlette.staticfiles import StaticFiles
from pymilvus import MilvusClient

from .config import Settings
from .db import verify_schema, open_db, system_state
from .process_locks import service_role
from .ingestion.router import router, IntakeBodyLimit
from .ingestion.store import BusinessError
from .parsing.router import router as parsing_router
from .indexing.router import router as indexing_router, deletion_engine
from .retrieval.router import router as retrieval_router
from .chat.service import ChatService
from .chat.router import router as chat_router
from .ui import router as ui_router, workflow_engine

settings = Settings.load()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    with service_role(settings, "api"):
        with closing(open_db(settings.sqlite)) as db:
            verify_schema(db, settings.project / "backend/migrations")
        retrieval = ChatService(settings)
        _app.state.retrieval = retrieval
        _app.state.chat = retrieval
        try:
            await retrieval.start()
            stopping = asyncio.Event()
            async def advance_imports():
                while not stopping.is_set():
                    await asyncio.to_thread(workflow_engine.tick)
                    await asyncio.to_thread(deletion_engine.tick)
                    try:
                        await asyncio.wait_for(stopping.wait(), timeout=1)
                    except asyncio.TimeoutError:
                        pass
            workflow_task = asyncio.create_task(advance_imports())
            try:
                yield
            finally:
                stopping.set()
                await workflow_task
        finally:
            await retrieval.close()


app = FastAPI(title="RAG Hub", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(parsing_router)
app.include_router(indexing_router)
app.include_router(retrieval_router)
app.include_router(chat_router)
app.include_router(ui_router)
app.add_middleware(IntakeBodyLimit)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-RAG-Data-Instance", "Idempotency-Key", "X-Request-ID"],
)


@app.exception_handler(BusinessError)
async def business_error(_request: Request, exc: BusinessError):
    headers = {"Retry-After": "2"} if exc.code == "SYSTEM_BUSY" else None
    return JSONResponse({"code": exc.code, "detail": str(exc), "can_retry": exc.status in {409,429,503}}, status_code=exc.status, headers=headers)


@app.middleware("http")
async def check_write_origin(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin not in {settings.frontend_origin, f"http://127.0.0.1:{settings.api_port}"}:
            return JSONResponse({"detail": "invalid local page origin"}, status_code=403)
    return await call_next(request)


def sqlite_state() -> dict:
    with closing(open_db(settings.sqlite)) as db:
        verify_schema(db, settings.project / "backend/migrations")
        state = system_state(db)
        return {
            "data_instance_id": state["data_instance_id"],
            "verification_status": state["verification_status"],
            "dispatch_open": bool(state["dispatch_open"]),
            "mode": state["mode"],
        }


def redis_ready() -> bool:
    client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
    try:
        return bool(client.ping())
    finally:
        client.close()


def milvus_ready() -> bool:
    client = MilvusClient(uri=settings.milvus_uri, timeout=5)
    try:
        client.list_collections()
        return True
    finally:
        client.close()


@app.get("/health/live")
async def live():
    return {"status": "alive"}


@app.get("/health/ready")
async def ready():
    checks: dict[str, object] = {}
    try:
        checks["sqlite"] = await asyncio.to_thread(sqlite_state)
    except Exception as exc:
        checks["sqlite"] = {"ready": False, "error_type": type(exc).__name__}
    for name, probe in (("redis", redis_ready), ("milvus", milvus_ready)):
        try:
            checks[name] = await asyncio.to_thread(probe)
        except Exception as exc:
            checks[name] = {"ready": False, "error_type": type(exc).__name__}
    try:
        async with httpx.AsyncClient(timeout=4, trust_env=False) as client:
            response = await client.get(f"http://{settings.model_host}:{settings.model_port}/health/ready")
            checks["model"] = response.status_code == 200 and response.json().get("ready") is True
    except Exception as exc:
        checks["model"] = {"ready": False, "error_type": type(exc).__name__}
    healthy = isinstance(checks.get("sqlite"), dict) and checks["sqlite"].get("verification_status") == "VERIFIED"
    healthy = healthy and all(checks.get(name) is True for name in ("redis", "milvus", "model"))
    return JSONResponse({"ready": bool(healthy), "checks": checks}, status_code=200 if healthy else 503)


# API routes precede the production bundle. No data directory is exposed.
frontend_dist = settings.project / "frontend/dist"
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
