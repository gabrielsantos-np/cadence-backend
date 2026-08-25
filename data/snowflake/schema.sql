-- =============================================================================
-- GENERATED FILE — do not edit.
--
-- Produced from data/schema.sql by scripts/translate_schema.py.
-- Edit the Postgres schema and regenerate; edits here are overwritten.
--
-- Differences from the Postgres original, and why:
--   * CHECK constraints removed      — Snowflake does not support them.
--   * CREATE INDEX removed           — Snowflake has no indexes (micro-partitions).
--   * FILTER (WHERE ...)             — rewritten as CASE aggregates.
--   * DISTINCT ON                    — rewritten with QUALIFY ROW_NUMBER().
--   * BOOL_OR / STRING_AGG           — BOOLOR_AGG / LISTAGG.
--   * DATE_PART(AGE(a, b))           — DATEDIFF('month', b, a).
--   * Adjacent string literals       — joined; Snowflake has no implicit
--                                      concatenation, Postgres does.
-- Constraints that remain (PRIMARY KEY, REFERENCES) are informational in
-- Snowflake: they are not enforced. The data is already validated by the
-- Postgres load, so this costs nothing here.
-- =============================================================================

-- =============================================================================
-- Synthetic US streaming-video subscription market  ·  Jan 2024 – Jun 2026
--
-- ENTIRELY FICTIONAL. Every service, parent company, franchise, research firm
-- and figure in this dataset is invented. No real brand is referenced.
--
-- Load order: this file first, then seed_data.sql, into an empty database.
-- =============================================================================

BEGIN;

DROP VIEW IF EXISTS v_genre_competition_trend CASCADE;
DROP VIEW IF EXISTS v_revenue_per_subscriber CASCADE;
DROP VIEW IF EXISTS v_entrant_ramp CASCADE;
DROP VIEW IF EXISTS v_price_change_impact CASCADE;
DROP VIEW IF EXISTS v_competitive_sets CASCADE;
DROP VIEW IF EXISTS v_service_overview CASCADE;
DROP VIEW IF EXISTS v_genre_market CASCADE;
DROP VIEW IF EXISTS v_market_share_by_month CASCADE;

DROP TABLE IF EXISTS known_data_gaps CASCADE;
DROP TABLE IF EXISTS market_events CASCADE;
DROP TABLE IF EXISTS annual_revenue CASCADE;
DROP TABLE IF EXISTS retention_cohorts CASCADE;
DROP TABLE IF EXISTS genre_engagement CASCADE;
DROP TABLE IF EXISTS monthly_subscribers CASCADE;
DROP TABLE IF EXISTS service_genre_catalog CASCADE;
DROP TABLE IF EXISTS streaming_service CASCADE;
DROP TABLE IF EXISTS content_genre CASCADE;
DROP TABLE IF EXISTS data_sources CASCADE;

-- ---------------------------------------------------------------------------
-- Provenance
-- ---------------------------------------------------------------------------

CREATE TABLE data_sources (
    source_id     SMALLINT PRIMARY KEY,
    source_name   TEXT NOT NULL UNIQUE,
    methodology   TEXT NOT NULL,
    coverage_note TEXT NOT NULL
);

COMMENT ON TABLE data_sources IS
    'The three fictional research providers behind the fact tables. Different providers use different methodologies, so figures are not strictly comparable across sources.';

-- ---------------------------------------------------------------------------
-- Dimensions
-- ---------------------------------------------------------------------------

CREATE TABLE content_genre (
    genre_id        SMALLINT PRIMARY KEY,
    genre_name      TEXT NOT NULL UNIQUE,
    lifecycle_stage TEXT NOT NULL,
    description     TEXT NOT NULL
);

COMMENT ON TABLE content_genre IS
    'The six content categories tracked market-wide. lifecycle_stage is an analyst judgement about the category, not a computed field.';

CREATE TABLE streaming_service (
    service_id     SMALLINT PRIMARY KEY,
    service_name   TEXT NOT NULL UNIQUE,
    parent_company TEXT NOT NULL,
    launch_date    DATE NOT NULL,
    base_price_usd NUMERIC(5,2) NOT NULL,
    tier           TEXT NOT NULL
);

COMMENT ON TABLE streaming_service IS
    'One row per service. NOTE: parent_company is not unique — Bellweather Media owns two services whose subscription revenue is reported as a single bundle. See annual_revenue.is_bundled.';

COMMENT ON COLUMN streaming_service.base_price_usd IS
    'Advertised standard monthly price at the end of the window. Realised revenue per subscriber differs because of ad-supported tiers and annual discounts.';

