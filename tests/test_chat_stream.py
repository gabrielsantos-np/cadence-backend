"""The /api/chat contract: an SSE stream, not its internals.

These tests deliberately assert the wire format the frontend consumes, so the
analyst can be swapped in later without them changing.
"""

import json

import httpx
import pytest

from cadence_backend.schemas.chat import EVENT_NAMES


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs, validating the framing."""
    frames = []
    for raw in body.split("\n\n"):
        block = raw.strip("\n")
        if not block:
            continue
        lines = block.split("\n")
        assert lines[0].startswith("event: "), f"frame is missing an event name: {block!r}"
        assert lines[1].startswith("data: "), f"frame is missing a data line: {block!r}"
        frames.append((lines[0][len("event: ") :], json.loads(lines[1][len("data: ") :])))
    return frames


async def test_chat_streams_event_stream_content_type(client: httpx.AsyncClient) -> None:
    async with client.stream("POST", "/api/chat", json={"question": "test"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # Both defeat proxy buffering, which would break streaming in transit.
        assert response.headers["cache-control"] == "no-cache, no-transform"
        assert response.headers["x-accel-buffering"] == "no"
        await response.aread()


async def test_chat_emits_valid_sse_frames(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/chat", json={"question": "test"})
    frames = parse_sse(response.text)

    assert frames, "the stream produced no frames"
    for event, _ in frames:
        assert event in EVENT_NAMES, f"unknown event name: {event}"


async def test_chat_terminates_with_done(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/chat", json={"question": "test"})
    frames = parse_sse(response.text)

    assert frames[-1] == ("done", {})


async def test_chat_accepts_a_conversation_id_and_model(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/chat",
        json={
            "question": "Which service leads on revenue?",
            "conversationId": "b4c3f1f4-0000-4000-8000-000000000000",
            "model": "anthropic/claude-opus-5",
        },
    )

    assert response.status_code == 200
    assert parse_sse(response.text)[-1][0] == "done"


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="missing-question"),
        pytest.param({"question": ""}, id="empty-question"),
        pytest.param({"question": "   "}, id="blank-question"),
        pytest.param({"question": 42}, id="wrong-type"),
    ],
)
async def test_chat_rejects_invalid_requests(client: httpx.AsyncClient, body: dict) -> None:
    response = await client.post("/api/chat", json=body)

    assert response.status_code == 422
    payload = response.json()
    assert payload["error"]["code"] == "BAD_REQUEST"
    assert "message" in payload["error"]


async def test_chat_rejects_malformed_json(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/chat",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "BAD_REQUEST"


async def test_chat_error_response_leaks_nothing(client: httpx.AsyncClient) -> None:
    """The validation error must not echo the submitted value back."""
    response = await client.post("/api/chat", json={"question": "s3cr3t-value-xyz"})
    assert response.status_code == 200

    bad = await client.post("/api/chat", json={"question": ""})
    assert "s3cr3t" not in bad.text


async def test_malformed_json_message_is_specific(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/chat",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )

    assert response.json()["error"]["message"] == "Invalid JSON body."
