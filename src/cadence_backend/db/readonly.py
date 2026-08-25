"""The read-only query path used by SQL sources.

The validator here is defence in depth, never the guarantee. The real boundary
is the `analyst_ro` role in data/app_schema.sql: SELECT on market data and
nothing else, `app.*` denied, and a statement timeout set at the role level. Do
not widen a connection's privileges to make a query work — fix the query.
"""

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import asyncpg

#: Rows returned to the model and rendered in the trace.
ROW_LIMIT = 50

#: Statements the analyst is never allowed to run, checked before the database
#: sees them.
#:
#: The Snowflake entries are not optional extras. Snowflake has no read-only
#: transaction mode, and its SQL can contain `USE ROLE`, so a statement that
#: switched role would otherwise look like an ordinary read. The dedicated
#: least-privilege login is what actually prevents escalation; this list is
#: what stops a confusing error before it reaches the driver.
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum"
    r"|reindex|call|do|set|reset|listen|notify|prepare|deallocate|lock"
    # Snowflake
    r"|use|merge|put|unload|undrop|execute)\b",
    re.IGNORECASE,
)

_STARTS_READ = re.compile(r"^(select|with)\b", re.IGNORECASE)


@dataclass(frozen=True)
class SqlOutcome:
    columns: list[str]
    rows: list[list[str]]
    row_count: int
    truncated: bool
    duration_ms: int


def assert_read_only(sql: str) -> None:
    """Reject anything that is not a single read.

    This exists so a bad query fails with a message the model can act on rather
    than a Postgres permission error, and so multi-statement injection never
    reaches the driver.
    """
    trimmed = re.sub(r";\s*$", "", sql.strip())
    if not trimmed:
        raise ValueError("Empty query.")
    if ";" in trimmed:
        raise ValueError("Only one statement per query. Remove the extra `;`.")
    if not _STARTS_READ.match(trimmed):
        raise ValueError("Only SELECT and WITH queries are allowed.")
    if FORBIDDEN.search(trimmed):
        raise ValueError("This query contains a statement that is not read-only.")


def render(value: Any) -> str:
    """Stringify one cell, which is why SqlStep.rows is a list of strings."""
    if value is None:
        return "NULL"
    if isinstance(value, datetime | date):
        return value.isoformat()[:10]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


async def run_read_only_query(pool: asyncpg.Pool, sql: str) -> SqlOutcome:
    """Run one read-only query and shape it for the model and the trace."""
    assert_read_only(sql)
    started = time.monotonic()

    async with pool.acquire() as connection:
        # Explicit read-only transaction: even if the role's default is
        # changed, this statement cannot write.
        async with connection.transaction(readonly=True):
            records = await connection.fetch(sql)

    duration_ms = int((time.monotonic() - started) * 1000)
    columns = list(records[0].keys()) if records else []
    all_rows = [[render(row[column]) for column in columns] for row in records]

    return SqlOutcome(
        columns=columns,
        rows=all_rows[:ROW_LIMIT],
        row_count=len(all_rows),
        truncated=len(all_rows) > ROW_LIMIT,
        duration_ms=duration_ms,
    )
