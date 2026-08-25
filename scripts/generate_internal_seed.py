"""Regenerate the FINANCE and SUPPORT schemas from the market dataset.

    uv run python scripts/generate_internal_seed.py

Run it after any change to data/seed_data.sql. The internal figures are derived
from the market ones — gross billings track subscriber counts and prices, ticket
volume tracks cancellations — so editing the market seed without regenerating
leaves the two telling different stories, which is precisely the disagreement
the analyst is meant to detect as real.


Both schemas belong to Bellweather Media, which operates Tidepool (5) and
Harborlight (6). Every figure is derived from data/seed_data.sql so the
internal systems and the market provider tell a consistent story — including
where they deliberately disagree.

Deterministic: a fixed PRNG seed, so re-running produces identical files.
"""

import pathlib
import random
import re
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "snowflake"

RNG = random.Random(20260825)

OURS = {5: "Tidepool", 6: "Harborlight"}

# --------------------------------------------------------------------------
# read what the market dataset already asserts
# --------------------------------------------------------------------------

seed = (ROOT / "data" / "seed_data.sql").read_text()


def rows(table: str) -> list[str]:
    body = re.search(rf"INSERT INTO {table} \([^)]*\) VALUES(.*?);\s*$", seed, re.S | re.M)
    if body is None:
        body = re.search(rf"INSERT INTO {table} \([^)]*\) VALUES(.*?);", seed, re.S)
    return [ln.strip().rstrip(",") for ln in body.group(1).strip().splitlines() if ln.strip()]


subs: dict[tuple[int, str], dict] = {}
for line in rows("monthly_subscribers"):
    m = re.match(r"\((\d+),\s*'([\d-]+)',\s*(\d+),\s*(\d+),\s*(\d+),", line)
    if m and int(m.group(1)) in OURS:
        sid, month = int(m.group(1)), m.group(2)
        subs[(sid, month)] = {
            "eom": int(m.group(3)),
            "gross_adds": int(m.group(4)),
            "cancellations": int(m.group(5)),
        }

months = sorted({m for _, m in subs})
assert len(months) == 30, months

# Provider figures we must stay close to (millions USD, subscriptions bundled).
MARKET_SUBS_REVENUE_M = {2024: 1848.28, 2025: 2044.35, 2026: 1097.30}
MARKET_ADVERTISING_M = {
    (5, 2024): 139.55,
    (5, 2025): 164.05,
    (5, 2026): 82.41,
    (6, 2024): 63.96,
    (6, 2025): 67.46,
    (6, 2026): 37.52,
}
MARKET_ADDONS_M = {
    (5, 2024): 59.83,
    (5, 2025): 66.42,
    (5, 2026): 38.22,
    (6, 2024): 20.42,
    (6, 2025): 20.91,
    (6, 2026): 10.44,
}

# --------------------------------------------------------------------------
# FINANCE
# --------------------------------------------------------------------------

ACCOUNTS = [
    ("4000", "Subscription revenue", "revenue", "credit"),
    ("4100", "Advertising revenue", "revenue", "credit"),
    ("4200", "Add-on revenue", "revenue", "credit"),
    ("4900", "Refunds and credits", "revenue", "debit"),
    ("5000", "Content amortisation", "cost_of_revenue", "debit"),
    ("6000", "Marketing spend", "opex", "debit"),
]

# End-of-window list prices are 9.99 and 8.99; both ramped over the window.
PRICE_START = {5: 9.29, 6: 8.29}
PRICE_END = {5: 9.99, 6: 8.99}


def price(sid: int, i: int) -> float:
    return PRICE_START[sid] + (PRICE_END[sid] - PRICE_START[sid]) * (i / (len(months) - 1))


def fiscal(month: str) -> tuple[int, int]:
    """Bellweather's fiscal year runs April to March. Apr 2024 is FY2025 Q1."""
    y, m = int(month[:4]), int(month[5:7])
    fy = y + 1 if m >= 4 else y
    fq = ((m - 4) % 12) // 3 + 1
    return fy, fq


# Raw gross billings per service-month, then scaled so the calendar-year total
# lands near the provider's bundled figure. The factor differs per year on
# purpose: a constant offset would let the analyst infer one from the other.
raw = {
    (sid, mo): subs[(sid, mo)]["eom"] * price(sid, i) for sid in OURS for i, mo in enumerate(months)
}