CREATE TABLE service_genre_catalog (
    service_id         SMALLINT NOT NULL REFERENCES streaming_service (service_id),
    genre_id           SMALLINT NOT NULL REFERENCES content_genre (genre_id),
    entered_genre_on   DATE NOT NULL,
    catalog_titles     INTEGER NOT NULL,
    flagship_franchise TEXT,
    PRIMARY KEY (service_id, genre_id)
);

COMMENT ON TABLE service_genre_catalog IS
    'Which genres each service competes in. Only the two incumbents carry all six. A row here means the service has a catalogue in that genre; it does not guarantee genre_engagement rows exist for every month (see known_data_gaps).';

-- ---------------------------------------------------------------------------
-- Facts — three separate grains. Never conflate them.
-- ---------------------------------------------------------------------------

-- Grain 1: service x month. A subscriber belongs to a SERVICE, not a genre.
CREATE TABLE monthly_subscribers (
    service_id      SMALLINT NOT NULL REFERENCES streaming_service (service_id),
    month           DATE NOT NULL,
    subscribers_eom INTEGER NOT NULL,
    gross_adds      INTEGER NOT NULL,
    cancellations   INTEGER NOT NULL,
    source_id       SMALLINT NOT NULL REFERENCES data_sources (source_id),
    PRIMARY KEY (service_id, month)
);

COMMENT ON TABLE monthly_subscribers IS
    'SERVICE GRAIN. subscribers_eom = prior month subscribers_eom + gross_adds - cancellations, exactly, for every row after a service''s first observed month. The opening balance for January 2024 predates the window and is not stored.';

-- Grain 2: service x genre x month. Engagement ATTRIBUTION, not subscribers.
CREATE TABLE genre_engagement (
    service_id          SMALLINT NOT NULL REFERENCES streaming_service (service_id),
    genre_id            SMALLINT NOT NULL REFERENCES content_genre (genre_id),
    month               DATE NOT NULL,
    viewing_hours_m     NUMERIC(10,2) NOT NULL,
    engaged_subscribers INTEGER NOT NULL,
    genre_share_pct     NUMERIC(5,2) NOT NULL,
    source_id           SMALLINT NOT NULL REFERENCES data_sources (source_id),
    PRIMARY KEY (service_id, genre_id, month),
    FOREIGN KEY (service_id, genre_id)
        REFERENCES service_genre_catalog (service_id, genre_id)
);

COMMENT ON TABLE genre_engagement IS
    'ENGAGEMENT GRAIN. engaged_subscribers counts subscribers who watched anything in the genre that month. A subscriber who watches three genres is counted in all three, so these DO NOT sum to monthly_subscribers.subscribers_eom. genre_share_pct is the genre''s share of that service''s total viewing hours and sums to 100 per service-month.';

COMMENT ON COLUMN genre_engagement.viewing_hours_m IS
    'Millions of viewing hours in the month.';

CREATE TABLE retention_cohorts (
    service_id        SMALLINT NOT NULL REFERENCES streaming_service (service_id),
    quarter_start     DATE NOT NULL,
    retained_m1_pct   NUMERIC(5,2) NOT NULL,
    retained_m3_pct   NUMERIC(5,2) NOT NULL,
    retained_m6_pct   NUMERIC(5,2) NOT NULL,
    avg_tenure_months NUMERIC(5,1) NOT NULL,
    source_id         SMALLINT NOT NULL REFERENCES data_sources (source_id),
    PRIMARY KEY (service_id, quarter_start)
);

COMMENT ON TABLE retention_cohorts IS
    'Survival of the cohort that joined during the quarter, measured 1, 3 and 6 months later. Because it tracks JOINERS, it is not the inverse of the cancellation rate over the whole base.';

-- Grain 3: service x revenue line x year. Dollars, annual only.
CREATE TABLE annual_revenue (
    service_id     SMALLINT NOT NULL REFERENCES streaming_service (service_id),
    revenue_line   TEXT NOT NULL,
    fiscal_year    SMALLINT NOT NULL,
    revenue_usd_m  NUMERIC(10,2) NOT NULL,
    is_bundled     BOOLEAN NOT NULL DEFAULT FALSE,
    source_id      SMALLINT NOT NULL REFERENCES data_sources (source_id),
    PRIMARY KEY (service_id, revenue_line, fiscal_year)
);

COMMENT ON TABLE annual_revenue IS
    'REVENUE-LINE GRAIN, annual only — there is no monthly revenue anywhere. FY2026 covers January-June 2026 only (half year); do not compare it like-for-like with FY2024/FY2025.';

