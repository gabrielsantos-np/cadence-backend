"""Derive acceptance cases whose answers are computed from the warehouse.

    uv run python scripts/gen_market_truth.py --count 20

The planted easter eggs turned out not to be ground truth. Sampled against the
data they describe, 0 of 8 MARKET claims and 0 of 6 SUPPORT claims held up — the
generator asserted phenomena the warehouses do not contain, and two SUPPORT eggs
cite months before the ticket data begins. A benchmark scored against those is
measuring agreement with fiction.

These cases cannot have that problem. Every expected answer is *computed by a
query against the same tables the analyst will query*, so the truth and the data
cannot disagree by construction. The trade is that this only produces questions
with a checkable answer — which is the point: it measures whether the analyst
gets the number right, not whether it retrieves a document.

Output is the ordinary acceptance-case shape, so `scripts/acceptance.py` runs
them with its existing cost accounting and pass/fail/error classification.
"""

import argparse
import asyncio
import datetime as dt
import json
import pathlib
import random

import asyncpg

from cadence_backend.core.config import get_settings

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "market_truth_cases.json"

#: Fixed so a rerun produces the same questions and two runs are comparable.
RNG = random.Random(20260901)


def d(iso: str) -> dt.date:
    """asyncpg binds DATE parameters as date objects, never strings."""
    return dt.date.fromisoformat(iso)


#: Digits must not be preceded or followed by another digit or a decimal
#: point. Without this, "3.3" matches inside "13.35" and a wrong answer scores
#: as correct — the first version of this file had exactly that bug, and would
#: have reported a falsely high accuracy.
GUARD_L = r"(?<![\d.])"
GUARD_R = r"(?![\d])"


def number_forms(value: int) -> str:
    """A regex accepting the ways a model legitimately writes one integer.

    Tolerates presentation without tolerating a wrong figure: 86000, 86,000 and
    +86,000 are the same answer; 86,001 is not. Rounded millions are accepted
    only at the precision that actually round-trips, so an expected 3,456,019
    accepts "3.46 million" but never a bare "3 million".
    """
    plain = str(abs(value))
    forms = [f"{abs(value):,}".replace(",", r",?"), plain]
    if abs(value) >= 1_000_000:
        m = abs(value) / 1_000_000
        for places in (1, 2):
            if abs(round(m, places) * 1_000_000 - abs(value)) < 0.5 * 10 ** (6 - places):
                forms.append(rf"{m:.{places}f}".replace(".", r"\.") + r"\s*(?:m\b|million)")
    return GUARD_L + "(?:" + "|".join(forms) + ")" + GUARD_R


def decimal_forms(value: float, places: int = 2) -> str:
    """A regex for a rate or percentage, accepting one less decimal place.

    4.24 may legitimately be written 4.2, but must not match inside 14.24.
    """
    exact = f"{value:.{places}f}".replace(".", r"\.")
    rounded = f"{value:.{places - 1}f}".replace(".", r"\.")
    return GUARD_L + f"(?:{exact}|{rounded})" + GUARD_R


