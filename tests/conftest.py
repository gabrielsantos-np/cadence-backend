"""Shared test fixtures.

The environment is neutralised *before* the app is imported. Without this the
suite picks up a developer's real .env and the chat tests call OpenRouter and
Postgres for real — slow, costly, and dependent on whoever is running them.
Explicit environment variables win over .env in pydantic-settings, and the
settings validators read an empty value as "not configured".
"""

import os

os.environ["OPENROUTER_API_KEY"] = ""
os.environ["DATABASE_URL"] = ""
os.environ["ANALYST_DATABASE_URL"] = ""
os.environ["FRONTEND_ORIGINS"] = "http://localhost:3000"

import httpx  # noqa: E402
import pytest  # noqa: E402

from cadence_backend.main import app  # noqa: E402


@pytest.fixture
async def client():
    """An HTTP client speaking to the app in-process.

    httpx over ASGI rather than TestClient: TestClient buffers the whole
    response body, which would hide whether the SSE endpoint streams at all.
    """
    # raise_app_exceptions=False so a 500 arrives as a response, the way a
    # real HTTP client sees it. Starlette sends the error response and then
    # re-raises so the server can log it; without this the exception would
    # surface here instead of the JSON body under test.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
