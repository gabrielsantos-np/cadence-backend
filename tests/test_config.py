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


# --------------------------------------------------------------------------
# Warehouse registration. The two SQL sources were built as drop-in
# replacements and both answered to the id "market", which is precisely why
# they could not be registered together.
# --------------------------------------------------------------------------


def test_market_source_accepts_both() -> None:
    assert settings(market_source="both").market_source == "both"


def test_each_mode_registers_the_expected_warehouses(monkeypatch) -> None:
    import cadence_backend.core.config as config_module
    import cadence_backend.sources as sources_module

    expected = {
        "postgres": ["market"],
        "snowflake": ["bellweather"],
        "both": ["market", "bellweather"],
    }
    for mode, ids in expected.items():
        monkeypatch.setattr(config_module, "get_settings", lambda m=mode: settings(market_source=m))
        monkeypatch.setattr(sources_module, "get_settings", config_module.get_settings)
        assert [s.id for s in sources_module._market_sources()] == ids, mode


def test_the_two_warehouses_have_distinct_ids() -> None:
    """Colliding ids would make find_sql_source return whichever came first."""
    from cadence_backend.sources.market import market_source
    from cadence_backend.sources.snowflake import snowflake_source

    assert market_source.id != snowflake_source.id
