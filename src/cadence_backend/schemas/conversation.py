"""The conversation shell returned by /api/conversations.

`timestamp` is an ISO-8601 string, not a pre-rendered "Tue, 2:25pm":
formatting a date for a human is a presentation concern and belongs in the
client, which knows the viewer's locale and timezone.
"""

from typing import Annotated, Literal

from pydantic import Field

from cadence_backend.schemas.answer import AnswerBlock
from cadence_backend.schemas.base import CamelModel
from cadence_backend.schemas.trace import TraceStep


class HistoryEntry(CamelModel):
    id: str
    title: str
    #: ISO-8601 UTC, e.g. "2026-08-25T12:25:00+00:00".
    timestamp: str


class UserMessage(CamelModel):
    id: str
    role: Literal["user"] = "user"
    text: str


class EngineMessage(CamelModel):
    id: str
    role: Literal["engine"] = "engine"
    status: Literal["running", "done"] = "done"
    steps: list[TraceStep]
    #: How many steps are visible — drives the streaming reveal.
    revealed: int
    blocks: list[AnswerBlock]
    #: Wall-clock, measured server-side. Deliberately not the sum of step
    #: durations: almost all the elapsed time is model latency between calls.
    elapsed_ms: int | None = None


Message = Annotated[UserMessage | EngineMessage, Field(discriminator="role")]


class Conversation(CamelModel):
    id: str
    title: str
    timestamp: str
    messages: list[Message]


class ConversationList(CamelModel):
    conversations: list[HistoryEntry]
