"""The market dataset on Snowflake.

Connects as a dedicated user whose only role holds SELECT on the market schema.
Snowflake has no equivalent of Postgres's `default_transaction_read_only` or
`BEGIN TRANSACTION READ ONLY`, so read-only here *is* the absence of write
grants — see scripts/setup_snowflake.py. The validator in db/readonly.py stays
defence in depth, never the guarantee.
"""

import asyncio
import logging
import time
from typing import Any, Literal

import snowflake.connector

from cadence_backend.core.config import get_settings
from cadence_backend.db.readonly import ROW_LIMIT, SqlOutcome, assert_read_only, render
from cadence_backend.sources.snowflake_schema import SNOWFLAKE_SCHEMA_CONTEXT

logger = logging.getLogger(__name__)

_connection: Any = None


def _connect() -> Any:
    """One lazily-created connection, reused across queries.

    The driver is synchronous and not safe to share across threads, so every
    query is serialised through a single connection on a worker thread. The
    analyst issues a handful of queries per question, so this is not a
    bottleneck; a pool would be.
    """
    global _connection
    if _connection is None or _connection.is_closed():
        settings = get_settings()
        _connection = snowflake.connector.connect(
            **settings.snowflake_analyst_connect_args(),
            client_session_keep_alive=True,
            login_timeout=30,
        )
    return _connection


def _query_blocking(sql: str) -> SqlOutcome:
    started = time.monotonic()
    cursor = _connect().cursor()
    try:
        cursor.execute(sql)
        records = cursor.fetchall()
        columns = [c[0] for c in cursor.description]
    finally:
        cursor.close()

    duration_ms = int((time.monotonic() - started) * 1000)
    all_rows = [[render(value) for value in row] for row in records]
    return SqlOutcome(
        columns=columns,
        rows=all_rows[:ROW_LIMIT],
        row_count=len(all_rows),
        truncated=len(all_rows) > ROW_LIMIT,
        duration_ms=duration_ms,
    )


class SnowflakeSource:
    id = "market"
    kind: Literal["sql"] = "sql"
    name = "Market dataset"
    description = (
        "The US streaming-subscription market: services, genres, monthly subscribers, "
        "genre engagement, retention cohorts, annual revenue, and market events, "
        "Jan 2024 - Jun 2026."
    )
    trace_label = "Queried market dataset (Snowflake)"
    schema_context = SNOWFLAKE_SCHEMA_CONTEXT

    async def query(self, sql: str) -> SqlOutcome:
        assert_read_only(sql)
        # The connector blocks; keep it off the event loop so one slow query
        # cannot stall the SSE stream for every other request.
        return await asyncio.to_thread(_query_blocking, sql)


snowflake_source = SnowflakeSource()


def close_connection() -> None:
    global _connection
    if _connection is not None and not _connection.is_closed():
        _connection.close()
    _connection = None
