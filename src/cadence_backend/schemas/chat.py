"""The /api/chat wire contract: the request, and the five SSE event payloads.

SSE is the transport; every payload is JSON. The field names are camelCase
because that is what the browser client consumes.
"""

from typing import Literal

from pydantic import ConfigDict, field_validator

from cadence_backend.core.models import is_known_model
from cadence_backend.schemas.answer import AnswerBlock
from cadence_backend.schemas.base import CamelModel
from cadence_backend.schemas.trace import TraceStep

#: Every event name the stream may emit, in the order a successful run
#: produces them. `error` is emitted in addition to `answer`, never instead
#: of it, and `done` is always last.
EVENT_NAMES: tuple[str, ...] = ("conversation", "step", "answer", "error", "done")


class ChatRequest(CamelModel):
    # Forgiving on input: an extra field from a newer frontend should not be a
    # 422. The outgoing models stay strict.
    model_config = ConfigDict(
        alias_generator=CamelModel.model_config["alias_generator"],
        populate_by_name=True,
        extra="ignore",
    )

    question: str
    #: Absent or null means "create a new conversation for this question".
    conversation_id: str | None = None
    #: Silently dropped unless it is on the allowlist — an arbitrary
    #: client-supplied model string must never reach the provider.
    model: str | None = None

    @field_validator("question")
    @classmethod
    def _require_question(cls, value: str) -> str:
        question = value.strip()
        if not question:
            raise ValueError("A question is required.")
        return question

    @field_validator("model")
    @classmethod
    def _allowlist_model(cls, value: str | None) -> str | None:
        return value if is_known_model(value) else None


class ConversationEvent(CamelModel):
    """Always the first event: the conversation this turn belongs to."""

    id: str
    is_new: bool


class StepEvent(CamelModel):
    """One trace step, streamed as the analyst produces it."""

    step: TraceStep
    #: Wall-clock since the run started — not the step's own duration, which
    #: lives on `step.durationMs`.
    elapsed_ms: int | None = None


class AnswerEvent(CamelModel):
    """The composed answer. Emitted even when the run failed."""

    blocks: list[AnswerBlock]
    elapsed_ms: int | None = None
    #: What this turn cost, as OpenRouter billed it — not a local estimate from
    #: a price table that would drift the moment a model is repriced. Absent
    #: when the gateway did not report one, so a figure here is always real.
    cost_usd: float | None = None
    #: Prompt + completion tokens across every model call in the turn.
    tokens: int | None = None


class ErrorEvent(CamelModel):
    message: str


class DoneEvent(CamelModel):
    """Terminator. Carries no payload; serialises as `{}`."""


class ApiErrorDetail(CamelModel):
    code: str
    message: str


class ApiError(CamelModel):
    """The response body for failures that happen before the stream starts."""

    error: ApiErrorDetail


ErrorCode = Literal["BAD_REQUEST", "NOT_FOUND", "INTERNAL_ERROR"]