GROSS_FACTOR = {2024: 0.997, 2025: 1.004, 2026: 0.991}
scale: dict[int, float] = {}
for year, target_m in MARKET_SUBS_REVENUE_M.items():
    got = sum(v for (sid, mo), v in raw.items() if mo.startswith(str(year)))
    scale[year] = (target_m * 1_000_000 * GROSS_FACTOR[year]) / got

gl: list[tuple] = []
entry_id = 1
for sid in OURS:
    for mo in months:
        year = int(mo[:4])
        n_months_in_year = sum(1 for x in months if x.startswith(str(year)))
        gross = raw[(sid, mo)] * scale[year]

        # Refunds run 3.5-5.5% of gross and climb slightly with cancellations.
        churn_pressure = subs[(sid, mo)]["cancellations"] / max(subs[(sid, mo)]["eom"], 1)
        refund_rate = 0.035 + churn_pressure * 1.4 + RNG.uniform(-0.003, 0.003)

        advertising = MARKET_ADVERTISING_M[(sid, year)] * 1_000_000 / n_months_in_year
        addons = MARKET_ADDONS_M[(sid, year)] * 1_000_000 / n_months_in_year
        advertising *= RNG.uniform(0.93, 1.07)
        addons *= RNG.uniform(0.94, 1.06)

        net_revenue = gross * (1 - refund_rate) + advertising + addons
        content = net_revenue * RNG.uniform(0.33, 0.38)
        marketing = net_revenue * RNG.uniform(0.10, 0.14)
        # Marketing runs hot in the two months after a subscriber loss.
        if subs[(sid, mo)]["cancellations"] > subs[(sid, mo)]["gross_adds"]:
            marketing *= RNG.uniform(1.15, 1.35)

        # Harborlight restated Feb/Mar 2025 after a content-amortisation error.
        restated = sid == 6 and mo in ("2025-02-01", "2025-03-01")

        for code, amount in (
            ("4000", gross),
            ("4100", advertising),
            ("4200", addons),
            ("4900", -gross * refund_rate),
            ("5000", content),
            ("6000", marketing),
        ):
            gl.append((entry_id, mo, sid, code, round(amount, 2), restated))
            entry_id += 1

# --------------------------------------------------------------------------
# SUPPORT
# --------------------------------------------------------------------------

MIGRATION = "2024-07-01"
support_months = [m for m in months if m >= MIGRATION]

REASONS = [
    ("BILLING_DISPUTE", "Billing dispute or unexpected charge", "billing"),
    ("PRICE_QUERY", "Question about a price change", "billing"),
    ("CANCEL_REQUEST", "Request to cancel or downgrade", "account"),
    ("LOGIN_ACCESS", "Cannot sign in or account locked", "account"),
    ("PLAYBACK_QUALITY", "Buffering, quality or playback failure", "technical"),
    ("DEVICE_SETUP", "Device or app setup problem", "technical"),
    ("CONTENT_MISSING", "Title unavailable or removed", "content"),
    ("OTHER", "Uncategorised escalation", "other"),
]
REASON_WEIGHTS = [0.14, 0.06, 0.19, 0.12, 0.21, 0.11, 0.12, 0.05]
CHANNELS = ["chat", "phone", "email"]
CHANNEL_WEIGHTS = [0.52, 0.31, 0.17]

tickets: list[tuple] = []
ticket_id = 1
for sid in OURS:
    baseline = 38 if sid == 5 else 27
    for mo in support_months:
        c = subs[(sid, mo)]
        # Escalations track net subscriber loss, not total contacts.
        pressure = c["cancellations"] / max(c["gross_adds"], 1)
        volume = int(baseline * (0.75 + pressure * 0.9) * RNG.uniform(0.85, 1.15))
        y, m = int(mo[:4]), int(mo[5:7])
        days = (date(y + (m == 12), (m % 12) + 1, 1) - date(y, m, 1)).days

        for _ in range(volume):
            opened = date(y, m, RNG.randint(1, days))
            reason = RNG.choices(REASONS, REASON_WEIGHTS)[0][0]
            channel = RNG.choices(CHANNELS, CHANNEL_WEIGHTS)[0]

            # A tenth stay open; the rest close within three weeks.
            if RNG.random() < 0.10:
                resolved, days_open = "NULL", None
            else:
                days_open = max(0, int(RNG.lognormvariate(0.9, 0.8)))
                days_open = min(days_open, 21)
                d = opened.toordinal() + days_open
                resolved = f"'{date.fromordinal(d).isoformat()}'"

            # CSAT is only collected on a minority of resolved tickets.
            if resolved != "NULL" and RNG.random() < 0.31:
                base = 4.1 if reason not in ("BILLING_DISPUTE", "CANCEL_REQUEST") else 3.2
                if days_open is not None and days_open > 7:
                    base -= 0.9
                csat = max(1, min(5, int(round(RNG.gauss(base, 1.0)))))
            else:
                csat = "NULL"

            tickets.append(
                (
                    ticket_id,
                    sid,
                    f"'{opened.isoformat()}'",
                    f"'{channel}'",
                    f"'{reason}'",
                    resolved,
                    csat,
                )
            )
            ticket_id += 1

