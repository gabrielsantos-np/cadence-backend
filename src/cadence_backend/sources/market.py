"""The synthetic streaming-market dataset.

Connects as `analyst_ro`, a role Postgres restricts to SELECT on the market
tables. The privilege boundary lives in the database because the analyst writes
its own SQL — see data/app_schema.sql.
"""

from typing import Literal

from cadence_backend.db import SqlOutcome, analyst_pool, run_read_only_query
from cadence_backend.sources.market_schema import MARKET_SCHEMA_CONTEXT

#: Only the Postgres copy has these. The Snowflake source must not advertise
#: them, or the model will spend turns querying tables that are not there.
POSTGRES_ONLY = """
## Event-grain tables (this source only)

- subscription_event(event_id, service_id, event_date, event_type, plan, prior_plan, channel)
    A 1-IN-281 PANEL SAMPLE of subscriber lifecycle events: signup, cancel,
    plan_change, reactivate. Multiply counts by market_panel_sample.sample_rate
    to estimate the population. Counting rows here and comparing the total to
    monthly_subscribers understates by that factor — one is a sample, the other
    is a census.
    A cancel carrying prior_plan = 'annual' alongside a same-month plan_change
    is a plan migration, not organic churn.
- market_panel_sample(sample_id, description, sample_rate)
    The sample rate. Read it rather than hardcoding 281.

- mv_event_monthly(service_id, month, event_type, sampled_events, estimated_events, estimated_from_annual)
    PREFER THIS over aggregating subscription_event directly: it is
    pre-aggregated and roughly three thousand times faster. estimated_events is
    already scaled by the sample rate and is directly comparable with
    monthly_subscribers; sampled_events is the raw count and is not.
"""


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
    schema_context = MARKET_SCHEMA_CONTEXT + POSTGRES_ONLY

    async def query(self, sql: str) -> SqlOutcome:
        return await run_read_only_query(await analyst_pool(), sql)


market_source = MarketSource()