COMMENT ON COLUMN annual_revenue.is_bundled IS
    'TRUE means the figure is a COMBINED total for more than one service, repeated on each participating service''s row. Summing bundled rows double-counts. Report the figure once, labelled as a bundle; it cannot be split per service.';

-- ---------------------------------------------------------------------------
-- Narrative / metadata
-- ---------------------------------------------------------------------------

CREATE TABLE market_events (
    event_id   SMALLINT PRIMARY KEY,
    event_date DATE NOT NULL,
    event_type TEXT NOT NULL,
    service_id SMALLINT REFERENCES streaming_service (service_id),
    headline   TEXT NOT NULL,
    detail     TEXT NOT NULL
);

COMMENT ON TABLE market_events IS
    'Dated events that explain inflections in the fact tables. service_id is NULL for market-wide events.';

CREATE TABLE known_data_gaps (
    gap_id          SMALLINT PRIMARY KEY,
    affected_table  TEXT NOT NULL,
    affected_period TEXT NOT NULL,
    description     TEXT NOT NULL
);

COMMENT ON TABLE known_data_gaps IS
    'Deliberate, documented holes. Consult this before concluding that a zero or an empty result means "no activity" — it may mean "not measured".';

COMMIT;

-- =============================================================================
-- Analytical views — the read-only surface an analyst should start from.
-- =============================================================================

BEGIN;

-- Each service's share of total market subscribers, per month.
CREATE VIEW v_market_share_by_month AS
WITH totals AS (
    SELECT month, SUM(subscribers_eom)::BIGINT AS market_subscribers
    FROM monthly_subscribers
    GROUP BY month
)
SELECT
    ms.month,
    s.service_id,
    s.service_name,
    s.tier,
    ms.subscribers_eom,
    t.market_subscribers,
    ROUND(100.0 * ms.subscribers_eom / t.market_subscribers, 2) AS market_share_pct,
    RANK() OVER (PARTITION BY ms.month ORDER BY ms.subscribers_eom DESC)
        AS subscriber_rank
FROM monthly_subscribers ms
JOIN streaming_service s ON s.service_id = ms.service_id
JOIN totals t            ON t.month      = ms.month;

COMMENT ON VIEW v_market_share_by_month IS
    'Service grain. Share is of observed subscriptions, which double-counts people who subscribe to several services — subscriber overlap is not modelled.';

-- The genre-level market spine.
CREATE VIEW v_genre_market AS
WITH per_genre AS (
    SELECT
        ge.genre_id,
        ge.month,
        SUM(ge.viewing_hours_m)          AS total_viewing_hours_m,
        COUNT(DISTINCT ge.service_id)    AS competing_services
    FROM genre_engagement ge
    GROUP BY ge.genre_id, ge.month
),
leader AS (
    SELECT
        ge.genre_id,
        ge.month,
        s.service_name    AS leading_service,
        ge.viewing_hours_m AS leading_viewing_hours_m
    FROM genre_engagement ge
    JOIN streaming_service s ON s.service_id = ge.service_id
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ge.genre_id, ge.month
        ORDER BY ge.viewing_hours_m DESC, s.service_name
    ) = 1
)
SELECT
    g.genre_id,
    g.genre_name,
    g.lifecycle_stage,
    p.month,
    p.total_viewing_hours_m,
    p.competing_services,
    l.leading_service,
    l.leading_viewing_hours_m,
    ROUND(100.0 * l.leading_viewing_hours_m / NULLIF(p.total_viewing_hours_m, 0), 2)
        AS leader_share_pct
FROM per_genre p
JOIN content_genre g ON g.genre_id = p.genre_id
JOIN leader l        ON l.genre_id = p.genre_id AND l.month = p.month;

COMMENT ON VIEW v_genre_market IS
    'Engagement grain. Totals cover only services that report engagement in that month — the three niche services are absent before July 2024, which depresses early totals. Check known_data_gaps before reading a trend across that break.';

