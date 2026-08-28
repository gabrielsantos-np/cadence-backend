"""Application settings, read from the environment.

Nothing here may require a secret to be present: /health has to answer even
when the analyst cannot run, so a missing key surfaces at the point of use
rather than stopping the service from starting.
"""

import json
from contextlib import suppress
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Cadence API"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"

    # Browser origins allowed to call this API. Never "*" — the frontend is a
    # known deployment, not an open one.
    #
    # NoDecode is load-bearing: without it pydantic-settings JSON-decodes a
    # list field before any validator runs, so the plain comma-separated form
    # below would fail at startup rather than being parsed.
    frontend_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # Optional on purpose. Absent means the analyst cannot run; it does not
    # mean the service is unhealthy.
    #
    # SecretStr so the value cannot ride along in a repr() — settings objects
    # end up in log lines and tracebacks.
    openrouter_api_key: SecretStr | None = None
    openrouter_model: str = "anthropic/claude-opus-5"

    # Two connections, deliberately. The app owns conversation storage and
    # connects as the schema owner; the analyst gets a role Postgres restricts
    # to reads, because the analyst writes its own SQL. Never point both at the
    # same role to "make a query work" — see data/app_schema.sql.
    #
    # SecretStr: a connection string carries a password.
    database_url: SecretStr | None = None
    analyst_database_url: SecretStr | None = None

    # Used only by scripts/load_dataset.py, to set the read-only role's
    # password when creating it. Never used to serve a request.
    analyst_db_password: SecretStr | None = None

    # Embeddings. OpenRouter serves /embeddings as well as chat, so this
    # falls back to OPENROUTER_API_KEY and needs no second key — set
    # EMBEDDING_API_KEY only to point somewhere else.
    embedding_api_key: SecretStr | None = None
    embedding_base_url: str = "https://openrouter.ai/api/v1"
    #: Prefixed, because OpenRouter routes by provider.
    embedding_model: str = "openai/text-embedding-3-small"

    # Which SQL warehouses the analyst can reach.
    #
    #   postgres  — Supabase only: the panel event log (929k rows), the
    #               materialised monthly rollup and the market census.
    #   snowflake — Snowflake only: the same market census plus FINANCE and
    #               SUPPORT, which exist nowhere else.
    #   both      — both registered at once. The two warehouses hold different
    #               halves of the story: the events and the corpus are on
    #               Supabase, the ledger and the helpdesk are on Snowflake, and
    #               questions that need both can only be answered here.
    #
    # Conversation storage and the document corpus are always Supabase, whatever
    # this is set to — they are read through a different pool.
    market_source: Literal["postgres", "snowflake", "both"] = "postgres"

    # Snowflake, used when market_source is "snowflake".
    #
    # ACCOUNT is the identifier, not the hostname: the "xy12345.region" part of
    # the console URL, without ".snowflakecomputing.com".
    snowflake_account: str | None = None
    snowflake_user: str | None = None
    snowflake_password: SecretStr | None = None
    snowflake_database: str = "CADENCE"
    snowflake_schema: str = "MARKET"
    snowflake_warehouse: str = "COMPUTE_WH"

    # The role the ANALYST connects with: SELECT on the market schema and
    # nothing else. Same principle as analyst_ro on Postgres — the privilege
    # boundary is the warehouse, not this application.
    snowflake_role: str = "CADENCE_ANALYST_RO"

    # Admin credentials, used only by scripts/setup_snowflake.py to create the
    # database, role and grants. Never used to serve a request.
    snowflake_admin_user: str | None = None
    snowflake_admin_password: SecretStr | None = None
    snowflake_admin_role: str = "ACCOUNTADMIN"

    @field_validator("frontend_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept a comma-separated string so .env stays readable.

        A JSON array still works too, since NoDecode turns off the decoding
        pydantic-settings would otherwise do for us.
        """
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                with suppress(json.JSONDecodeError):
                    return json.loads(text)
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return value

    @field_validator("openrouter_api_key", "embedding_api_key", mode="before")
    @classmethod
    def _blank_key_is_absent(cls, value: object) -> object:
        """`OPENROUTER_API_KEY=` in a .env means absent, not an empty key."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("database_url", "analyst_database_url", "analyst_db_password", mode="before")
    @classmethod
    def _blank_url_is_absent(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def has_openrouter_key(self) -> bool:
        return self.openrouter_api_key is not None

    def require_database_url(self) -> str:
        return _require(self.database_url, "DATABASE_URL")

    def require_analyst_database_url(self) -> str:
        return _require(self.analyst_database_url, "ANALYST_DATABASE_URL")

    def require_openrouter_api_key(self) -> str:
        return _require(self.openrouter_api_key, "OPENROUTER_API_KEY")

    def require_embedding_api_key(self) -> str:
        """The embeddings key, falling back to the OpenRouter one.

        They are the same account by default. Keeping the setting separate
        still allows pointing embeddings at another provider without moving
        the analyst's chat traffic with it.
        """
        if self.embedding_api_key is not None:
            return self.embedding_api_key.get_secret_value()
        return _require(self.openrouter_api_key, "OPENROUTER_API_KEY or EMBEDDING_API_KEY")

    def snowflake_analyst_connect_args(self) -> dict[str, str]:
        """Connection arguments for the analyst's read-only Snowflake role."""
        return {
            "account": _require_str(self.snowflake_account, "SNOWFLAKE_ACCOUNT"),
            "user": _require_str(self.snowflake_user, "SNOWFLAKE_USER"),
            "password": _require(self.snowflake_password, "SNOWFLAKE_PASSWORD"),
            "role": self.snowflake_role,
            "warehouse": self.snowflake_warehouse,
            "database": self.snowflake_database,
            "schema": self.snowflake_schema,
        }

    def snowflake_admin_connect_args(self) -> dict[str, str]:
        """Connection arguments for the setup script. Not used to serve requests."""
        return {
            "account": _require_str(self.snowflake_account, "SNOWFLAKE_ACCOUNT"),
            "user": _require_str(self.snowflake_admin_user, "SNOWFLAKE_ADMIN_USER"),
            "password": _require(self.snowflake_admin_password, "SNOWFLAKE_ADMIN_PASSWORD"),
            "role": self.snowflake_admin_role,
            "warehouse": self.snowflake_warehouse,
        }


def _require_str(value: str | None, name: str) -> str:
    if not value:
        raise RuntimeError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def _require(value: SecretStr | None, name: str) -> str:
    """Read a required secret, or fail with a message that says what to do.

    Called at the point of use rather than at startup, so the service still
    boots and answers /health when a dependency is not configured.
    """
    if value is None:
        raise RuntimeError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
