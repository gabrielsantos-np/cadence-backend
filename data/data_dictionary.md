# Data Dictionary — Synthetic Streaming-Market Dataset

**Everything in this dataset is fictional.** All ten services, their parent
companies, every franchise title, the three research firms and every number were
invented for this prototype. No real company, brand, service or franchise is
referenced, and no figure is derived from real market data.

| | |
|---|---|
| **Domain** | US streaming-video subscription market (fictional) |
| **Window** | January 2024 – June 2026 (30 monthly periods) |
| **Engine** | PostgreSQL 15+ (verified on 16) |
| **Load order** | `schema.sql`, then `seed_data.sql`, into an empty database |

```sh
psql -d market -v ON_ERROR_STOP=1 -f schema.sql
psql -d market -v ON_ERROR_STOP=1 -f seed_data.sql
```

| Table | Rows |
|---|---|
| `data_sources` | 3 |
| `content_genre` | 6 |
| `streaming_service` | 10 |
| `service_genre_catalog` | 35 |
| `monthly_subscribers` | 287 |
| `genre_engagement` | 977 |
| `retention_cohorts` | 92 |
| `annual_revenue` | 87 |
| `market_events` | 22 |
| `known_data_gaps` | 5 |

---

## Read this first: the three grains

The dataset holds three different grains. Conflating them is the single most
likely way to produce a confidently wrong answer.

| Grain | Table | The unit is… | Never do this |
|---|---|---|---|
| **Service** | `monthly_subscribers` | one subscription to one service in one month | Do not break subscribers down by genre. A subscriber belongs to a service; the data does not say which genre they "belong" to. |
| **Engagement** | `genre_engagement` | viewing attributed to one service × genre × month | Do not sum `engaged_subscribers` across genres and call it a subscriber count. One subscriber who watches three genres appears in three rows. |
| **Revenue line** | `annual_revenue` | dollars for one service × line × **year** | Do not derive a monthly revenue figure. There is no monthly revenue anywhere, and FY2026 is a half year. |

Two further traps sit on top of the grains:

- **Subscriber overlap is not modelled at all.** Market share in
  `v_market_share_by_month` is share of *subscriptions*, not of people. Nothing in
  the dataset supports a question about how many households subscribe to two
  services at once.
