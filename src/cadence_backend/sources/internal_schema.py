"""Schema context for Bellweather's own systems, FINANCE and SUPPORT.

Snowflake-only: these two schemas have no Postgres counterpart, because the
point of them is a client warehouse that federates a bought-in panel with
internal systems.

Curated rather than introspected, for the same reason as the market schema: the
traps below — the coverage subset, the fiscal calendar, the units, and the two
disagreeing measures of revenue — are exactly what an information_schema dump
omits, and they are the difference between a correct answer and a confident
wrong one.
"""

INTERNAL_SCHEMA_CONTEXT = """# Internal systems: FINANCE and SUPPORT

Cadence is deployed at **Bellweather Media**. The MARKET schema above is a
third-party panel covering all ten services in the market. FINANCE and SUPPORT
are Bellweather's OWN systems and cover ONLY the two services it operates:
Tidepool (service_id 5) and Harborlight (service_id 6).

All three schemas live in the same Snowflake database, so you CAN join across
them in one query. Qualify the schema: `MARKET.streaming_service`,
`FINANCE.gl_entry`, `SUPPORT.ticket`.

## FINANCE — the general ledger
- fiscal_calendar(period_month, fiscal_year, fiscal_quarter, period_status)
    Bellweather's fiscal year runs APRIL to MARCH. April 2024 is FY2025 Q1.
    period_status 'open' means unaudited and still subject to change.
- gl_account(account_code, account_name, statement_line, normal_balance)
    4000 subscription revenue | 4100 advertising | 4200 add-ons |
    4900 refunds and credits (stored NEGATIVE) | 5000 content amortisation |
    6000 marketing spend.
- gl_entry(entry_id, period_month, service_id, account_code, amount_usd, is_restated)
    SERVICE-MONTH-ACCOUNT grain. amount_usd is in DOLLARS.
- v_finance_monthly_pl(period_month, fiscal_year, fiscal_quarter, period_status, service_id,
    service_name, gross_subscription_usd, refunds_usd, net_subscription_usd, advertising_usd,
    addon_usd, content_cost_usd, marketing_cost_usd, contribution_usd, any_line_restated)
    Prefer this. It already joins the service dimension out of MARKET.

## SUPPORT — the helpdesk
- reason_code(code, label, category)
    category: billing | account | technical | content | other.
- ticket(ticket_id, service_id, opened_on, channel, reason_code, resolved_on, csat_score)
    TICKET grain. channel: chat | phone | email. resolved_on NULL means still open.
- v_support_monthly(month, service_id, service_name, category, tickets, resolved_tickets,
    avg_days_to_resolve, csat_responses, avg_csat)
    Prefer this.

## SIX TRAPS — each one produces a confidently wrong answer if missed.

1. COVERAGE. FINANCE and SUPPORT hold services 5 and 6 ONLY. Joining either to
   MARKET without care silently drops the other eight services. A question about
   the market must be answered from MARKET; a question about "our" services may
   use the internal schemas. Never present a two-service figure as a market figure.

2. UNITS. gl_entry.amount_usd is in DOLLARS. MARKET.annual_revenue.revenue_usd_m is
   in MILLIONS. Comparing them without converting is wrong by a factor of a million.

3. FISCAL CALENDAR. FINANCE.fiscal_calendar.fiscal_year runs April-March;
   MARKET.annual_revenue.fiscal_year is a calendar year. FINANCE FY2025 is
   Apr 2024 - Mar 2025. NEVER join or group on fiscal_year across the two — filter
   on period_month instead when you need a calendar comparison.

4. TWO MEASURES OF REVENUE, BOTH CORRECT. MARKET.annual_revenue is the panel
   provider's estimate of GROSS BILLINGS. GL account 4000 is the closest internal
   equivalent and agrees to within roughly 1% per calendar year, but the gap is not
   constant and cannot be used to convert one into the other. Net subscription
   revenue (4000 + 4900) is a DIFFERENT measure and will not match the provider at
   all. Whenever you quote a revenue figure for Tidepool or Harborlight, say which
   source and which measure it is.

5. THE BUNDLE — FINANCE CAN SPLIT WHAT MARKET CANNOT. MARKET reports Tidepool and
   Harborlight subscription revenue as ONE combined figure repeated on both rows
   with is_bundled TRUE, and it genuinely cannot be decomposed from that source.
   FINANCE posts the two separately, because it is Bellweather's own ledger. So a
   per-service subscription split for services 5 and 6 IS answerable — from FINANCE.
   "Cannot be decomposed" is a fact about the panel, not about the world. Name the
   source that gave you the split.

6. TICKETS ARE ESCALATIONS, NOT CONTACTS. SUPPORT.ticket records TIER-2 ESCALATIONS
   only; self-service and tier-1 deflection are recorded nowhere. It is a lower
   bound on contact volume and cannot produce a contact rate per subscriber. Three
   further limits:
   - Coverage starts 2024-07-01 (helpdesk migration). Nothing before that month is
     absence of contact; it is absence of measurement.
   - A ticket's service_id is the service the customer contacted ABOUT, which is not
     necessarily the one they went on to cancel.
   - csat_score is populated on roughly a third of resolved tickets. Average it over
     COUNT(csat_score), never over COUNT(*), and report the response count.
   - Harborlight's Feb and Mar 2025 ledger periods carry is_restated TRUE.
"""
