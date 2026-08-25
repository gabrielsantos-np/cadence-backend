-- =============================================================================
-- CADENCE.FINANCE — general ledger
--
-- Synthetic. Bellweather Media's internal ERP, covering ONLY the two
-- services it operates: Tidepool (5) and Harborlight (6). The market dataset
-- in the MARKET schema covers all ten services and comes from a third-party
-- provider — these two are not the same population and must not be joined as
-- if they were.
-- =============================================================================

USE SCHEMA FINANCE;

-- Bellweather's fiscal year runs April to March: April 2024 falls in FY2025.
-- The market provider reports on calendar years, so the two never align.
CREATE OR REPLACE TABLE fiscal_calendar (
    period_month    DATE    NOT NULL PRIMARY KEY,
    fiscal_year     INTEGER NOT NULL,
    fiscal_quarter  INTEGER NOT NULL,
    -- 'open' periods are unaudited and may still move.
    period_status   VARCHAR NOT NULL
);

CREATE OR REPLACE TABLE gl_account (
    account_code    VARCHAR NOT NULL PRIMARY KEY,
    account_name    VARCHAR NOT NULL,
    statement_line  VARCHAR NOT NULL,
    normal_balance  VARCHAR NOT NULL
);

-- One row per service, month and account. Amounts are in DOLLARS, unlike
-- MARKET.annual_revenue which is in millions.
CREATE OR REPLACE TABLE gl_entry (
    entry_id        INTEGER      NOT NULL PRIMARY KEY,
    period_month    DATE         NOT NULL,
    service_id      INTEGER      NOT NULL,
    account_code    VARCHAR      NOT NULL,
    amount_usd      NUMBER(16,2) NOT NULL,
    is_restated     BOOLEAN      NOT NULL
);

-- The gold view. Joins the conformed service dimension out of CADENCE.MARKET,
-- which is legal because both schemas live in one Snowflake database.
CREATE OR REPLACE VIEW v_finance_monthly_pl AS
SELECT
    e.period_month,
    c.fiscal_year,
    c.fiscal_quarter,
    c.period_status,
    e.service_id,
    s.service_name,
    SUM(CASE WHEN e.account_code = '4000' THEN e.amount_usd END) AS gross_subscription_usd,
    SUM(CASE WHEN e.account_code = '4900' THEN e.amount_usd END) AS refunds_usd,
    SUM(CASE WHEN e.account_code IN ('4000', '4900') THEN e.amount_usd END)
        AS net_subscription_usd,
    SUM(CASE WHEN e.account_code = '4100' THEN e.amount_usd END) AS advertising_usd,
    SUM(CASE WHEN e.account_code = '4200' THEN e.amount_usd END) AS addon_usd,
    SUM(CASE WHEN e.account_code = '5000' THEN e.amount_usd END) AS content_cost_usd,
    SUM(CASE WHEN e.account_code = '6000' THEN e.amount_usd END) AS marketing_cost_usd,
    SUM(CASE WHEN e.account_code IN ('4000', '4900', '4100', '4200') THEN e.amount_usd END)
      - SUM(CASE WHEN e.account_code IN ('5000', '6000') THEN e.amount_usd END)
        AS contribution_usd,
    BOOLOR_AGG(e.is_restated) AS any_line_restated
FROM gl_entry e
JOIN fiscal_calendar c ON c.period_month = e.period_month
JOIN MARKET.streaming_service s ON s.service_id = e.service_id
GROUP BY e.period_month, c.fiscal_year, c.fiscal_quarter, c.period_status,
         e.service_id, s.service_name;
