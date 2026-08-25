"""Conversation persistence.

Two tables, `app.conversation` and `app.message`. The message payload is JSONB
on purpose: the answer-block vocabulary can evolve without a migration, so
nothing here needs to know what a block looks like.

This connects as the schema owner. The analyst's role cannot read `app.*` at
all, which is what stops a prompt-injected query from reaching chat history.
"""

import json
from typing import Any
from uuid import uuid4

from cadence_backend.db import app_pool
from cadence_backend.schemas.conversation import (
    Conversation,
    EngineMessage,
    HistoryEntry,
    Message,
    UserMessage,
)


async def list_conversations(limit: int = 40) -> list[HistoryEntry]:
    pool = await app_pool()
    rows = await pool.fetch(
        """SELECT id, title, updated_at
             FROM app.conversation
            ORDER BY updated_at DESC
            LIMIT $1""",
        limit,
    )
    return [
        HistoryEntry(
            id=str(row["id"]),
            title=row["title"],
            timestamp=row["updated_at"].isoformat(),
        )
        for row in rows
    ]


async def create_conversation(title: str) -> str:
    conversation_id = str(uuid4())
    pool = await app_pool()
    await pool.execute(
        "INSERT INTO app.conversation (id, title) VALUES ($1::uuid, $2)",
        conversation_id,
        title,
    )
    return conversation_id


async def rename_conversation(conversation_id: str, title: str) -> None:
    pool = await app_pool()
    await pool.execute(
        "UPDATE app.conversation SET title = $2 WHERE id = $1::uuid",
        conversation_id,
        title,
    )


def _payload(row: Any) -> dict[str, Any]:
    """asyncpg hands back JSONB as text unless a codec is registered."""
    raw = row["payload"]
    return json.loads(raw) if isinstance(raw, str) else raw


def _to_message(row: Any) -> Message:
    payload = _payload(row)
    if row["role"] == "user":
        return UserMessage(id=str(row["id"]), text=payload.get("text", ""))
    steps = payload.get("steps") or []
    return EngineMessage(
        id=str(row["id"]),
        status="done",
        steps=steps,
        revealed=len(steps),
        blocks=payload.get("blocks") or [],
        elapsed_ms=payload.get("elapsedMs"),
    )


async def load_conversation(conversation_id: str) -> Conversation | None:
    pool = await app_pool()
    head = await pool.fetchrow(
        "SELECT id, title, updated_at FROM app.conversation WHERE id = $1::uuid",
        conversation_id,
    )
    if head is None:
        return None

    rows = await pool.fetch(
        """SELECT id, role, payload FROM app.message
            WHERE conversation_id = $1::uuid ORDER BY seq""",
        conversation_id,
    )
    return Conversation(
        id=str(head["id"]),
        title=head["title"],
        timestamp=head["updated_at"].isoformat(),
        messages=[_to_message(row) for row in rows],
    )


async def append_message(
    conversation_id: str,
    role: str,
    payload: dict[str, Any],
) -> str:
    """Append one message and bump updated_at, in a single transaction."""
    message_id = str(uuid4())
    pool = await app_pool()
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            """INSERT INTO app.message (id, conversation_id, seq, role, payload)
               VALUES ($1::uuid, $2::uuid,
                       COALESCE((SELECT MAX(seq) + 1 FROM app.message
                                  WHERE conversation_id = $2::uuid), 1),
                       $3, $4::jsonb)""",
            message_id,
            conversation_id,
            role,
            json.dumps(payload),
        )
        await connection.execute(
            "UPDATE app.conversation SET updated_at = NOW() WHERE id = $1::uuid",
            conversation_id,
        )
    return message_id


async def prior_turns(conversation_id: str, limit: int = 6) -> list[dict[str, str]]:
    """The last few turns, flattened for the model's context on a follow-up."""
    pool = await app_pool()
    rows = await pool.fetch(
        """SELECT role, payload FROM app.message
            WHERE conversation_id = $1::uuid
            ORDER BY seq DESC LIMIT $2""",
        conversation_id,
        limit,
    )

    turns: list[dict[str, str]] = []
    for row in reversed(rows):
        payload = _payload(row)
        if row["role"] == "user":
            content = payload.get("text", "")
        else:
            # Only the prose survives into context — replaying whole result
            # sets would crowd out the current question for little benefit.
            blocks = payload.get("blocks") or []
            content = "\n\n".join(
                b.get("text", "")
                for b in blocks
                if isinstance(b, dict) and b.get("type") in ("text", "bottomLine")
            )
            content = content or "(answered)"
        if content.strip():
            turns.append(
                {"role": "user" if row["role"] == "user" else "assistant", "content": content}
            )
    return turns
