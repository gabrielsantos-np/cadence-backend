"""The analyst's chat client, behind a provider switch.

Two gateways serve the same Claude models: OpenRouter, and Anthropic directly.
Both are spoken to through the OpenAI-compatible chat-completions shape — so
the engine's function-calling code, its tool schemas and its message handling
are identical either way, and switching providers is a configuration change
rather than a rewrite.

What differs is billing and who is in the request path. `LLM_PROVIDER=anthropic`
needs a key from console.anthropic.com beginning `sk-ant-api03-`. A Claude Code
subscription credential (`sk-ant-oat01-`) is a different kind of thing: it is
scoped to Claude Code, and the API rate-limits it rather than serving it.

Adaptive thinking and the effort parameter are not exposed by this shape on
either provider, and tool calls use the function-calling schema rather than
Anthropic's native tool_use blocks.
"""

from functools import lru_cache

from openai import AsyncOpenAI

from cadence_backend.core.config import get_settings

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@lru_cache
def llm() -> AsyncOpenAI:
    """The shared client. Raises only when actually used without a key."""
    api_key, base_url, _ = get_settings().llm_credentials()
    return AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        # OpenRouter attributes traffic by these; Anthropic ignores them.
        default_headers={
            "HTTP-Referer": "http://localhost:3000",
            "X-Title": "Cadence",
        },
        max_retries=2,
        timeout=120.0,
    )


def default_model() -> str:
    """The model id for the selected provider.

    The two are not interchangeable strings: OpenRouter routes by a prefixed
    id (`anthropic/claude-opus-5`), Anthropic by a bare one
    (`claude-opus-4-5-20251101`). Sending either to the other 404s.
    """
    return get_settings().llm_credentials()[2]
