"""The schema description for the Snowflake copy of the market dataset.

The table, grain and trap documentation is shared verbatim with the Postgres
source — those facts are about the data, not the engine, and duplicating them
is how the two drift. Only the dialect notes below are Snowflake-specific, and
they exist to stop the analyst writing Postgres SQL that fails on the first
attempt and burns a turn repairing it.
"""

from cadence_backend.sources.market_schema import MARKET_SCHEMA_CONTEXT

SNOWFLAKE_DIALECT_NOTES = """
## Writing SQL for this source

This source is Snowflake, not Postgres. The tables, columns and traps above are
identical; the dialect is not.

- Unquoted identifiers are case-insensitive and resolve to upper case. Write
  them lower case as documented — it works — but expect result column names
  to come back upper case.
- There is no `DISTINCT ON`. For "the latest row per group", use
  `QUALIFY ROW_NUMBER() OVER (PARTITION BY x ORDER BY y DESC) = 1`.
- There is no aggregate `FILTER (WHERE ...)`. Use
  `SUM(CASE WHEN cond THEN col END)`.
- `BOOL_OR` is `BOOLOR_AGG`. `STRING_AGG(x, ', ' ORDER BY y)` is
  `LISTAGG(x, ', ') WITHIN GROUP (ORDER BY y)`.
- There is no `AGE()`. For a month difference use
  `DATEDIFF('month', earlier, later)`.
- `EXTRACT(YEAR FROM col)`, `DATE_TRUNC('month', col)`, `::` casts, window
  functions, CTEs and `LIMIT` all behave as you expect.
- One statement per query, `SELECT` or `WITH` only.
""".strip()


SNOWFLAKE_SCHEMA_CONTEXT = f"{MARKET_SCHEMA_CONTEXT}\n\n{SNOWFLAKE_DIALECT_NOTES}"