-- One row per service: dimension info + latest observation from each fact grain.
CREATE VIEW v_service_overview AS
WITH latest_subs AS (
    SELECT
        service_id, month AS latest_month, subscribers_eom
    FROM monthly_subscribers
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY service_id ORDER BY month DESC
    ) = 1
),
latest_ret AS (
    SELECT
        service_id, quarter_start AS latest_quarter,
        retained_m1_pct, retained_m3_pct, retained_m6_pct, avg_tenure_months
    FROM retention_cohorts
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY service_id ORDER BY quarter_start DESC
    ) = 1
),
latest_fy AS (
    SELECT service_id, MAX(fiscal_year) AS latest_fiscal_year
    FROM annual_revenue
    GROUP BY service_id
),
fy_lines AS (
    SELECT
        ar.service_id,
        ar.fiscal_year,
        SUM(CASE WHEN ar.revenue_line = 'subscriptions'
                 THEN ar.revenue_usd_m END)
            AS subscription_revenue_usd_m,
        BOOLOR_AGG(CASE WHEN ar.revenue_line = 'subscriptions'
                        THEN ar.is_bundled END)
            AS subscription_is_bundled,
        SUM(CASE WHEN ar.revenue_line <> 'subscriptions'
                 THEN ar.revenue_usd_m END)
            AS other_revenue_usd_m
    FROM annual_revenue ar
    GROUP BY ar.service_id, ar.fiscal_year
),
genres AS (
    SELECT service_id, COUNT(*) AS genre_count
    FROM service_genre_catalog
    GROUP BY service_id
)
SELECT
    s.service_id,
    s.service_name,
    s.parent_company,
    s.tier,
    s.launch_date,
    s.base_price_usd,
    g.genre_count,
    ls.latest_month,
    ls.subscribers_eom            AS latest_subscribers,
    lr.latest_quarter,
    lr.retained_m1_pct            AS latest_retained_m1_pct,
    lr.retained_m3_pct            AS latest_retained_m3_pct,
    lr.retained_m6_pct            AS latest_retained_m6_pct,
    lr.avg_tenure_months          AS latest_avg_tenure_months,
    lf.latest_fiscal_year,
    fl.subscription_revenue_usd_m,
    fl.subscription_is_bundled,
    fl.other_revenue_usd_m
FROM streaming_service s
LEFT JOIN genres      g  ON g.service_id  = s.service_id
LEFT JOIN latest_subs ls ON ls.service_id = s.service_id
LEFT JOIN latest_ret  lr ON lr.service_id = s.service_id
LEFT JOIN latest_fy   lf ON lf.service_id = s.service_id
LEFT JOIN fy_lines    fl ON fl.service_id = s.service_id
                        AND fl.fiscal_year = lf.latest_fiscal_year;

COMMENT ON VIEW v_service_overview IS
    'Mixes three grains in one row for convenience. Each column is a LATEST observation at its own cadence — monthly, quarterly, annual — so never treat the row as a single consistent period. If subscription_is_bundled is TRUE the revenue figure is shared with another service and must not be added up.';

-- Derived competitor graph: services overlapping in two or more genres.
CREATE VIEW v_competitive_sets AS
SELECT
    a.service_id                     AS service_id,
    sa.service_name                  AS service_name,
    b.service_id                     AS competitor_id,
    sb.service_name                  AS competitor_name,
    COUNT(*)                         AS shared_genres,
    LISTAGG(g.genre_name, ', ') WITHIN GROUP (ORDER BY g.genre_name)
        AS shared_genre_names
FROM service_genre_catalog a
JOIN service_genre_catalog b
     ON a.genre_id = b.genre_id
    AND a.service_id <> b.service_id
JOIN content_genre     g  ON g.genre_id    = a.genre_id
JOIN streaming_service sa ON sa.service_id = a.service_id
JOIN streaming_service sb ON sb.service_id = b.service_id
GROUP BY a.service_id, sa.service_name, b.service_id, sb.service_name
HAVING COUNT(*) >= 2;

COMMENT ON VIEW v_competitive_sets IS
    'Catalogue overlap only. It says nothing about whether the two services actually compete for the same viewers — subscriber overlap is not modelled.';

-- Cancellations and retention around each price change.
CREATE VIEW v_price_change_impact AS
WITH price_events AS (
    SELECT event_id, event_date, service_id, headline
    FROM market_events
    WHERE event_type = 'price_change' AND service_id IS NOT NULL
),
windowed AS (
    SELECT
        pe.event_id,
        pe.event_date,
        pe.service_id,
        pe.headline,
        ms.month,
        ms.cancellations,
        ms.gross_adds,
        ms.subscribers_eom,
        DATEDIFF('month', DATE_TRUNC('month', pe.event_date), ms.month)::INT
            AS months_from_event
    FROM price_events pe
    JOIN monthly_subscribers ms ON ms.service_id = pe.service_id
)
SELECT
    w.event_id,
    w.event_date,
    w.service_id,
    s.service_name,
    w.headline,
    w.month,
    w.months_from_event,
    w.cancellations,
    w.gross_adds,
    w.subscribers_eom,
    ROUND(100.0 * w.cancellations
          / NULLIF(w.subscribers_eom + w.cancellations - w.gross_adds, 0), 2)
        AS monthly_churn_pct