# --------------------------------------------------------------------------
# emit
# --------------------------------------------------------------------------

HEADER = """\
-- =============================================================================
-- {title}
--
-- Synthetic. Bellweather Media's internal {system}, covering ONLY the two
-- services it operates: Tidepool (5) and Harborlight (6). The market dataset
-- in the MARKET schema covers all ten services and comes from a third-party
-- provider — these two are not the same population and must not be joined as
-- if they were.
-- =============================================================================

"""


def values(rowlist, per_line=1) -> str:
    return ",\n".join("  (" + ", ".join(str(v) for v in r) + ")" for r in rowlist)


finance_schema = (
    HEADER.format(title="CADENCE.FINANCE — general ledger", system="ERP")
    + """\
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
"""
)

cal = []
for mo in months:
    fy, fq = fiscal(mo)
    status = "open" if mo >= "2026-05-01" else "closed"
    cal.append((f"'{mo}'", fy, fq, f"'{status}'"))

finance_seed = (
    HEADER.format(title="CADENCE.FINANCE — seed data", system="ERP")
    + "USE SCHEMA FINANCE;\n\nBEGIN;\n\n"
    + "INSERT INTO fiscal_calendar\n"
    "  (period_month, fiscal_year, fiscal_quarter, period_status) VALUES\n"
    + values(cal)
    + ";\n\n"
    + "INSERT INTO gl_account\n"
    "  (account_code, account_name, statement_line, normal_balance) VALUES\n"
    + values([(f"'{a}'", f"'{b}'", f"'{c}'", f"'{d}'") for a, b, c, d in ACCOUNTS])
    + ";\n\n"
    + "INSERT INTO gl_entry\n"
    "  (entry_id, period_month, service_id, account_code, amount_usd, is_restated) VALUES\n"
    + values(
        [
            (i, f"'{mo}'", sid, f"'{code}'", amt, "TRUE" if r else "FALSE")
            for i, mo, sid, code, amt, r in gl
        ]
    )
    + ";\n\nCOMMIT;\n"
)

support_schema = (
    HEADER.format(title="CADENCE.SUPPORT — helpdesk", system="helpdesk")
    + """\
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
"""
)

support_seed = (
    HEADER.format(title="CADENCE.SUPPORT — seed data", system="helpdesk")
    + "USE SCHEMA SUPPORT;\n\nBEGIN;\n\n"
    + "INSERT INTO reason_code (code, label, category) VALUES\n"
    + values([(f"'{a}'", f"'{b}'", f"'{c}'") for a, b, c in REASONS])
    + ";\n\n"
    + "INSERT INTO ticket\n"
    "  (ticket_id, service_id, opened_on, channel, reason_code, resolved_on, csat_score)"
    " VALUES\n" + values(tickets) + ";\n\nCOMMIT;\n"
)

for name, text in (
    ("finance_schema.sql", finance_schema),
    ("finance_seed.sql", finance_seed),
    ("support_schema.sql", support_schema),
    ("support_seed.sql", support_seed),
):
    (OUT / name).write_text(text)
    print(f"{name:22} {len(text):>9,} bytes")

print(f"\ngl_entry rows   {len(gl):,}")
print(f"ticket rows     {len(tickets):,}")
print(f"support window  {support_months[0]} .. {support_months[-1]}")
for year in (2024, 2025, 2026):
    got = sum(a for _, mo, _, code, a, _ in gl if code == "4000" and mo.startswith(str(year)))
    print(
        f"  GL 4000 {year}: {got / 1e6:8.2f}M vs market {MARKET_SUBS_REVENUE_M[year]:8.2f}M "
        f"({(got / 1e6 / MARKET_SUBS_REVENUE_M[year] - 1) * 100:+.1f}%)"
    )
