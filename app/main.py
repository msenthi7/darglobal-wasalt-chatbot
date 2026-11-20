"""FastAPI server: one orchestration path per chat request (blueprint C02/C04/C05/C06/C08)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import ipaddress
import json
import logging
import math
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings
from .llm import LLMError, ModelRegistry, OpenRouterClient
from .prompts import build_messages, no_match_answer, postprocess, retrieval_query
from .ratelimit import SlidingWindowLimiter
from .retrieval import BM25Index, Document, build_index, load_corpus
from .schemas import ChatRequest, ChatResponse, ErrorBody, ErrorResponse

APP_VERSION = "1.0.0"
STATIC = Path(__file__).parent / "static"
MAX_BODY_BYTES = 32 * 1024
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
       "frame-ancestors 'self'")
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")


@dataclass
class Context:
    settings: Settings
    docs: list[Document]
    index: BM25Index
    rejected: list[str]
    meta: dict
    http: httpx.AsyncClient
    registry: ModelRegistry
    llm: OpenRouterClient
    client_minute: SlidingWindowLimiter
    client_day: SlidingWindowLimiter
    global_minute: SlidingWindowLimiter


def error_response(status: int, code: str, message: str, *, retryable: bool, request_id: str,
                   retry_after: int | None = None) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, retryable=retryable, request_id=request_id,
                                         retry_after_s=retry_after))
    headers = {"X-Request-ID": request_id, **SECURITY_HEADERS}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(status_code=status, content=body.model_dump(), headers=headers)


def client_key(request: Request) -> str:
    """Rate-limit identity. X-Forwarded-For is trusted only when the direct peer is an internal address
    (i.e. we sit behind the host's proxy). Proxies append on the right, so we take the rightmost globally
    routable entry: addresses a client injects further left cannot displace the one the edge proxy saw."""
    peer = request.client.host if request.client else ""
    ip = peer or "unknown"
    try:
        behind_proxy = not ipaddress.ip_address(peer).is_global
    except ValueError:
        behind_proxy = False
    if behind_proxy:
        forwarded = ",".join(request.headers.getlist("x-forwarded-for"))  # all header lines, not just the first
        for entry in reversed(forwarded.split(",")[-10:]):
            candidate = entry.strip().strip("[]")
            try:
                if ipaddress.ip_address(candidate).is_global:
                    ip = candidate
                    break
            except ValueError:
                continue
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6:  # one subscriber usually controls a whole /64
            ip = str(ipaddress.ip_network(f"{addr}/64", strict=False))
    except ValueError:
        pass
    return hashlib.sha256(ip.encode()).hexdigest()[:24]  # raw IPs are never stored or logged


def build_meta(settings: Settings, docs: list[Document], rejected: list[str]) -> dict:
    as_of: dict[str, dict[str, str]] = {}
    for d in docs:
        key = f"{d.site}:{d.source_type}"
        cur = as_of.setdefault(key, {"oldest": d.captured_at, "newest": d.captured_at})
        cur["oldest"] = min(cur["oldest"], d.captured_at)
        cur["newest"] = max(cur["newest"], d.captured_at)
    meta: dict = {"documents": {"total": len(docs), **Counter(f"{d.site}:{d.source_type}" for d in docs)},
                  "data_as_of": as_of, "rejected_records": len(rejected), "build": {}, "traps": {}}
    try:
        m = json.loads(settings.manifest_path.read_text("utf-8"))
        meta["build"] = {"finished_at": m.get("finished_at"), "complete": m.get("complete"),
                         "stages": {k: v.get("status") for k, v in (m.get("stages") or {}).items()},
                         "policy": m.get("policy")}
    except (OSError, ValueError, AttributeError):
        meta["build"] = {"error": "build manifest unavailable"}
    try:
        t = json.loads(settings.trap_report_path.read_text("utf-8"))
        meta["traps"] = {"summary": t.get("summary"), "by_location": t.get("by_location"),
                         "examples": [{k: f.get(k) for k in ("source", "url", "location", "pattern", "severity", "excerpt")}
                                      for f in (t.get("findings") or [])[:10]]}
    except (OSError, ValueError, AttributeError):
        meta["traps"] = {"error": "trap report unavailable"}
    return meta


def create_app(settings_factory: Callable[[], Settings] = Settings.from_env,
               transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    """Build the app. Tests inject settings and a mock OpenRouter transport (no network)."""

    async def _refresh_models(ctx: Context) -> None:
        while True:
            await asyncio.sleep(ctx.settings.model_refresh_s)
            await ctx.registry.refresh(ctx.http, ctx.settings.openrouter_base_url)


    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings = settings_factory()  # invalid configuration fails fast at startup
        docs, rejected = load_corpus(settings.corpus_path)
        if rejected:
            log.warning("rejected %d corpus records, e.g. %s", len(rejected), rejected[:3])
        index = build_index(docs)
        http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(settings.request_timeout_s, connect=settings.connect_timeout_s),
                                 limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
                                 headers={"User-Agent": "dgw-assistant/1.0"})
        registry = ModelRegistry(settings.models)
        await registry.refresh(http, settings.openrouter_base_url)
        ctx = Context(settings=settings, docs=docs, index=index, rejected=rejected,
                      meta=build_meta(settings, docs, rejected), http=http, registry=registry,
                      llm=OpenRouterClient(settings, http, registry,
                                           SlidingWindowLimiter(settings.upstream_per_day, 86_400),
                                           SlidingWindowLimiter(settings.client_upstream_per_day, 86_400)),
                      client_minute=SlidingWindowLimiter(settings.per_client_per_minute, 60),
                      client_day=SlidingWindowLimiter(settings.per_client_per_day, 86_400),
                      global_minute=SlidingWindowLimiter(settings.global_per_minute, 60))
        app.state.ctx = ctx
        log.info("ready: %d documents, %d chunks, models=%s, llm_configured=%s", len(docs), len(index.chunks),
                 registry.available, settings.llm_configured)
        task = asyncio.create_task(_refresh_models(ctx))
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await http.aclose()


    app = FastAPI(title="DarGlobal & Wasalt Assistant", version=APP_VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)


    @app.middleware("http")
    async def guard(request: Request, call_next):
        request_id = uuid.uuid4().hex[:16]
        request.state.request_id = request_id
        if request.method == "POST":
            length = request.headers.get("content-length")
            if length is None:
                return error_response(411, "length_required", "A Content-Length header is required.", retryable=False,
                                      request_id=request_id)
            if not length.isdigit() or int(length) > MAX_BODY_BYTES:
                return error_response(413, "payload_too_large", "The request body is too large.", retryable=False,
                                      request_id=request_id)
        started = time.monotonic()
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers["X-Request-ID"] = request_id
        if request.url.path.startswith("/api/"):
            log.info(json.dumps({"event": "http", "request_id": request_id, "method": request.method,
                                 "path": request.url.path, "status": response.status_code,
                                 "ms": int((time.monotonic() - started) * 1000)}))
        return response


    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = exc.errors()
        first = errors[0] if errors else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"Invalid input{' at ' + where if where else ''}: {first.get('msg', 'validation failed')}"[:200]
        return error_response(422, "invalid_input", message, retryable=False,
                              request_id=getattr(request.state, "request_id", uuid.uuid4().hex[:16]))


    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, code.replace("_", " ").capitalize() + ".", retryable=False,
                              request_id=getattr(request.state, "request_id", uuid.uuid4().hex[:16]))


    @app.exception_handler(Exception)
    async def on_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex[:16])
        ctx = getattr(request.app.state, "ctx", None)
        detail = f"{type(exc).__name__}: {exc}"
        if ctx is not None:
            detail = ctx.llm.redact(detail)
        log.error(json.dumps({"event": "unhandled", "request_id": request_id, "detail": detail[:500]}))
        return error_response(500, "internal_error", "Something went wrong on our side. Please try again.",
                              retryable=True, request_id=request_id)


    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")


    app.mount("/static", StaticFiles(directory=STATIC), name="static")


    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        ctx: Context = request.app.state.ctx
        ready = ctx.settings.llm_configured and bool(ctx.registry.available)
        return {"status": "ok" if ready else "degraded", "documents": len(ctx.docs), "chunks": len(ctx.index.chunks),
                "models_available": len(ctx.registry.available), "llm_configured": ctx.settings.llm_configured,
                "version": APP_VERSION}


    @app.get("/api/meta")
    async def meta(request: Request) -> dict:
        ctx: Context = request.app.state.ctx
        s = ctx.settings
        return {"version": APP_VERSION, **ctx.meta, "source_url": s.source_url or None,
                "models": {"available": ctx.registry.available, "verified_free": ctx.registry.verified,
                           "rejected": ctx.registry.rejected},
                "limits": {"per_client_per_minute": s.per_client_per_minute, "per_client_per_day": s.per_client_per_day,
                           "global_per_minute": s.global_per_minute, "upstream_calls_per_day": s.upstream_per_day}}


    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(body: ChatRequest, request: Request):
        ctx: Context = request.app.state.ctx
        request_id: str = request.state.request_id
        started = time.monotonic()
        client = client_key(request)
        waits = [ctx.client_minute.retry_after(client), ctx.client_day.retry_after(client),
                 ctx.global_minute.retry_after("global")]
        wait = max((w for w in waits if w is not None), default=None)
        if wait is not None:  # checked before consuming anything, so a rejected request costs nothing
            return error_response(429, "rate_limited", "Too many requests. Please wait a moment and try again.",
                                  retryable=True, request_id=request_id, retry_after=max(1, math.ceil(wait)))
        ctx.client_minute.consume(client)
        ctx.client_day.consume(client)
        ctx.global_minute.consume("global")

        hits = ctx.index.search(retrieval_query(body.message, body.history), k=6)
        if not hits:
            return ChatResponse(answer=no_match_answer(body.message), grounding="no_sources", citations=[], related=[],
                                model="none (no matching sources, so no model call was made)", fallback_used=False,
                                request_id=request_id, latency_ms=int((time.monotonic() - started) * 1000))
        try:
            messages, sent = build_messages(body.message, body.history, hits)
            result = await ctx.llm.complete(messages, request_id=request_id, client=client)
        except LLMError as exc:
            return error_response(exc.http_status, exc.code, exc.message, retryable=exc.retryable, request_id=request_id,
                                  retry_after=exc.retry_after_s)
        answer, citations, related, grounding = postprocess(result.text, sent)
        latency = int((time.monotonic() - started) * 1000)
        log.info(json.dumps({"event": "chat", "request_id": request_id, "hits": len(hits), "grounding": grounding,
                             "model": result.model, "attempts": result.attempts, "latency_ms": latency}))
        return ChatResponse(answer=answer, grounding=grounding, truncated=result.truncated, citations=citations,
                            related=related, model=result.model, fallback_used=result.fallback_used,
                            request_id=request_id, latency_ms=latency)

    return app


app = create_app()
