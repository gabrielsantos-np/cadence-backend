"""CORS is the only thing standing between this API and any page in a browser.

The frontend is a known deployment, so the allowed origins are a list, never
a wildcard.
"""

import httpx
import pytest

from cadence_backend.core.config import Settings
from cadence_backend.main import create_app

ALLOWED = "http://localhost:3000"
DISALLOWED = "http://evil.example"


@pytest.fixture
async def client():
    app = create_app(Settings(_env_file=None, frontend_origins=[ALLOWED]))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_preflight_allows_the_configured_origin(client: httpx.AsyncClient) -> None:
    response = await client.options(
        "/api/chat",
        headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED


async def test_preflight_refuses_an_unknown_origin(client: httpx.AsyncClient) -> None:
    response = await client.options(
        "/api/chat",
        headers={"Origin": DISALLOWED, "Access-Control-Request-Method": "POST"},
    )

    assert "access-control-allow-origin" not in response.headers


async def test_actual_request_from_unknown_origin_gets_no_cors_header(
    client: httpx.AsyncClient,
) -> None:
    """Without the header the browser discards the response, wildcard or not."""
    response = await client.post(
        "/api/chat", json={"question": "test"}, headers={"Origin": DISALLOWED}
    )

    assert "access-control-allow-origin" not in response.headers


async def test_allowed_origin_is_never_a_wildcard(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/chat", json={"question": "test"}, headers={"Origin": ALLOWED}
    )

    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert response.headers["access-control-allow-origin"] != "*"


async def test_credentials_are_not_allowed(client: httpx.AsyncClient) -> None:
    """No cookie auth today; enabling credentials would widen the surface."""
    response = await client.options(
        "/api/chat",
        headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST"},
    )

    assert "access-control-allow-credentials" not in response.headers
