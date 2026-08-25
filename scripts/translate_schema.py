"""Generate the Snowflake schema from the Postgres one.

`data/schema.sql` is the source of truth for the dataset. This regenerates
`data/snowflake/schema.sql` from it so the two cannot drift.

Every rule below is an exact-match replacement that raises if it does not fire.
A silent miss would produce SQL that loads but answers differently — the views
carry the dataset's traps, so a mistranslated view is a wrong answer, not a
crash. Run it after any change to the Postgres schema:

    uv run python scripts/translate_schema.py
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "schema.sql"
TARGET = ROOT / "data" / "snowflake" / "schema.sql"

HEADER = """\
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

"""

# --- exact-match rewrites ---------------------------------------------------
# (label, before, after). Order matters: FILTER rules run before the bare
# BOOL_OR rule, so the filtered form is not matched twice.
REWRITES: list[tuple[str, str, str]] = [
    (
        "v_service_overview: SUM ... FILTER (subscriptions)",
        """        SUM(ar.revenue_usd_m) FILTER (WHERE ar.revenue_line = 'subscriptions')
            AS subscription_revenue_usd_m,""",
        """        SUM(CASE WHEN ar.revenue_line = 'subscriptions'
                 THEN ar.revenue_usd_m END)
            AS subscription_revenue_usd_m,""",
    ),
    (
        "v_service_overview: BOOL_OR ... FILTER (subscriptions)",
        """        BOOL_OR(ar.is_bundled) FILTER (WHERE ar.revenue_line = 'subscriptions')
            AS subscription_is_bundled,""",
        """        BOOLOR_AGG(CASE WHEN ar.revenue_line = 'subscriptions'
                        THEN ar.is_bundled END)
            AS subscription_is_bundled,""",
    ),
    (
        "v_service_overview: SUM ... FILTER (other revenue)",
        """        SUM(ar.revenue_usd_m) FILTER (WHERE ar.revenue_line <> 'subscriptions')
            AS other_revenue_usd_m""",
        """        SUM(CASE WHEN ar.revenue_line <> 'subscriptions'
                 THEN ar.revenue_usd_m END)
            AS other_revenue_usd_m""",
    ),
    (
        "v_revenue_per_subscriber: bare BOOL_OR",
        "        BOOL_OR(is_bundled)                                    AS any_line_bundled",
        "        BOOLOR_AGG(is_bundled)                                 AS any_line_bundled",
    ),
    (
        "v_competitive_sets: STRING_AGG -> LISTAGG",
        "    STRING_AGG(g.genre_name, ', ' ORDER BY g.genre_name) AS shared_genre_names",
        "    LISTAGG(g.genre_name, ', ') WITHIN GROUP (ORDER BY g.genre_name)\n"
        "        AS shared_genre_names",
    ),
    (
        "v_genre_market: DISTINCT ON -> QUALIFY",
        """    SELECT DISTINCT ON (ge.genre_id, ge.month)
        ge.genre_id,
        ge.month,
        s.service_name    AS leading_service,
        ge.viewing_hours_m AS leading_viewing_hours_m
    FROM genre_engagement ge
    JOIN streaming_service s ON s.service_id = ge.service_id
    ORDER BY ge.genre_id, ge.month, ge.viewing_hours_m DESC, s.service_name""",
        """    SELECT
        ge.genre_id,
        ge.month,
        s.service_name    AS leading_service,
        ge.viewing_hours_m AS leading_viewing_hours_m
    FROM genre_engagement ge
    JOIN streaming_service s ON s.service_id = ge.service_id
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY ge.genre_id, ge.month
        ORDER BY ge.viewing_hours_m DESC, s.service_name
    ) = 1""",
    ),
    (
        "v_service_overview: latest_subs DISTINCT ON -> QUALIFY",
        """    SELECT DISTINCT ON (service_id)
        service_id, month AS latest_month, subscribers_eom
    FROM monthly_subscribers
    ORDER BY service_id, month DESC""",
        """    SELECT
        service_id, month AS latest_month, subscribers_eom
    FROM monthly_subscribers
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY service_id ORDER BY month DESC
    ) = 1""",
    ),
    (
        "v_service_overview: latest_ret DISTINCT ON -> QUALIFY",
        """    SELECT DISTINCT ON (service_id)
        service_id, quarter_start AS latest_quarter,
        retained_m1_pct, retained_m3_pct, retained_m6_pct, avg_tenure_months
    FROM retention_cohorts
    ORDER BY service_id, quarter_start DESC""",
        """    SELECT
        service_id, quarter_start AS latest_quarter,
        retained_m1_pct, retained_m3_pct, retained_m6_pct, avg_tenure_months
    FROM retention_cohorts
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY service_id ORDER BY quarter_start DESC
    ) = 1""",
    ),
    (
        "v_price_change_impact: AGE -> DATEDIFF",
        """        (DATE_PART('year',  AGE(ms.month, DATE_TRUNC('month', pe.event_date))) * 12
       + DATE_PART('month', AGE(ms.month, DATE_TRUNC('month', pe.event_date))))::INT
            AS months_from_event""",
        """        DATEDIFF('month', DATE_TRUNC('month', pe.event_date), ms.month)::INT
            AS months_from_event""",
    ),
    (
        "v_entrant_ramp: AGE -> DATEDIFF",
        """    (DATE_PART('year',  AGE(ms.month, fm.first_observed_month)) * 12
   + DATE_PART('month', AGE(ms.month, fm.first_observed_month)))::INT + 1
        AS month_index""",
        """    DATEDIFF('month', fm.first_observed_month, ms.month)::INT + 1
        AS month_index""",
    ),
]

# A named table constraint, e.g.
#     CONSTRAINT monthly_subscribers_month_is_first_of_month
#         CHECK (EXTRACT(DAY FROM month) = 1)
# The whole clause has to go: stripping only the CHECK would leave an orphaned
# CONSTRAINT name, which is a syntax error rather than a dropped constraint.
# Runs before CHECK_INLINE so the bare-CHECK rule cannot strip half of it.
NAMED_CHECK = re.compile(
    r",?\s*CONSTRAINT\s+\w+\s+CHECK \((?:[^()]|\([^()]*\))*\)"
)

# Inline CHECK on a column definition.
CHECK_INLINE = re.compile(r"\s+CHECK \((?:[^()]|\([^()]*\))*\)")
CREATE_INDEX = re.compile(r"^CREATE INDEX[^;]*;\s*$", re.MULTILINE)

# `'one ' 'two'` across lines is SQL-standard implicit concatenation, which
# Postgres performs and Snowflake does not. Only whitespace may separate the
# quotes, so this cannot match two unrelated literals in a VALUES list — those
# are always separated by a comma or a paren.
ADJACENT_LITERALS = re.compile(r"'\s*\n\s*'")


def translate(sql: str) -> str:
    for label, before, after in REWRITES:
        if before not in sql:
            sys.exit(
                f"rule did not match: {label}\n"
                "The Postgres schema changed. Update scripts/translate_schema.py "
                "rather than editing the generated file."
            )
        sql = sql.replace(before, after, 1)

    named = len(NAMED_CHECK.findall(sql))
    sql = NAMED_CHECK.sub("", sql)

    checks = len(CHECK_INLINE.findall(sql))
    sql = CHECK_INLINE.sub("", sql)

    indexes = len(CREATE_INDEX.findall(sql))
    sql = CREATE_INDEX.sub("", sql)

    joins = len(ADJACENT_LITERALS.findall(sql))
    sql = ADJACENT_LITERALS.sub("", sql)

    # A column whose only trailing content was a CHECK can be left with a
    # dangling comma before the closing paren.
    sql = re.sub(r",(\s*\n\s*\))", r"\1", sql)
    # Collapse the blank runs left where indexes were.
    sql = re.sub(r"\n{3,}", "\n\n", sql)

    print(f"  rewrites applied : {len(REWRITES)}")
    print(f"  CHECK removed    : {checks} inline, {named} named")
    print(f"  CREATE INDEX rm  : {indexes}")
    print(f"  literals joined  : {joins}")
    return sql


def main() -> None:
    sql = SOURCE.read_text()
    print(f"reading {SOURCE.relative_to(ROOT)} ({len(sql):,} bytes)")
    translated = translate(sql)

    # Checked before the header is prepended: the header documents these
    # constructs by name, and would trip every rule.
    for banned in ("DISTINCT ON", "FILTER (WHERE", "BOOL_OR(", "STRING_AGG(", "AGE("):
        if banned in translated:
            sys.exit(f"unsupported construct still present: {banned}")

    if "CHECK (" in translated:
        sys.exit("a CHECK constraint survived translation")
    orphan = re.search(r"^\s*CONSTRAINT\s+\w+\s*$", translated, re.MULTILINE)
    if orphan:
        sys.exit(f"orphaned constraint name left behind: {orphan.group(0).strip()}")

    out = HEADER + translated
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(out)
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(out):,} bytes)")


if __name__ == "__main__":
    main()