FROM windowed w
JOIN streaming_service s ON s.service_id = w.service_id
WHERE w.months_from_event BETWEEN -3 AND 4;

COMMENT ON VIEW v_price_change_impact IS
    'Three months before to four months after each price change. months_from_event = 0 is the month the new price took effect; the cancellation spike typically lands at +1 to +2.';

-- The new entrant's ramp, aligned to months-since-launch against every other service.
CREATE VIEW v_entrant_ramp AS
WITH first_month AS (
    SELECT service_id, MIN(month) AS first_observed_month
    FROM monthly_subscribers
    GROUP BY service_id
)
SELECT
    s.service_id,
    s.service_name,
    s.tier,
    ms.month,
    DATEDIFF('month', fm.first_observed_month, ms.month)::INT + 1
        AS month_index,
    ms.subscribers_eom,
    ms.gross_adds,
    ms.cancellations
FROM monthly_subscribers ms
JOIN streaming_service s ON s.service_id = ms.service_id
JOIN first_month fm      ON fm.service_id = ms.service_id;

COMMENT ON VIEW v_entrant_ramp IS
    'month_index is months since the service was FIRST OBSERVED in this dataset, which equals months since launch only for the entrant. For services that launched before January 2024, month_index 1 is simply January 2024.';

-- Revenue per subscriber, with retention alongside it.
CREATE VIEW v_revenue_per_subscriber AS
WITH avg_subs AS (
    SELECT
        service_id,
        EXTRACT(YEAR FROM month)::SMALLINT AS fiscal_year,
        AVG(subscribers_eom)               AS avg_subscribers,
        COUNT(*)                           AS months_observed
    FROM monthly_subscribers
    GROUP BY service_id, EXTRACT(YEAR FROM month)
),
rev AS (
    SELECT
        service_id,
        fiscal_year,
        SUM(revenue_usd_m)                                     AS total_revenue_usd_m,
        BOOLOR_AGG(is_bundled)                                 AS any_line_bundled
    FROM annual_revenue
    GROUP BY service_id, fiscal_year
),
ret AS (
    SELECT
        service_id,
        EXTRACT(YEAR FROM quarter_start)::SMALLINT AS fiscal_year,
        AVG(retained_m6_pct)                       AS avg_retained_m6_pct
    FROM retention_cohorts
    GROUP BY service_id, EXTRACT(YEAR FROM quarter_start)
)
SELECT
    s.service_id,
    s.service_name,
    s.tier,
    a.fiscal_year,
    a.avg_subscribers,
    a.months_observed,
    r.total_revenue_usd_m,
    r.any_line_bundled,
    ROUND((r.total_revenue_usd_m * 1000000.0) / NULLIF(a.avg_subscribers, 0), 2)
        AS revenue_per_subscriber_usd,
    t.avg_retained_m6_pct
FROM avg_subs a
JOIN streaming_service s ON s.service_id = a.service_id
LEFT JOIN rev r ON r.service_id = a.service_id AND r.fiscal_year = a.fiscal_year
LEFT JOIN ret t ON t.service_id = a.service_id AND t.fiscal_year = a.fiscal_year;

COMMENT ON VIEW v_revenue_per_subscriber IS
    'revenue_per_subscriber_usd is annual, not monthly. Rows where any_line_bundled is TRUE overstate the figure, because the bundled revenue covers two services but is divided by one service''s subscribers. FY2026 covers six months of revenue, so its figure is roughly half-year.';

-- How crowded each genre is getting, and what that does to each service's share.
CREATE VIEW v_genre_competition_trend AS
SELECT
    g.genre_id,
    g.genre_name,
    g.lifecycle_stage,
    ge.month,
    ge.service_id,
    s.service_name,
    s.tier,
    ge.viewing_hours_m,
    SUM(ge.viewing_hours_m) OVER (PARTITION BY ge.genre_id, ge.month)
        AS genre_total_hours_m,
    ROUND(100.0 * ge.viewing_hours_m
          / NULLIF(SUM(ge.viewing_hours_m)
                   OVER (PARTITION BY ge.genre_id, ge.month), 0), 2)
        AS share_of_genre_pct,
    COUNT(*) OVER (PARTITION BY ge.genre_id, ge.month)
        AS competing_services
FROM genre_engagement ge
JOIN content_genre     g ON g.genre_id   = ge.genre_id
JOIN streaming_service s ON s.service_id = ge.service_id;

COMMENT ON VIEW v_genre_competition_trend IS
    'Share of genre viewing hours, per service per month, with the competitor count alongside. A share that falls while absolute hours rise means the genre grew faster than the service did.';

COMMIT;
