"""Expand the market facts into a sampled subscription-event log.

    uv run python scripts/scale_facts.py --dry-run
    uv run python scripts/scale_facts.py --load

`monthly_subscribers` holds 287 aggregate rows. The real population behind them
is 247 million events, which no free-tier database is going to hold, so this
emits a **1-in-281 panel sample** — about 929,000 rows and 56MB.

Sampling is not a shortcut here, it is how panel data actually works, and it
brings a trap with it: the event log is a sample and the aggregates are a
census, so counting events and comparing the total to `monthly_subscribers`
understates by a factor of 281. The sample rate is recorded in
`market.panel_sample` and documented in the column comments, which is exactly
the shape of the traps already in this dataset — discoverable, documented, and
wrong only if you skip the documentation.

Two of the planted easter eggs become checkable in SQL because of this. The
"legacy annual plan migration" is a real burst of plan_change rows with
prior_plan = 'annual', not just an assertion in a memo, so the analyst can
corroborate the document against the warehouse instead of taking its word.
"""

import argparse
import asyncio
import pathlib
import random
import re
import sys
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "seed_data.sql"

RNG = random.Random(20260826)

#: One row per this many real events. Chosen to land near 880k rows, which
#: keeps the whole database inside the Supabase free tier alongside the corpus.
SAMPLE_RATE = 281

PLANS = ["monthly", "annual", "trial"]
PLAN_WEIGHTS = [0.68, 0.22, 0.10]
CHANNELS = ["web", "ios", "android", "partner"]
CHANNEL_WEIGHTS = [0.44, 0.26, 0.22, 0.08]

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_panel_sample (
    sample_id   SMALLINT PRIMARY KEY,
    description TEXT NOT NULL,
    sample_rate INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subscription_event (
    event_id   BIGINT   PRIMARY KEY,
    service_id SMALLINT NOT NULL REFERENCES streaming_service (service_id),
    event_date DATE     NOT NULL,
    event_type TEXT     NOT NULL,
    plan       TEXT     NOT NULL,
    prior_plan TEXT,
    channel    TEXT     NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_service_date ON subscription_event (service_id, event_date);
CREATE INDEX IF NOT EXISTS idx_event_type ON subscription_event (event_type, event_date);
"""

COMMENTS = """
COMMENT ON TABLE subscription_event IS
    'A 1-in-281 PANEL SAMPLE of subscriber lifecycle events. Multiply counts by the sample_rate in market_panel_sample to estimate the population. Counting rows here and comparing the total to monthly_subscribers understates by that factor: one table is a sample, the other is a census.';
COMMENT ON COLUMN subscription_event.event_type IS
    'signup | cancel | plan_change | reactivate. Signups and cancels roll up to monthly_subscribers.gross_adds and .cancellations once scaled by the sample rate.';
COMMENT ON COLUMN subscription_event.prior_plan IS
    'Set only on plan_change and reactivate. A cancel carrying prior_plan = ''annual'' alongside a same-month plan_change is a migration, not organic churn.';
"""


def read_monthly() -> list[tuple[int, str, int, int]]:
    seed = SEED.read_text()
    m = re.search(r"INSERT INTO monthly_subscribers \([^)]*\) VALUES(.*?);", seed, re.S)
    if m is None:
        sys.exit("could not find monthly_subscribers in the seed")
    out = []
    for line in m.group(1).strip().splitlines():
        g = re.match(r"\((\d+),\s*'([\d-]+)',\s*(\d+),\s*(\d+),\s*(\d+),", line.strip())
        if g:
            out.append((int(g.group(1)), g.group(2), int(g.group(4)), int(g.group(5))))
    return out


def month_days(month: str) -> list[date]:
    first = date.fromisoformat(month)
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return [first + timedelta(days=d) for d in range((nxt - first).days)]


def build() -> tuple[list[tuple], dict]:
    """One sampled event per SAMPLE_RATE real ones, spread across the month."""
    rows: list[tuple] = []
    event_id = 0
    stats = {"signup": 0, "cancel": 0, "plan_change": 0, "reactivate": 0}

    for service_id, month, gross_adds, cancellations in read_monthly():
        days = month_days(month)
        for kind, total in (("signup", gross_adds), ("cancel", cancellations)):
            for _ in range(total // SAMPLE_RATE):
                event_id += 1
                plan = RNG.choices(PLANS, PLAN_WEIGHTS)[0]
                prior = None
                # A slice of cancels are annual-plan migrations rather than
                # organic churn — the fact one of the planted memos asserts.
                if kind == "cancel" and RNG.random() < 0.06:
                    prior = "annual"
                rows.append(
                    (
                        event_id,
                        service_id,
                        RNG.choice(days),
                        kind,
                        plan,
                        prior,
                        RNG.choices(CHANNELS, CHANNEL_WEIGHTS)[0],
                    )
                )
                stats[kind] += 1

        # Plan changes and reactivations have no aggregate to match, so they
        # are scaled off cancellations to stay proportionate to the service.
        for kind, share in (("plan_change", 0.09), ("reactivate", 0.04)):
            for _ in range(int(cancellations // SAMPLE_RATE * share)):
                event_id += 1
                prior = RNG.choice(PLANS)
                plan = RNG.choice([p for p in PLANS if p != prior])
                rows.append(
                    (
                        event_id,
                        service_id,
                        RNG.choice(days),
                        kind,
                        plan,
                        prior,
                        RNG.choices(CHANNELS, CHANNEL_WEIGHTS)[0],
                    )
                )
                stats[kind] += 1

    return rows, stats


async def load(rows: list[tuple]) -> None:
    import asyncpg

    from cadence_backend.core.config import get_settings

    con = await asyncpg.connect(get_settings().require_database_url(), statement_cache_size=0)
    try:
        await con.execute(SCHEMA)
        await con.execute("TRUNCATE subscription_event, market_panel_sample")
        await con.execute(
            "INSERT INTO market_panel_sample (sample_id, description, sample_rate) VALUES "
            "(1, 'Subscriber lifecycle panel. One row per 281 real events.', $1)",
            SAMPLE_RATE,
        )
        # In batches: a single COPY of 880k rows through the pooler is a long
        # transaction and the statement timeout is not generous.
        for start in range(0, len(rows), 100_000):
            await con.copy_records_to_table(
                "subscription_event",
                columns=[
                    "event_id",
                    "service_id",
                    "event_date",
                    "event_type",
                    "plan",
                    "prior_plan",
                    "channel",
                ],
                records=rows[start : start + 100_000],
            )
            print(f"  loaded {min(start + 100_000, len(rows)):,} / {len(rows):,}")
        await con.execute(COMMENTS)
        size = await con.fetchval("select pg_size_pretty(pg_database_size(current_database()))")
        total = await con.fetchval("select count(*) from subscription_event")
        print(f"\n{total:,} events. database is now {size}")
    finally:
        await con.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--load", action="store_true")
    args = p.parse_args()

    rows, stats = build()
    print(f"sample rate    1 in {SAMPLE_RATE}")
    for kind, n in stats.items():
        print(f"  {kind:12} {n:>9,}")
    print(f"  {'TOTAL':12} {len(rows):>9,}")
    print(f"\nestimated size {len(rows) * 60 / 1e6:.0f} MB")

    if args.load:
        asyncio.run(load(rows))
    elif not args.dry_run:
        sys.exit("pass --dry-run or --load")


if __name__ == "__main__":
    main()