- **Two services report revenue as one bundle.** See
  [The bundled-revenue trap](#the-bundled-revenue-trap) below — it is the subtlest
  thing in here and worth reading before writing any revenue query.

---

## Dimensions

### `content_genre`

**Grain:** one row per genre. Six rows, fixed for the whole window.

| Column | Type | Notes |
|---|---|---|
| `genre_id` | `SMALLINT` PK | 1–6 |
| `genre_name` | `TEXT` | Unique |
| `lifecycle_stage` | `TEXT` | `growing` / `mature` / `declining` |
| `description` | `TEXT` | What the category covers |

| id | Genre | Stage |
|---|---|---|
| 1 | Prestige Drama | mature |
| 2 | Documentary | mature |
| 3 | Kids & Family | growing |
| 4 | Anime | growing |
| 5 | Live Sports | growing |
| 6 | Reality & Lifestyle | **declining** |

**Caveat:** `lifecycle_stage` is a static analyst label, not a computed field. It
does not update as the numbers move, and it is a market-wide judgement — a
declining category can still be growing for an individual service.

### `streaming_service`

**Grain:** one row per service. Ten rows.

| Column | Type | Notes |
|---|---|---|
| `service_id` | `SMALLINT` PK | 1–10 |
| `service_name` | `TEXT` | Unique |
| `parent_company` | `TEXT` | **Not unique** — Bellweather Media owns two services |
| `launch_date` | `DATE` | Most predate the window; only Cinder launches inside it |
| `base_price_usd` | `NUMERIC(5,2)` | Advertised standard monthly price **at the end of the window** |
| `tier` | `TEXT` | `incumbent` / `mid` / `niche` / `entrant` |

| id | Service | Parent | Tier | Price | Launched |
|---|---|---|---|---|---|
| 1 | Lumora+ | Halcyon Media Group | incumbent | $15.99 | 2013-06-12 |
| 2 | Northgate Stream | Northgate Broadcasting | incumbent | $14.99 | 2014-09-03 |
| 3 | Kestrel | Vantage Entertainment | mid | $11.99 | 2017-03-21 |
| 4 | Solstice TV | Marrow Holdings | mid | $10.99 | 2018-01-16 |
| 5 | Tidepool | **Bellweather Media** | mid | $9.99 | 2016-11-08 |
| 6 | Harborlight | **Bellweather Media** | mid | $8.99 | 2019-05-14 |
| 7 | Kinoloft | Kinoloft Collective | niche | $8.99 | 2019-08-27 |
| 8 | Orikuma | Orikuma Labs | niche | $7.49 | 2020-02-11 |
| 9 | Grovehouse | Grovehouse Films | niche | $18.99 | 2018-06-05 |
| 10 | Cinder | Ardent Global | entrant | $13.49 | **2025-02-04** |

**Caveats:**
- `base_price_usd` is the **end-of-window** price. Four services changed price
  during the window (see `market_events`), so multiplying this price by early-window
  subscribers overstates revenue. `annual_revenue` already accounts for the changes.
- Realised revenue per subscriber is always *below* `base_price_usd` for
  ad-supported services and roughly at it for premium ones, because of ad tiers and
  annual discounts.

### `service_genre_catalog`

**Grain:** service × genre. 35 rows.

| Column | Type | Notes |
|---|---|---|
| `service_id`, `genre_id` | `SMALLINT` PK | Composite |
| `entered_genre_on` | `DATE` | May predate the observation window |
| `catalog_titles` | `INTEGER` | Titles held at the end of the window |
| `flagship_franchise` | `TEXT` | Nullable — most genres have none |

Only the two incumbents carry all six genres. Coverage runs from 2 genres
(Orikuma, Grovehouse, Cinder) to 6.

**Caveat:** a row here means the service has a catalogue in that genre. It does
**not** guarantee `genre_engagement` rows exist for every month — see
[Known data gaps](#known-data-gaps). Solstice TV keeps its
`service_genre_catalog` row for Reality & Lifestyle even after exiting the genre,
because the row records catalogue history, not current activity.

---

## Facts

### `monthly_subscribers` — service grain

**Grain:** service × month, first of month. 287 rows (10 services × 30 months,
less the 13 pre-launch months for Cinder).

| Column | Type | Notes |
|---|---|---|
| `service_id` | `SMALLINT` FK | |
| `month` | `DATE` | Always the first of the month |
| `subscribers_eom` | `INTEGER` | Active subscriptions at month end |
| `gross_adds` | `INTEGER` | New subscriptions started in the month |
| `cancellations` | `INTEGER` | Subscriptions ended in the month |
| `source_id` | `SMALLINT` FK | Always 2 (Cobalt Research Group) |

**The arithmetic is exact:**

```
subscribers_eom = (previous month's subscribers_eom) + gross_adds - cancellations
```

This holds for all 277 rows that have a predecessor in the table, with no
exceptions. Cinder's first month (2025-02) opens from zero, so it is checkable on
its own: 826,000 − 43,000 = 783,000.

**Caveats:**
- The opening balance for January 2024 predates the window and is not stored, so
  the January 2024 rows cannot be recomputed from the table alone. Every later row
  can.
- Values are rounded to the nearest thousand — that is reporting precision, not
  false accuracy.
- Monthly churn is `cancellations / (subscribers_eom + cancellations - gross_adds)`,
  i.e. cancellations over the *opening* base. Dividing by `subscribers_eom` gives a
  slightly different number for a growing service.
- Seasonality is real and market-wide: December and October–November are the
  strongest net-add months, January is negative market-wide (post-holiday
  cancellations), and July is the summer trough.

### `genre_engagement` — engagement grain

**Grain:** service × genre × month. 977 rows.

| Column | Type | Notes |
|---|---|---|
| `service_id`, `genre_id`, `month` | PK | FK to `service_genre_catalog` |
| `viewing_hours_m` | `NUMERIC(10,2)` | **Millions** of viewing hours in the month |
| `engaged_subscribers` | `INTEGER` | Subscribers who watched anything in the genre |
| `genre_share_pct` | `NUMERIC(5,2)` | Genre's share of that service's total viewing hours |
| `source_id` | `SMALLINT` FK | 3 for the seven larger services, 1 for the three niche services |

**`genre_share_pct` sums to exactly 100.00 for every one of the 269
service-months.** It is a share of *hours*, not of subscribers or revenue.

**Caveats:**
- `engaged_subscribers` **does not sum to `subscribers_eom`.** A subscriber who
  watches three genres is counted in all three, so the genre-level sum
  substantially exceeds the service's actual subscriber count. This is engagement
  *attribution*, and it is the most common way to get a wrong answer from this
  dataset.
- The genre taxonomy assigns each title exactly one genre (Streamscope co-op
  rule), so a title that could plausibly sit in two categories contributes to only
  one.
- Two different providers feed this table. Niche-service rows come from a consumer
  panel (source 1), the rest from device telemetry (source 3). The two are not
  strictly comparable at the margin.
- Coverage begins in July 2024 for the three niche services, and Solstice TV's
  Reality & Lifestyle rows stop after January 2026. Both are documented gaps.

### `retention_cohorts` — service grain, quarterly

**Grain:** service × quarter. 92 rows.

| Column | Type | Notes |
|---|---|---|
| `service_id`, `quarter_start` | PK | `quarter_start` is the first day of the quarter |
| `retained_m1_pct` | `NUMERIC(5,2)` | Share of the quarter's joiners still active 1 month later |
| `retained_m3_pct` | `NUMERIC(5,2)` | …3 months later |
| `retained_m6_pct` | `NUMERIC(5,2)` | …6 months later |
| `avg_tenure_months` | `NUMERIC(5,1)` | Mean tenure of the cohort |
| `source_id` | `SMALLINT` FK | Always 1 (Meridian Panel Analytics) |

`retained_m1_pct > retained_m3_pct > retained_m6_pct` holds for every row, enforced
by a table constraint.

**Caveats:**
- This tracks **joiners in that quarter**, not the whole subscriber base. It is
  therefore *not* the inverse of the cancellation rate in `monthly_subscribers` —
  new subscribers churn faster than tenured ones, so retention looks worse than
  base churn implies.
- The 6-month figure for the last two quarters (2026 Q1, 2026 Q2) necessarily
  extends past the end of the observation window; treat it as the provider's
  projection rather than an observed outcome.
- Solstice TV has no 2024 rows at all (documented gap), so any
  year-over-year retention comparison for Solstice starts in 2025.

### `annual_revenue` — revenue-line grain

**Grain:** service × revenue line × fiscal year. 87 rows.

| Column | Type | Notes |
|---|---|---|
| `service_id`, `revenue_line`, `fiscal_year` | PK | |
| `revenue_line` | `TEXT` | `subscriptions` / `advertising` / `add_ons` |
| `revenue_usd_m` | `NUMERIC(10,2)` | **Millions** of USD |
| `is_bundled` | `BOOLEAN` | `TRUE` means the figure covers more than one service |
| `source_id` | `SMALLINT` FK | Always 2 (Cobalt Research Group) |

Subscription revenue reconciles to average subscribers × `base_price_usd` ×
months observed, within ±15% (worst case in the data: 13.9%). Advertising and
add-ons are each smaller than subscriptions for every service and year.

**Caveats:**
- **FY2026 covers January–June 2026 only.** It is a half year. Comparing FY2026 to
  FY2025 without annualising makes every service look like it collapsed.
- Cinder has no FY2024 rows (it launched in February 2025) and its FY2025 covers
  eleven months, not twelve.
- **Tidepool and Harborlight subscription rows are bundled.** See below.

---

## The bundled-revenue trap

> **Tidepool (5) and Harborlight (6) share the parent Bellweather Media, which
> reports their subscription revenue as a single combined figure.**
>
> Both services' `subscriptions` rows carry the **same combined value**, with
> `is_bundled = TRUE`:
>
> | Fiscal year | Tidepool row | Harborlight row |
> |---|---|---|
> | 2024 | $1,848.28M | $1,848.28M |
> | 2025 | $2,044.35M | $2,044.35M |
> | 2026 | $1,097.30M | $1,097.30M |
>
> **Summing the two rows double-counts the bundle.** The correct behaviour is to
> report the figure **once**, explicitly labelled as a combined Tidepool +
> Harborlight bundle. It **cannot be split** into per-service revenue at any grain,
> by any method — the underlying split is not in the dataset and cannot be inferred
> from subscriber ratios, because the two services have different prices and
> different discounting.
>
> Only the `subscriptions` line is bundled. `advertising` and `add_ons` are
> reported per service and are safe to use normally.

**The knock-on effect worth knowing about:** `v_revenue_per_subscriber` divides
total revenue by one service's subscribers. For Harborlight — the smaller of the
pair — that inflates FY2025 revenue per subscriber to **$318.40**, the highest in
the market and roughly 2.3× its true level. The figure is flagged by
`any_line_bundled`, and it is wrong. Filter bundled rows out (or label them) before
ranking anything by revenue per subscriber.

---

## Narrative and metadata tables

### `market_events`

**Grain:** one row per event. 22 rows spanning 2024-02 to 2026-05.

| Column | Type | Notes |
|---|---|---|
| `event_id` | `SMALLINT` PK | |
| `event_date` | `DATE` | Exact day; facts are monthly, so join on `DATE_TRUNC('month', …)` |
| `event_type` | `TEXT` | `launch` / `price_change` / `content_deal` / `franchise_release` / `outage` / `genre_exit` |
| `service_id` | `SMALLINT` FK | **Nullable** — NULL for market-wide events (event 5) |
| `headline`, `detail` | `TEXT` | |

**Every event leaves a fingerprint in the facts:**

- **`price_change` → cancellation spike at +1 to +2 months.** All four:
  Lumora+ May 2024 (churn 2.64% → 5.15%), Tidepool June 2025 (2.54% → 4.24%),
  Lumora+ September 2025 (2.88% → 5.03%), Grovehouse May 2026 (1.60% → 2.54%).
- **`franchise_release` → gross-adds bump in the release month.** All nine show
  higher gross adds than the preceding month.
- **`genre_exit` → the rows stop.** Solstice TV's last Reality & Lifestyle
  engagement row is January 2026; there are zero rows from February 2026 onward.
- **`outage` events (9, 19) are deliberately inert.** Neither leaves a measurable
  mark on subscribers. They exist so that "did the outage hurt them?" has the
  honest answer *no*, rather than being unanswerable.

### `data_sources`

Three fictional providers. Methodology differences matter: Meridian is a
consumer panel (which is why niche coverage starts late), Cobalt normalises
operator disclosures (which is why bundled revenue arrives bundled), and
Streamscope is device telemetry (which is why genre attribution is single-genre).

### `known_data_gaps`

Five documented holes. **Consult this table before concluding that an empty
result means "no activity" — it may mean "not measured".**

| id | Table | Period | Gap |
|---|---|---|---|
| 1 | `genre_engagement` | 2024-01 → 2024-06 | Kinoloft, Orikuma and Grovehouse have no rows. Panel cells were below reporting threshold. Genre totals before July 2024 understate the market — do not trend across the break. |
| 2 | `retention_cohorts` | 2024 | Solstice TV has no cohorts for any 2024 quarter. First measured cohort is 2025 Q1. |
| 3 | `genre_engagement` | 2026-02 → 2026-06 | Solstice TV × Reality & Lifestyle. A **real withdrawal** (event 20), not a measurement gap. |
| 4 | `annual_revenue` | all years | Tidepool/Harborlight subscription bundle; cannot be split. |
| 5 | *not modelled* | entire window | **Subscriber overlap between services does not exist anywhere in this dataset.** No household, account or panel-person table. |

---

## Views

| View | Grain | Use it for |
|---|---|---|
| `v_market_share_by_month` | service × month | Share of total market subscriptions, with `subscriber_rank` |
| `v_genre_market` | genre × month | Total genre viewing hours, competitor count, leading service |
| `v_service_overview` | service | One-row profile joining latest values from all three grains |
| `v_competitive_sets` | service pair | Services overlapping in ≥2 genres (the competitor graph) |
| `v_price_change_impact` | event × month | Churn from 3 months before to 4 months after each price change |
| `v_entrant_ramp` | service × month | Subscribers indexed to months-since-first-observation |
| `v_revenue_per_subscriber` | service × year | Annual revenue per subscriber, with retention alongside |
| `v_genre_competition_trend` | service × genre × month | Share of genre viewing, with competitor count |

**View-specific caveats:**

- `v_market_share_by_month` — share of *subscriptions*, which double-counts people
  who hold several. Not share of households.
- `v_genre_market` — covers only services reporting engagement that month. The
  July 2024 coverage change (gap 1) creates an artificial step up in genre totals
  for Prestige Drama, Documentary, Kids & Family and Anime. Reality & Lifestyle and
  Live Sports are unaffected, because no niche service competes in them.
- `v_service_overview` — **deliberately mixes grains.** Each column is a latest
  observation at its own cadence (monthly / quarterly / annual). Never read the row
  as one consistent period. Check `subscription_is_bundled` before using the
  revenue column.
- `v_competitive_sets` — catalogue overlap only; it says nothing about whether two
  services actually compete for the same viewers.
- `v_entrant_ramp` — `month_index` counts from **first observation in this
  dataset**, which equals months-since-launch only for Cinder. For everyone else,
  `month_index = 1` is simply January 2024.
- `v_revenue_per_subscriber` — annual, not monthly. FY2026 is a half year. Rows
  with `any_line_bundled = TRUE` are inflated and must be excluded from rankings.

---

## Storylines hidden in the data

### 1. The challenger — Kestrel climbs from 8th to 3rd

Kestrel opens the window ranked **8th** with 3,018,000 subscribers and ends it
**3rd** with 17,771,000 — a 5.9× rise, while every other mid-size service moved by
less than 30%.

The engine is a single growing genre. Kestrel won exclusive Anthem Cup rights
in August 2024 (`market_events` id 6) and began live coverage in May 2025 (id 13).
Live Sports goes from **51.9% to 68.2%** of Kestrel's own viewing, and its share of
the *market's* Live Sports hours goes from **8.6% to 36.0%** — it takes the genre
lead from the incumbents.

> **Evidence:** `v_market_share_by_month` for Kestrel at 2024-01-01
> (`subscriber_rank` 8) and 2026-06-01 (rank 3); `v_genre_competition_trend` for
> Kestrel × Live Sports across the window; `market_events` 6 and 13.

### 2. The stumbling incumbent — Lumora+ raises prices twice

Lumora+ raised its standard tier from $12.99 to $14.49 in **May 2024**
(`market_events` id 3) and from $14.49 to $15.99 in **September 2025** (id 16).

Each hike produced a cancellation spike peaking one month later:

| Hike | Churn at −1 month | Peak churn at +1 | Cancellations at peak |
|---|---|---|---|
| May 2024 | 2.64% | **5.15%** (Jun 2024) | 2,178,000 |
| Sep 2025 | 2.88% | **5.03%** (Oct 2025) | 2,243,000 |

Retention never recovers between them. `retained_m1_pct` falls monotonically
across all ten quarters, 93.30% → 88.20%, and `retained_m6_pct` falls 65.95% →
47.07%, with visible step-downs in 2024 Q2 and 2025 Q4 — the quarters containing
each hike. Lumora+ ends the window with the **worst 6-month retention in the
market** despite still being the largest service.

> **Evidence:** `v_price_change_impact` filtered to `service_id = 1`;
> `retention_cohorts` for service 1 across all ten quarters.

### 3. The successful launch — Cinder overtakes Orikuma in 9 months

Cinder launched **2025-02-04** (`market_events` id 10) with exactly two genres:
**Anime** and **Kids & Family**. It has zero rows in every fact table before
February 2025.

The ramp decelerates as it should — 783k in month 1, 2,281k by month 3, 3,572k by
month 6, 5,399k by month 12 — with re-acceleration at the two franchise releases
(March 2025, October 2025).

It passes the niche anime specialist Orikuma in **October 2025, month 9**:

| Month | Cinder | Orikuma |
|---|---|---|
| 2025-09 | 4,166,000 | 4,597,000 |
| **2025-10** | **4,830,000** | 4,592,000 |

Orikuma's own trajectory inverts at the launch: average monthly net adds go from
**+37,231 before** February 2025 to **−14,118 after**.

> **Evidence:** `v_entrant_ramp` for service 10; `monthly_subscribers` for
> services 8 and 10 around 2025-10; `service_genre_catalog` for service 10
> (exactly two rows).

### 4. The genre in decline — Reality & Lifestyle, and Solstice TV's exit

Reality & Lifestyle total viewing hours fall from **580.6M (Jan 2024) to 435.5M
(Jun 2026) — exactly −25.0%** — while every other genre grew or held flat, and
while total market subscriptions rose.

The most exposed service is **Solstice TV**, which drew **56.1%** of its viewing
from the category in January 2024 — far more than the next most exposed
(Harborlight, 42.2%). Its exposure erodes to 43.1% by January 2026 as the category
shrinks, and its subscribers slide from 11,245,000 to 9,702,000 — the only service
to decline across the window.

In **February 2026** Solstice exits the genre outright (`market_events` id 20,
`event_type = 'genre_exit'`). Its last Reality & Lifestyle engagement row is
January 2026; there are zero rows afterwards, and the genre's competitor count
drops from 6 to 5.

> **Evidence:** `v_genre_market` for Reality & Lifestyle at 2024-01-01 and
> 2026-06-01; `genre_engagement` share for service 4 × genre 6 over time;
> `market_events` id 20; `known_data_gaps` id 3.

### 5. The quiet compounder — Grovehouse

Grovehouse never ranks above **10th** in subscribers (3.4M at the end, the
smallest in the market), but:

- **Best retention in the market by a wide margin.** Latest quarter (2026 Q2):
  97.76% / 93.44% / 87.32% at 1, 3 and 6 months, and 34.9 months average tenure.
  The next best 6-month figure belongs to Kinoloft at 82.33%; Lumora+ manages 47.07%.
- **Highest genuine revenue per subscriber:** $264.23 (FY2024) and $260.43
  (FY2025), against $211.77 for Lumora+ and $208.74 for Northgate Stream.

The two facts are linked: Grovehouse charges the market's highest price ($18.99),
runs almost no advertising (2% of subscription revenue, versus 19–22% for the
incumbents) and earns the market's largest add-on share (12%). Low churn is what
lets a premium, ad-free proposition work at that price.

> **Careful:** a naive ranking of `v_revenue_per_subscriber` puts **Harborlight**
> first at $318.40. That figure is an artefact of the bundled-revenue trap — it is
> flagged `any_line_bundled = TRUE`. Exclude bundled rows and Grovehouse is
> correctly first. Harborlight's mediocre 68.6% 6-month retention is the tell that
> the number cannot be real.
>
> **Evidence:** `v_revenue_per_subscriber` for FY2025 filtered to
> `NOT any_line_bundled`; `retention_cohorts` for service 9.

---

## Acceptance questions

The prototype writes its own SQL against these views. Expected answers below are
the actual values in the seeded data.

### Tier 1 — Smoke tests

| # | Question | Expected answer |
|---|---|---|
| 1 | How many streaming services are in the catalog? | **10** |
| 2 | How many content genres do we track? | **6** |
| 3 | What was Lumora+'s subscription revenue in fiscal 2025? | **$7,451.53M** (not bundled) |
| 4 | What was Kestrel's subscriber count at the end of June 2026? | **17,771,000** |
| 5 | What's Grovehouse's 6-month retention in the latest quarter? | **87.32%** (2026 Q2; m1 97.76%, m3 93.44%, tenure 34.9 months) |

### Tier 2 — The data-trap test (run this first)

**6. What was Tidepool's and Harborlight's subscription revenue in fiscal 2025?**

- **Correct:** "$2,044.35M — but that is a single **combined** figure covering both
  Tidepool and Harborlight. Bellweather Media reports them as one bundle
  (`is_bundled = TRUE`), and it cannot be split per service."
- **Wrong:** reporting $4,088.70M, or reporting $2,044.35M twice as though each
  service earned it independently.

### Tier 3 — Trend and comparison questions (should chart)

| # | Question | Expected shape |
|---|---|---|
| 7 | Monthly subscribers for Kestrel since January 2024 | Line rising 3,018,000 → 17,771,000, inflecting after Aug 2024 and again from May 2025 |
| 8 | Compare 1/3/6-month retention for Lumora+, Kestrel and Grovehouse | Grouped bars, latest quarter: Lumora+ 88.20 / 68.60 / 47.07; Kestrel 94.24 / 83.70 / 70.06; Grovehouse 97.76 / 93.44 / 87.32 |
| 9 | Cinder's ramp against Orikuma since launch | Two lines crossing in **October 2025**; Cinder 4,830,000 vs Orikuma 4,592,000 |
| 10 | Total viewing hours in Reality & Lifestyle over the window | Downtrend, 580.6M → 435.5M (−25.0%) |

### Tier 4 — Analytical questions

**11. Lumora+ raised prices twice — what happened, and what if they do it again?**
Both hikes produced a churn spike peaking at +1 month (5.15% after May 2024,
5.03% after September 2025, against a ~2.6–2.9% baseline), decaying over roughly
two months. Retention did **not** recover between them: `retained_m1_pct` fell
monotonically 93.30% → 88.20% across the ten quarters. A third rise should be
expected to produce a similar ~2× churn spike, but from an already-degraded base —
and the cumulative retention damage, not the one-month spike, is the thing to
model.

**12. What does a realistic first-year entrant ramp look like?**
From Cinder: 783k in month 1, 2,281k by month 3, 3,572k by month 6, 4,830k by
month 9, 5,399k by month 12. Front-loaded and decelerating — roughly half of
first-year subscribers arrive in the first four months. Franchise releases at
months 2 and 9 produce visible re-acceleration.

**13. How did Cinder's launch affect the niche services in the same two genres?**
Cinder competes in Anime and Kids & Family. Orikuma (Anime + Kids & Family) is hit
directly: average monthly net adds fall from **+37,231 pre-launch to −14,118
post-launch**, and it is overtaken in October 2025. Kinoloft, whose Kids & Family
catalogue is a small third genre, is largely unaffected.

**14. Which service is most exposed to Reality & Lifestyle, and what did they do?**
**Solstice TV** — 56.1% of its viewing in January 2024, the highest exposure in the
market. It **exited the genre in February 2026** (`market_events` id 20), and
reports no Reality & Lifestyle engagement from that month onward.

**15. Which service has the best revenue per subscriber, and does retention explain it?**
**Grovehouse**, at $260.43 in FY2025 — once bundled rows are excluded. Yes:
Grovehouse has the market's best retention (87.32% at 6 months, 34.9-month average
tenure), which is what sustains the highest price point ($18.99) with almost no
advertising. *A system that fails to exclude bundled rows will wrongly answer
Harborlight ($318.40) — see the trap above.*

**16. Which genres are getting more competitive, and what does it do to a mid-size service's share?**
Anime (4 → 5 reporting services) and Kids & Family (6 → 7) gain competitors, both
driven by Cinder's entry; Reality & Lifestyle loses one (6 → 5) via Solstice's
exit. For a mid-size service the effect is visible in
`v_genre_competition_trend`: Tidepool's absolute Anime hours rise while its *share*
of the genre falls, because the genre grew faster than Tidepool did.
**Note:** compare from July 2024 onward only — gap 1 changes genre coverage before
then and will otherwise read as a spurious jump in competitiveness.

### Tier 5 — Guardrails (the correct answer is a refusal)

| # | Question | Required behaviour | Verified |
|---|---|---|---|
| 17 | What was StreamVault's revenue in 2025? | **"No such service."** StreamVault does not exist in `streaming_service`. Never invent a figure. | 0 rows match |
| 18 | What's the subscriber overlap between Lumora+ and Northgate Stream? | **"Not available."** Subscriber overlap is not modelled anywhere — no household or account table exists. Point to `known_data_gaps` id 5. | No such table |
| 19 | Show me Orikuma's genre engagement for March 2024. | **"Not measured."** Falls inside gap 1 — niche coverage begins July 2024. Name the gap; do not report zero as if it meant no viewing. | 0 rows |
| 20 | What are Tidepool's standalone subscription revenues? | **"Cannot be split."** The figure is bundled with Harborlight (`is_bundled = TRUE`); no standalone split exists at any grain. Point to `known_data_gaps` id 4. | Both rows identical |
