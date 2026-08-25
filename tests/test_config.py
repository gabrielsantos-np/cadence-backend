"""Settings parsing.

`_env_file=None` isolates these from any real .env a developer has created.
"""

import pytest

from cadence_backend.core.config import Settings


def settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_boots_with_no_environment_at_all(monkeypatch) -> None:
    """Every setting has a working default; nothing is required to start."""
    for key in ("FRONTEND_ORIGINS", "OPENROUTER_API_KEY", "LOG_LEVEL", "APP_NAME"):
        monkeypatch.delenv(key, raising=False)

    s = settings()

    assert s.app_name == "Cadence API"
    assert s.frontend_origins == ["http://localhost:3000"]
    assert s.has_openrouter_key is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:3000", ["http://localhost:3000"]),
        ("http://a.test,http://b.test", ["http://a.test", "http://b.test"]),
        ("http://a.test, http://b.test", ["http://a.test", "http://b.test"]),
        ('["http://a.test","http://b.test"]', ["http://a.test", "http://b.test"]),
        ("http://a.test,,", ["http://a.test"]),
    ],
)
def test_frontend_origins_parsing(monkeypatch, raw: str, expected: list[str]) -> None:
    """The plain comma form is what .env.example documents — it must not raise.

    pydantic-settings JSON-decodes list fields before validators run unless the
    field is annotated NoDecode, which would make the documented form a
    startup crash.
    """
    monkeypatch.setenv("FRONTEND_ORIGINS", raw)

    assert Settings().frontend_origins == expected


def test_blank_api_key_is_treated_as_absent(monkeypatch) -> None:
    """`OPENROUTER_API_KEY=` in a .env must not read as a present empty key."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "")

    assert Settings().openrouter_api_key is None
    assert Settings().has_openrouter_key is False


def test_api_key_is_read_when_present(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-value")

    s = Settings()

    assert s.has_openrouter_key is True
    assert s.openrouter_api_key is not None
    assert s.openrouter_api_key.get_secret_value() == "sk-or-test-value"


def test_secrets_are_not_in_the_repr() -> None:
    """A settings object reaches logs and tracebacks; the key must not ride along."""
    s = settings(openrouter_api_key="sk-or-supersecret")

    assert "supersecret" not in repr(s)
    assert "supersecret" not in str(s)
    # Still reachable deliberately, at the call site that needs it.
    assert s.openrouter_api_key.get_secret_value() == "sk-or-supersecret"
