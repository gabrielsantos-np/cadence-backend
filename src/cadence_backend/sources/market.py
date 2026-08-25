"""The synthetic streaming-market dataset.

Connects as `analyst_ro`, a role Postgres restricts to SELECT on the market
tables. The privilege boundary lives in the database because the analyst writes
its own SQL — see data/app_schema.sql.
"""

from typing import Literal

from cadence_backend.db import SqlOutcome, analyst_pool, run_read_only_query
from cadence_backend.sources.market_schema import MARKET_SCHEMA_CONTEXT


class MarketSource:
    id = "market"
    kind: Literal["sql"] = "sql"
    name = "Market dataset"
    description = (
        "The US streaming-subscription market: services, genres, monthly subscribers, "
        "genre engagement, retention cohorts, annual revenue, and market events, "
        "Jan 2024 - Jun 2026."
    )
    trace_label = "Queried market dataset"
    schema_context = MARKET_SCHEMA_CONTEXT

    async def query(self, sql: str) -> SqlOutcome:
        return await run_read_only_query(await analyst_pool(), sql)


market_source = MarketSource()
