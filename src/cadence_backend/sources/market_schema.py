"""The schema description injected into the system prompt.

Curated rather than introspected: the traps in this dataset (grain
conflation, bundled revenue, documented gaps) are exactly what a raw
information_schema dump would omit, and they are the difference between a
correct answer and a confident wrong one.
"""

MARKET_SCHEMA_CONTEXT = """# Dataset: fictional US streaming-subscription market, Jan 2024 - Jun 2026 (30 months)

All data is synthetic. Ten invented services, six genres.

## Dimensions
- streaming_service(service_id, service_name, parent_company, launch_date, base_price_usd, tier)
    tier: incumbent | mid | niche | entrant. base_price_usd is the END-OF-WINDOW price.
- content_genre(genre_id, genre_name, lifecycle_stage, description)
- service_genre_catalog(service_id, genre_id, entered_genre_on, catalog_titles, flagship_franchise)

## Facts — THREE SEPARATE GRAINS. Never join them as if they were one.
- monthly_subscribers(service_id, month, subscribers_eom, gross_adds, cancellations, source_id)
    SERVICE grain. subscribers_eom = prior month + gross_adds - cancellations, exactly.
    Monthly churn = cancellations / (subscribers_eom + cancellations - gross_adds).
- genre_engagement(service_id, genre_id, month, viewing_hours_m, engaged_subscribers, genre_share_pct, source_id)
    ENGAGEMENT grain. viewing_hours_m is MILLIONS of hours. genre_share_pct sums to 100
    per service-month. engaged_subscribers DOUBLE-COUNTS across genres and must never be
    summed into a subscriber total.
- retention_cohorts(service_id, quarter_start, retained_m1_pct, retained_m3_pct, retained_m6_pct, avg_tenure_months, source_id)
    Tracks JOINERS in that quarter, not the whole base. Not the inverse of churn.
- annual_revenue(service_id, revenue_line, fiscal_year, revenue_usd_m, is_bundled, source_id)
    ANNUAL only, in MILLIONS USD. revenue_line: subscriptions | advertising | add_ons.
    FY2026 covers Jan-Jun 2026 only — a HALF year.
- market_events(event_id, event_date, event_type, service_id, headline, detail)
    event_type: launch | price_change | content_deal | franchise_release | outage | genre_exit.
    service_id is NULL for market-wide events.
- data_sources(source_id, source_name, methodology, coverage_note)
- known_data_gaps(gap_id, affected_table, affected_period, description)

## Views (prefer these)
- v_market_share_by_month(month, service_id, service_name, tier, subscribers_eom, market_subscribers, market_share_pct, subscriber_rank)
- v_genre_market(genre_id, genre_name, lifecycle_stage, month, total_viewing_hours_m, competing_services, leading_service, leading_viewing_hours_m, leader_share_pct)
- v_service_overview(service_id, service_name, parent_company, tier, launch_date, base_price_usd, genre_count, latest_month, latest_subscribers, latest_quarter, latest_retained_m1_pct, latest_retained_m3_pct, latest_retained_m6_pct, latest_avg_tenure_months, latest_fiscal_year, subscription_revenue_usd_m, subscription_is_bundled, other_revenue_usd_m)
- v_competitive_sets(service_id, service_name, competitor_id, competitor_name, shared_genres, shared_genre_names)
- v_price_change_impact(event_id, event_date, service_id, service_name, headline, month, months_from_event, cancellations, gross_adds, subscribers_eom, monthly_churn_pct)
    months_from_event 0 = the month the new price took effect. Spikes land at +1 to +2.
- v_entrant_ramp(service_id, service_name, tier, month, month_index, subscribers_eom, gross_adds, cancellations)
    month_index counts from FIRST OBSERVED month — equals months-since-launch only for the entrant.
- v_revenue_per_subscriber(service_id, service_name, tier, fiscal_year, avg_subscribers, months_observed, total_revenue_usd_m, any_line_bundled, revenue_per_subscriber_usd, avg_retained_m6_pct)
- v_genre_competition_trend(genre_id, genre_name, lifecycle_stage, month, service_id, service_name, tier, viewing_hours_m, genre_total_hours_m, share_of_genre_pct, competing_services)

## THREE TRAPS — getting these wrong produces a confidently wrong answer.

1. GRAIN. Subscribers are service-grain; genre engagement is attribution. Never sum
   engaged_subscribers to get a subscriber count. Revenue is annual only.

2. BUNDLED REVENUE. Tidepool and Harborlight share the parent Bellweather Media, which
   reports their subscription revenue as ONE COMBINED figure. Both rows carry the SAME
   value with is_bundled = TRUE. Summing them double-counts. It cannot be split per
   service. When ranking by revenue or revenue-per-subscriber you MUST exclude or flag
   bundled rows — otherwise Harborlight appears to lead revenue-per-subscriber, which is
   an artefact, not a fact.

3. DOCUMENTED GAPS. An empty result may mean "not measured", not "no activity". Always
   check known_data_gaps before concluding something is absent:
   - Kinoloft, Orikuma, Grovehouse have NO genre_engagement before 2024-07.
   - Solstice TV has NO retention_cohorts for any 2024 quarter.
   - Solstice TV has no Reality & Lifestyle engagement from 2026-02 (a real genre exit).
   - SUBSCRIBER OVERLAP BETWEEN SERVICES IS NOT MODELLED ANYWHERE. There is no household,
     account or person table. Questions about how many people hold two subscriptions, or
     about de-duplicated reach, CANNOT be answered — say so rather than approximating."""
