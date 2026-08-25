"""The /api/conversations contract.

The database is deliberately unconfigured in tests (see conftest), so these
assert the shape of the contract and that failures stay inside the error
envelope rather than leaking a traceback.
"""

import httpx


async def test_list_without_a_database_returns_the_error_envelope(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/conversations")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


async def test_errors_never_leak_internals(client: httpx.AsyncClient) -> None:
    """A connection string or traceback must not reach the client."""
    body = (await client.get("/api/conversations")).text

    assert "postgres://" not in body
    assert "Traceback" not in body
    assert "asyncpg" not in body


async def test_by_id_route_exists_and_is_not_the_list_route(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/conversations/b4c3f1f4-0000-4000-8000-000000000000")

    # Reaches the handler (and fails on the database) rather than 404ing as an
    # unknown route — which is what a missing path parameter would look like.
    assert response.status_code == 500


def test_openapi_documents_both_conversation_routes() -> None:
    from cadence_backend.main import app

    paths = app.openapi()["paths"]

    assert "/api/conversations" in paths
    assert "/api/conversations/{conversation_id}" in paths


def test_conversation_routes_omit_null_optionals() -> None:
    """An absent optional must be omitted, never serialised as null.

    The frontend guards charts with `marker !== undefined`, so a null marker
    slips through and then crashes on `marker.x`. The streaming path already
    dumps with exclude_none; these routes have to match it.
    """
    from cadence_backend.main import app

    for route in app.routes:
        for sub in getattr(route, "routes", [route]):
            if getattr(sub, "path", "").startswith("/api/conversations"):
                assert sub.response_model_exclude_none is True, sub.path
