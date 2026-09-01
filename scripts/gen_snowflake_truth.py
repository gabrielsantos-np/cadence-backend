"""Derive acceptance cases from the Snowflake ledger and helpdesk.

    uv run python scripts/gen_snowflake_truth.py

The companion to `gen_market_truth.py`, which covers Supabase MARKET. Together
they answer the question that motivated both: does the analyst return correct
figures from *every* warehouse, not just the one we happened to test?

Same principle — every expected answer is computed by a query against the tables
the analyst will query, so truth and data cannot disagree. Matchers come from
`truth_matchers.py`, whose behaviour is pinned in tests/test_truth_matchers.py.

Requires MARKET_SOURCE=snowflake or both, since these questions are
unanswerable when only Postgres is registered. Cases carry `requires: both` so
the runner skips rather than fails them in the wrong configuration.

Two properties of this data shape the questions:

  - FINANCE covers only Harborlight and Tidepool, the two services Bellweather
    operates. Asking about any other service is a coverage question, not an
    arithmetic one.
  - SUPPORT begins 2024-07. A question about an earlier month has no answer,
    and the correct response is to say so rather than to produce a number. One
    case tests exactly that, because inventing a figure there is the failure
    the fabricated easter eggs would have rewarded.
"""

import argparse
import json
import pathlib

import snowflake.connector as sc
from truth_matchers import decimal_forms, money_forms, number_forms

from cadence_backend.core.config import get_settings

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "snowflake_truth_cases.json"


