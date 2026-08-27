"""FastAPI application for the Cadence backend."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from cadence_backend.api import chat, conversations, health
from cadence_backend.core.config import Settings, get_settings
from cadence_backend.db import close_pools
from cadence_backend.schemas.chat import ApiError, ApiErrorDetail

logger = logging.getLogger(__name__)


async def _warm_corpus() -> None:
    try:
        from cadence_backend.sources.corpus import corpus_source

        await corpus_source._index_ready()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("corpus warmup failed; search will build on first use")


DESCRIPTION = """\
The backend for Cadence, an AI market analyst.

`POST /api/chat` streams Server-Sent Events: `conversation`, `step`, `answer`,
`error`, `done`. SSE is the transport; every payload is JSON.

`GET /api/conversations` lists them; `GET /api/conversations/{id}` loads one.
"""


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    body = ApiError(error=ApiErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status_code, content=body.model_dump(by_alias=True))


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Warm the corpus index in the background. Building it takes minutes,
        # and doing it lazily means the first question that searches pays the
        # whole cost inside the analyst's turn budget. Started rather than
        # awaited so the service is answering /health immediately, and so a
        # database that is down delays search rather than blocking startup.
        warmup = asyncio.create_task(_warm_corpus())
        yield
        warmup.cancel()
        await close_pools()

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION,
        version=settings.app_version,
        lifespan=lifespan,
    )

    # Never "*": the frontend is a known deployment, and a wildcard here would
    # let any page in the browser call this API.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.frontend_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default body echoes the offending input back to the client.
        # Report which fields failed and nothing else.
        errors = exc.errors()
        if any(e["type"] == "json_invalid" for e in errors):
            return _error_response(422, "BAD_REQUEST", "Invalid JSON body.")
        fields = ", ".join(".".join(str(part) for part in e["loc"][1:]) or "body" for e in errors)
        return _error_response(422, "BAD_REQUEST", f"Invalid request body: {fields}.")

    @app.exception_handler(HTTPException)
    async def _on_http_error(_: Request, exc: HTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "BAD_REQUEST"
        if exc.status_code >= 500:
            code = "INTERNAL_ERROR"
        return _error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _on_unhandled_error(_: Request, exc: Exception) -> JSONResponse:
        # Logged in full server-side, generic to the client: exception text can
        # carry connection strings, keys and internal paths.
        logger.exception("unhandled error: %s", type(exc).__name__)
        return _error_response(500, "INTERNAL_ERROR", "Internal server error.")

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(conversations.router)

    logger.info(
        "%s %s starting (environment=%s, market_source=%s, llm=%s, analyst=%s)",
        settings.app_name,
        settings.app_version,
        settings.environment,
        settings.market_source,
        # Which gateway is actually in the path, so a surprising bill or a
        # sudden 401 has an obvious first thing to check.
        settings.llm_provider,
        "ready" if settings.has_llm_key else "no API key",
    )
    return app


app = create_app()
