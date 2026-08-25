"""The health endpoint is what a deployment health check calls."""

import httpx


async def test_health_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_needs_no_api_key(client: httpx.AsyncClient, monkeypatch) -> None:
    """The service must stay healthy without an OpenRouter key."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    response = await client.get("/health")

    assert response.status_code == 200
