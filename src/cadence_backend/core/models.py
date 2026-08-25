"""The OpenRouter model allowlist.

This is a security control, not a convenience: only ids from this list may
reach the provider, so an arbitrary client-supplied model string can never be
passed through.
"""

MODEL_OPTIONS: tuple[str, ...] = (
    "anthropic/claude-opus-5",
    "anthropic/claude-sonnet-5",
    "anthropic/claude-haiku-4.5",
)

DEFAULT_MODEL = MODEL_OPTIONS[0]


def is_known_model(model_id: str | None) -> bool:
    return model_id in MODEL_OPTIONS
