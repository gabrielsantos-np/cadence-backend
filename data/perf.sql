-- =============================================================================
-- Make the analyst's SQL fast.
--
-- The analyst writes its own queries, so it cannot be told to use an index.
-- What it can be given is a schema where the obvious query is already the fast
-- one. Measured before any of this: a monthly rollup over 929k events took
-- 1,448ms, and a question typically runs three to eight queries.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- The rollup the analyst reaches for constantly: events by service by month.
-- A materialised view turns a 929k-row aggregation into a 287-row lookup.
--
-- Deliberately carries the scaled estimate alongside the raw count. The event
-- log is a 1-in-281 panel sample and monthly_subscribers is a census, so
-- comparing a raw count to it understates by that factor — precomputing the
-- corrected figure removes the trap from the path of least resistance rather
-- than leaving it as a footnote nobody reads.
-- ---------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS mv_event_monthly;

CREATE MATERIALIZED VIEW mv_event_monthly AS
SELECT
    e.service_id,
    date_trunc('month', e.event_date)::DATE      AS month,
    e.event_type,
    COUNT(*)                                     AS sampled_events,
    COUNT(*) * s.sample_rate                     AS estimated_events,
    COUNT(*) FILTER (WHERE e.prior_plan = 'annual') * s.sample_rate
                                                 AS estimated_from_annual
FROM subscription_event e
CROSS JOIN (SELECT sample_rate FROM market_panel_sample WHERE sample_id = 1) s
GROUP BY e.service_id, 2, e.event_type, s.sample_rate;

CREATE UNIQUE INDEX idx_mv_event_monthly
    ON mv_event_monthly (service_id, month, event_type);

COMMENT ON MATERIALIZED VIEW mv_event_monthly IS
    'Pre-aggregated subscription_event by service, month and type. estimated_events is already scaled by the panel sample rate, so it is directly comparable with monthly_subscribers; sampled_events is the raw row count and is not.';

-- ---------------------------------------------------------------------------
-- BRIN rather than btree on the date. The table was loaded in date order, so
-- physically adjacent rows share a month and a block-range summary is enough.
-- A few kilobytes where a btree over 929k rows is tens of megabytes, and the
-- database is already at 433MB of a 500MB tier.
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_event_date_brin
    ON subscription_event USING BRIN (event_date) WITH (pages_per_range = 32);

-- A covering index on (service_id, event_date) INCLUDE (...) was tried here and
-- removed: 44MB for a single scan, and it made the point lookup 2.7x slower by
-- offering the planner a wider, colder alternative to an index that already fit.
--
-- Partial index for the question actually asked of this table — cancellations
-- carrying a prior plan are how the "migration, not churn" claim is checked.
CREATE INDEX IF NOT EXISTS idx_event_annual_cancels
    ON subscription_event (service_id, event_date)
    WHERE event_type = 'cancel' AND prior_plan IS NOT NULL;

COMMIT;

-- Outside the transaction: ANALYZE cannot run inside one, and the planner
-- needs fresh statistics or it will keep choosing the plans it chose before
-- any of the above existed.
ANALYZE subscription_event;
ANALYZE mv_event_monthly;
