"""OpenRouter, via its OpenAI-compatible surface.

The model is still Claude (`anthropic/claude-opus-5` by default), but routed
through OpenRouter's chat-completions shape — so adaptive thinking and the
effort parameter are not available here, and tool calls use the
function-calling schema rather than Anthropic's native tool_use blocks.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI

from cadence_backend.core.config import get_settings

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@dataclass
class Usage:
    """What one request spent, across every model call it made."""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: OpenRouter's own figure, not a local estimate from a price table that
    #: would drift the moment a model is repriced. None until a response
    #: carries one — a run is either fully costed or reports nothing.
    cost_usd: float | None = None


# A ContextVar rather than a module global: two chat requests run concurrently
# on one event loop, and a global would bill one turn for the other's tokens.
_usage: ContextVar[Usage | None] = ContextVar("llm_usage", default=None)


@contextmanager
def track_usage() -> Iterator[Usage]:
    """Accumulate usage for everything called inside this block."""
    usage = Usage()
    token = _usage.set(usage)
    try:
        yield usage
    finally:
        _usage.reset(token)


def _record(completion: Any) -> None:
    usage = _usage.get()
    raw = getattr(completion, "usage", None)
    if usage is None or raw is None:
        return
    usage.calls += 1
    usage.prompt_tokens += getattr(raw, "prompt_tokens", 0) or 0
    usage.completion_tokens += getattr(raw, "completion_tokens", 0) or 0
    # OpenRouter puts cost on the usage object when the request asks for it.
    # The OpenAI SDK does not model the field, so it lands in model_extra.
    extra = getattr(raw, "model_extra", None) or {}
    cost = extra.get("cost", getattr(raw, "cost", None))
    if cost is not None:
        usage.cost_usd = (usage.cost_usd or 0.0) + float(cost)


async def complete(client: AsyncOpenAI, **kwargs: Any) -> Any:
    """`client.chat.completions.create`, with what it cost recorded.

    Every model call in the analyst goes through here so that accounting is in
    one place rather than repeated at each call site — and so that a new call
    site cannot silently escape it.

    Asking OpenRouter to include usage costs nothing and returns its own price
    for the call, which is the only figure that stays right when a model is
    repriced.
    """
    extra_body = {**kwargs.pop("extra_body", {}), "usage": {"include": True}}
    completion = await client.chat.completions.create(**kwargs, extra_body=extra_body)
    try:
        _record(completion)
    except Exception:  # noqa: BLE001 — accounting must never fail a request
        logger.debug("could not record usage", exc_info=True)
    return completion


@lru_cache
def llm() -> AsyncOpenAI:
    """The shared client. Raises only when actually used without a key."""
    settings = get_settings()
    return AsyncOpenAI(
        api_key=settings.require_openrouter_api_key(),
        base_url=OPENROUTER_BASE_URL,
        default_headers={
            "HTTP-Referer": "http://localhost:3000",
            "X-Title": "Cadence",
        },
        max_retries=2,
        timeout=120.0,
    )


def default_model() -> str:
    return get_settings().openrouter_model
