"""OpenRouter, via its OpenAI-compatible surface.

The model is still Claude (`anthropic/claude-opus-5` by default), but routed
through OpenRouter's chat-completions shape — so adaptive thinking and the
effort parameter are not available here, and tool calls use the
function-calling schema rather than Anthropic's native tool_use blocks.
"""

from functools import lru_cache

from openai import AsyncOpenAI

from cadence_backend.core.config import get_settings

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


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