async def build(con: asyncpg.Connection, count: int) -> list[dict]:
    cases: list[dict] = []

    def add(cid, question, why, must, sql, expected):
        cases.append(
            {
                "id": cid,
                "question": question,
                "why": why,
                "must_include": must,
                # Not asserted on, but recorded: a failure needs the query that
                # produced the expected value, or it cannot be diagnosed.
                "expected": expected,
                "derived_from": " ".join(sql.split()),
            }
        )

    # --- net subscriber movement: the most basic thing the client will ask ---
    q = """SELECT s.service_name, to_char(m.month,'FMMonth YYYY') AS mon,
                  m.gross_adds - m.cancellations AS net
             FROM monthly_subscribers m JOIN streaming_service s USING(service_id)
            WHERE m.month = $1"""
    for month in ("2025-04-01", "2024-11-01"):
        for r in RNG.sample(await con.fetch(q, d(month)), 2):
            add(
                f"net-{r['service_name'][:6].lower().strip('+ ')}-{month[:7]}",
                f"What was {r['service_name']}'s net subscriber change in {r['mon']}?",
                "Net movement from the census. Wrong here means bad arithmetic or the wrong month.",
                [number_forms(r["net"])],
                q,
                r["net"],
            )

    # --- the biggest service in a month: a lookup, not a calculation ---
    q = """SELECT s.service_name, m.subscribers_eom, to_char(m.month,'FMMonth YYYY') AS mon
             FROM monthly_subscribers m JOIN streaming_service s USING(service_id)
            WHERE m.month = $1 ORDER BY m.subscribers_eom DESC LIMIT 1"""
    for month in ("2026-06-01", "2024-06-01"):
        r = await con.fetchrow(q, d(month))
        add(
            f"largest-{month[:7]}",
            f"Which streaming service had the most subscribers in {r['mon']}?",
            "A plain lookup. If this is hedged or refused, the guardrails are too aggressive.",
            [r["service_name"].replace("+", r"\+")],
            q,
            r["service_name"],
        )

    # --- monthly churn: a derived rate, where the denominator is easy to get wrong ---
    q = """SELECT s.service_name, to_char(m.month,'FMMonth YYYY') AS mon,
                  ROUND(100.0*m.cancellations/(m.subscribers_eom + m.cancellations
                        - m.gross_adds), 2) AS churn
             FROM monthly_subscribers m JOIN streaming_service s USING(service_id)
            WHERE m.month = $1"""
    for month in ("2025-07-01", "2026-02-01"):
        r = RNG.choice(await con.fetch(q, d(month)))
        churn = float(r["churn"])
        add(
            f"churn-{r['service_name'][:6].lower().strip('+ ')}-{month[:7]}",
            f"What was {r['service_name']}'s monthly churn rate in {r['mon']}?",
            "Churn needs the opening base, not the closing one — a near-miss denominator.",
            [decimal_forms(churn)],
            q,
            churn,
        )

    # --- gross adds: distinguishes gross from net, the trap in the demo set ---
    q = """SELECT s.service_name, to_char(m.month,'FMMonth YYYY') AS mon, m.gross_adds
             FROM monthly_subscribers m JOIN streaming_service s USING(service_id)
            WHERE m.month = $1"""
    for r in RNG.sample(await con.fetch(q, d("2025-09-01")), 2):
        add(
            f"adds-{r['service_name'][:6].lower().strip('+ ')}",
            f"How many gross additions did {r['service_name']} record in {r['mon']}?",
            "Gross, not net. Returning the net change here is the classic misread.",
            [number_forms(r["gross_adds"])],
            q,
            r["gross_adds"],
        )

    # --- panel scaling: the sample-vs-census trap, with a checkable number ---
    q = """SELECT COUNT(*) AS sampled,
                  (SELECT sample_rate FROM market_panel_sample WHERE sample_id=1) AS rate
             FROM subscription_event
            WHERE event_type='cancel' AND event_date BETWEEN $1 AND $2"""
    for lo, hi, label in (
        ("2025-04-01", "2025-04-30", "April 2025"),
        ("2026-01-01", "2026-01-31", "January 2026"),
    ):
        r = await con.fetchrow(q, d(lo), d(hi))
        estimated = r["sampled"] * r["rate"]
        add(
            f"panel-{lo[:7]}",
            f"Using the event panel, roughly how many cancellations happened "
            f"market-wide in {label}?",
            "subscription_event is a 1-in-281 sample; unscaled understates by that factor.",
            [r"\b281\b", number_forms(estimated)],
            q,
            estimated,
        )

    # --- market share: needs a market total, not just the service ---
    q = """SELECT v.service_name, to_char(v.month,'FMMonth YYYY') AS mon,
                  ROUND(v.market_share_pct, 2) AS share
             FROM v_market_share_by_month v
            WHERE v.month = $1 ORDER BY v.market_share_pct DESC LIMIT 1"""
    r = await con.fetchrow(q, d("2025-12-01"))
    if r:
        add(
            "share-leader",
            f"What share of the market did {r['service_name']} hold in {r['mon']}?",
            "Share needs the market total as the denominator, not the service's own base.",
            [decimal_forms(float(r["share"]))],
            q,
            float(r["share"]),
        )

    # --- counting: trivial, and a canary for over-refusal ---
    n = await con.fetchval("SELECT COUNT(*) FROM streaming_service")
    add(
        "count-services",
        "How many streaming services are covered in this dataset?",
        "Trivial. A refusal or a hedge here means the guardrails have gone too far.",
        [rf"\b{n}\b"],
        "SELECT COUNT(*) FROM streaming_service",
        n,
    )

    # --- worst month: an argmax over a series ---
    q = """SELECT s.service_name, to_char(m.month,'FMMonth YYYY') AS mon, m.cancellations
             FROM monthly_subscribers m JOIN streaming_service s USING(service_id)
            WHERE s.service_name = $1 ORDER BY m.cancellations DESC LIMIT 1"""
    for svc in ("Lumora+", "Tidepool"):
        r = await con.fetchrow(q, svc)
        if r:
            add(
                f"peak-{svc[:6].lower().strip('+ ')}",
                f"In which month did {svc} record its highest cancellations?",
                "An argmax over the series — tests that it scans the range, not a slice.",
                [r["mon"].split()[0], r["mon"].split()[1]],
                q,
                r["mon"],
            )

    # --- annual revenue: the bundling trap lives here ---
    q = """SELECT s.service_name, r.fiscal_year, r.revenue_usd_m, r.is_bundled
             FROM annual_revenue r JOIN streaming_service s USING(service_id)
            WHERE r.revenue_line='advertising' AND r.fiscal_year=$1 AND NOT r.is_bundled
            ORDER BY r.revenue_usd_m DESC LIMIT 2"""
    for r in await con.fetch(q, 2025):
        add(
            f"adrev-{r['service_name'][:6].lower().strip('+ ')}",
            f"What was {r['service_name']}'s advertising revenue in FY2025?",
            "Advertising is unbundled and safely attributable, unlike the subscription line.",
            [decimal_forms(float(r["revenue_usd_m"]))],
            q,
            float(r["revenue_usd_m"]),
        )

    return cases[:count]


async def main_async(count: int) -> None:
    con = await asyncpg.connect(
        get_settings().require_analyst_database_url(), statement_cache_size=0, timeout=30
    )
    try:
        cases = await build(con, count)
    finally:
        await con.close()

    OUT.write_text(json.dumps(cases, indent=2) + "\n")
    print(f"{len(cases)} cases -> {OUT}\n")
    for c in cases:
        print(f"  {c['id']:<24} {c['question']}")
        print(f"  {'':<24} expects: {c['expected']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--count", type=int, default=20)
    asyncio.run(main_async(p.parse_args().count))


if __name__ == "__main__":
    main()