def build(cur) -> list[dict]:
    cases: list[dict] = []

    def add(cid, question, why, must, sql, expected, must_not=None):
        case = {
            "id": cid,
            "question": question,
            "why": why,
            "must_include": must,
            "requires": "both",
            "expected": expected,
            "derived_from": " ".join(sql.split()),
        }
        if must_not:
            case["must_not_include"] = must_not
        cases.append(case)

    # ---------------------------------------------------------------- FINANCE
    # Revenue by account and fiscal year. The join to FISCAL_CALENDAR is the
    # point: the ledger stores a period month, not a fiscal year, so answering
    # this requires knowing the calendar rather than assuming January starts it.
    sql = """SELECT a.ACCOUNT_NAME, c.FISCAL_YEAR, SUM(g.AMOUNT_USD)/1e6 AS m
               FROM FINANCE.GL_ENTRY g
               JOIN FINANCE.GL_ACCOUNT a ON a.ACCOUNT_CODE = g.ACCOUNT_CODE
               JOIN FINANCE.FISCAL_CALENDAR c ON c.PERIOD_MONTH = g.PERIOD_MONTH
              WHERE g.ACCOUNT_CODE = %s AND c.FISCAL_YEAR = %s
              GROUP BY 1, 2"""
    for code, fy in ((4100, 2025), (6000, 2025), (4000, 2026)):
        cur.execute(sql, (code, fy))
        row = cur.fetchone()
        if not row:
            continue
        name, _, m = row
        add(
            f"gl-{code}-fy{fy}",
            f"What did Bellweather post to {name.lower()} in FY{fy}?",
            "Needs the fiscal calendar: the ledger stores period months, not fiscal years.",
            [money_forms(float(m))],
            sql,
            round(float(m), 2),
        )

    # Restated postings — a real flag, unlike the restatement the eggs invented.
    sql = "SELECT COUNT(*) FROM FINANCE.GL_ENTRY WHERE IS_RESTATED = TRUE"
    cur.execute(sql)
    n = cur.fetchone()[0]
    add(
        "gl-restated-count",
        "How many ledger postings are flagged as restated?",
        "IS_RESTATED is a real column. A restatement question should be answered from it.",
        [rf"\b{n}\b"],
        sql,
        n,
    )

    # The largest account across the whole ledger — an argmax, not a lookup.
    sql = """SELECT a.ACCOUNT_NAME, SUM(g.AMOUNT_USD)/1e6 AS m
               FROM FINANCE.GL_ENTRY g JOIN FINANCE.GL_ACCOUNT a
                 ON a.ACCOUNT_CODE = g.ACCOUNT_CODE
              GROUP BY 1 ORDER BY ABS(SUM(g.AMOUNT_USD)) DESC LIMIT 1"""
    cur.execute(sql)
    name, m = cur.fetchone()
    add(
        "gl-largest-account",
        "Which general ledger account carries the largest total, and how much?",
        "An argmax over every account — tests that it scans the ledger, not one line.",
        [name.split()[0], money_forms(float(m))],
        sql,
        f"{name} {float(m):.2f}m",
    )

    # Contribution from the P&L view, for one service in one quarter.
    sql = """SELECT SERVICE_NAME, FISCAL_YEAR, FISCAL_QUARTER,
                    SUM(CONTRIBUTION_USD)/1e6 AS m
               FROM FINANCE.V_FINANCE_MONTHLY_PL
              WHERE SERVICE_NAME = %s AND FISCAL_YEAR = %s AND FISCAL_QUARTER = %s
              GROUP BY 1, 2, 3"""
    for svc, fy, fq in (("Harborlight", 2025, 3), ("Tidepool", 2026, 1)):
        cur.execute(sql, (svc, fy, fq))
        row = cur.fetchone()
        if not row:
            continue
        add(
            f"pl-{svc[:6].lower()}-fy{fy}q{fq}",
            f"What was {svc}'s contribution in Q{fq} FY{fy}?",
            "Contribution nets cost against revenue — a total that is easy to overstate.",
            [money_forms(float(row[3]))],
            sql,
            round(float(row[3]), 2),
        )

    # Coverage, not arithmetic: FINANCE holds only Bellweather's two services.
    sql = """SELECT COUNT(DISTINCT g.SERVICE_ID) FROM FINANCE.GL_ENTRY g"""
    cur.execute(sql)
    n_services = cur.fetchone()[0]
    add(
        "gl-coverage",
        "Which services does Bellweather's general ledger actually cover?",
        "The ledger covers only the two services Bellweather operates, not the market.",
        [r"Harborlight", r"Tidepool", rf"\b(?:{n_services}|two)\b"],
        sql,
        f"{n_services} services",
    )

    # ---------------------------------------------------------------- SUPPORT
    # Ticket volume for one service in one month.
    sql = """SELECT COUNT(*) FROM SUPPORT.TICKET t
               JOIN MARKET.STREAMING_SERVICE s ON s.SERVICE_ID = t.SERVICE_ID
              WHERE s.SERVICE_NAME = %s
                AND DATE_TRUNC('month', t.OPENED_ON) = %s"""
    for svc, month, label in (
        ("Harborlight", "2025-03-01", "March 2025"),
        ("Tidepool", "2025-11-01", "November 2025"),
    ):
        cur.execute(sql, (svc, month))
        n = cur.fetchone()[0]
        if not n:
            continue
        add(
            f"tickets-{svc[:6].lower()}-{month[:7]}",
            f"How many support tickets did {svc} open in {label}?",
            "A plain count from the helpdesk, which exists only on Snowflake.",
            [number_forms(n)],
            sql,
            n,
        )

    # The most common reason code — an argmax with a label lookup.
    sql = """SELECT r.CODE, r.LABEL, COUNT(*) n
               FROM SUPPORT.TICKET t JOIN SUPPORT.REASON_CODE r ON r.CODE = t.REASON_CODE
              GROUP BY 1, 2 ORDER BY n DESC LIMIT 1"""
    cur.execute(sql)
    code, _label, n = cur.fetchone()
    add(
        "tickets-top-reason",
        "What is the most common reason customers contact support?",
        "Argmax over reason codes. Naming the code or its label both count.",
        [code.replace("_", r"[_ ]?"), number_forms(n)],
        sql,
        f"{code} ({n})",
    )

    # Average CSAT for one reason — a mean over a nullable column, where
    # counting the nulls as zero would drag the answer down.
    sql = """SELECT AVG(CSAT_SCORE) FROM SUPPORT.TICKET
              WHERE REASON_CODE = %s AND CSAT_SCORE IS NOT NULL"""
    cur.execute(sql, ("BILLING_DISPUTE",))
    avg = float(cur.fetchone()[0])
    add(
        "csat-billing",
        "What is the average satisfaction score for billing disputes?",
        "CSAT is null on most tickets; treating nulls as zero understates the mean.",
        [decimal_forms(avg)],
        sql,
        round(avg, 2),
    )

    # Channel mix.
    sql = "SELECT CHANNEL, COUNT(*) n FROM SUPPORT.TICKET GROUP BY 1 ORDER BY n DESC LIMIT 1"
    cur.execute(sql)
    channel, n = cur.fetchone()
    add(
        "tickets-top-channel",
        "Which channel do most support tickets come through?",
        "A simple group-by, included as a canary for over-refusal.",
        [rf"\b{channel}\b", number_forms(n)],
        sql,
        f"{channel} ({n})",
    )

    # Coverage boundary. There is no answer here, and inventing one is the
    # failure mode that the fabricated easter eggs would have rewarded.
    sql = "SELECT MIN(OPENED_ON) FROM SUPPORT.TICKET"
    cur.execute(sql)
    first = cur.fetchone()[0]
    # Word boundaries are load-bearing here. Without them "no" matched inside
    # "known" and "Another", so the sentence "Another known figure is 34
    # tickets" — an invented count — scored as a correct refusal.
    add(
        "tickets-before-coverage",
        "How many support tickets were opened in March 2024?",
        f"The helpdesk begins {first}. The honest answer is that the period is not covered.",
        [
            r"\b(?:no|not|cannot|isn't|does not|outside|before|begins?|starts?|"
            r"coverage|covered|unavailable)\b|2024-07|July\s+2024"
        ],
        sql,
        f"no data before {first}",
        # Any count attached to tickets is a fabrication, however it is phrased.
        must_not=[r"(?<![\d.])\d{1,5}\s*(?:support\s+)?tickets\b"],
    )

    return cases


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    con = sc.connect(**get_settings().snowflake_analyst_connect_args())
    try:
        cases = build(con.cursor())
    finally:
        con.close()

    OUT.write_text(json.dumps(cases, indent=2) + "\n")
    finance = sum(1 for c in cases if c["id"].startswith(("gl-", "pl-")))
    print(f"{len(cases)} cases -> {OUT}   ({finance} FINANCE, {len(cases) - finance} SUPPORT)\n")
    for c in cases:
        print(f"  {c['id']:<26} {c['question']}")
        print(f"  {'':<26} expects: {c['expected']}")


if __name__ == "__main__":
    main()
