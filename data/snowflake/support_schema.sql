-- =============================================================================
-- CADENCE.SUPPORT — helpdesk
--
-- Synthetic. Bellweather Media's internal helpdesk, covering ONLY the two
-- services it operates: Tidepool (5) and Harborlight (6). The market dataset
-- in the MARKET schema covers all ten services and comes from a third-party
-- provider — these two are not the same population and must not be joined as
-- if they were.
-- =============================================================================

USE SCHEMA SUPPORT;

CREATE OR REPLACE TABLE reason_code (
    code      VARCHAR NOT NULL PRIMARY KEY,
    label     VARCHAR NOT NULL,
    category  VARCHAR NOT NULL
);

-- One row per TIER-2 ESCALATION, not per customer contact. Self-service and
-- tier-1 chat deflection are not recorded anywhere, so this is a lower bound
-- on contact volume and cannot be used to compute a contact rate.
--
-- service_id is the service the customer contacted ABOUT. A subscriber who
-- holds both may raise it against either, and it is not necessarily the
-- service they went on to cancel.
CREATE OR REPLACE TABLE ticket (
    ticket_id    INTEGER NOT NULL PRIMARY KEY,
    service_id   INTEGER NOT NULL,
    opened_on    DATE    NOT NULL,
    channel      VARCHAR NOT NULL,
    reason_code  VARCHAR NOT NULL,
    -- NULL while still open.
    resolved_on  DATE,
    -- Collected on a minority of resolved tickets only.
    csat_score   INTEGER
);

CREATE OR REPLACE VIEW v_support_monthly AS
SELECT
    DATE_TRUNC('month', t.opened_on)::DATE AS month,
    t.service_id,
    s.service_name,
    r.category,
    COUNT(*)                                            AS tickets,
    COUNT(t.resolved_on)                                AS resolved_tickets,
    AVG(DATEDIFF('day', t.opened_on, t.resolved_on))    AS avg_days_to_resolve,
    COUNT(t.csat_score)                                 AS csat_responses,
    AVG(t.csat_score)                                   AS avg_csat
FROM ticket t
JOIN reason_code r ON r.code = t.reason_code
JOIN MARKET.streaming_service s ON s.service_id = t.service_id
GROUP BY 1, 2, 3, 4;
