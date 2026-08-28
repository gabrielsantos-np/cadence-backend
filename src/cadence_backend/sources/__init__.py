"""Every source the analyst can reach.

Adding one is a two-step change: write a module exporting a SqlSource or a
DocumentSource, then add it to SOURCES below. The tool schemas, the source
enums the model picks from, and the schema context in the system prompt are all
derived from this list — nothing else needs editing.

When this reaches three or more SQL sources, add a routing step before the main
loop (one cheap model call: "which sources can answer this?") so the prompt does
not carry every schema on every request. Below three, the routing call costs
more than it saves.
"""

from cadence_backend.core.config import get_settings
from cadence_backend.sources.corpus import corpus_source
from cadence_backend.sources.notes import notes_source
from cadence_backend.sources.types import DataSource, DocumentSource, SqlSource


def _market_sources() -> list[SqlSource]:
    """The SQL warehouses the analyst can reach, per MARKET_SOURCE.

    `postgres` and `snowflake` register one each, which is what the migration
    comparison needed: same question, same prompt, one variable.

    `both` registers them together, and that is a different thing rather than a
    superset. The two warehouses hold different halves of the dataset — the
    929k-row panel and the document corpus are on Supabase, the general ledger
    and the helpdesk are on Snowflake — so a question spanning them is only
    answerable with both registered. The cost is roughly 4,200 tokens of schema
    in every prompt instead of 1,500 or 2,700, and a model that now has to
    choose. `build_source_context` tells it how.
    """
    mode = get_settings().market_source
    sources: list[SqlSource] = []
    if mode in ("postgres", "both"):
        from cadence_backend.sources.market import market_source

        sources.append(market_source)
    if mode in ("snowflake", "both"):
        from cadence_backend.sources.snowflake import snowflake_source

        sources.append(snowflake_source)
    return sources


# The curated notes stay alongside the corpus rather than being folded into
# it. They are short methodology guidance the analyst leans on for the refusal
# cases, and twenty thousand documents of market prose would bury them.
SOURCES: list[DataSource] = [*_market_sources(), notes_source, corpus_source]

__all__ = [
    "SOURCES",
    "DataSource",
    "DocumentSource",
    "SqlSource",
    "build_source_context",
    "document_sources",
    "find_document_source",
    "find_sql_source",
    "sql_sources",
]


def sql_sources() -> list[SqlSource]:
    return [s for s in SOURCES if s.kind == "sql"]


def document_sources() -> list[DocumentSource]:
    return [s for s in SOURCES if s.kind == "documents"]


def find_sql_source(source_id: str) -> SqlSource:
    for source in sql_sources():
        if source.id == source_id:
            return source
    available = ", ".join(s.id for s in sql_sources())
    raise ValueError(f'Unknown SQL source "{source_id}". Available: {available}.')


def find_document_source(source_id: str) -> DocumentSource:
    for source in document_sources():
        if source.id == source_id:
            return source
    available = ", ".join(s.id for s in document_sources())
    raise ValueError(f'Unknown document source "{source_id}". Available: {available}.')


#: Guidance that only applies when both warehouses are registered. The market
#: census is duplicated across them, so without this the model picks by chance
#: and two runs of the same question can cite different sources for one figure.
#: Everything named here is a real asymmetry, not a preference.
_BOTH_SOURCES_NOTE = """
## Choosing between `market` and `bellweather`

Both carry the same bought-in market census, and for those tables they agree.
They differ in what only one of them has:

- `market` (Supabase) alone has `subscription_event`, the 929k-row 1-in-281
  panel of individual signups, cancels and plan changes, and `mv_event_monthly`,
  its pre-scaled monthly rollup. Anything about *why* a subscriber moved, or any
  count of events, has to come from here.
- `bellweather` (Snowflake) alone has FINANCE and SUPPORT — the general ledger
  and the helpdesk for the two services Bellweather operates. Anything about
  postings, accounts, tickets or resolution times has to come from here.

Prefer `market` for the census and anything panel-derived, so figures in one
answer are drawn consistently. Use `bellweather` for ledger and support
questions, and for the market census only when joining it to those in one query.
Questions that need both — a billing variance explained by a ledger posting and
checked against subscriber movement — should query each for its own half rather
than looking for one source that has everything.
"""


def build_source_context() -> str:
    """The source catalogue and every SQL schema, assembled for the prompt."""
    catalogue = "\n".join(f"- {s.id} ({s.kind}) — {s.name}. {s.description}" for s in SOURCES)
    schemas = "\n\n".join(
        f"## Source `{s.id}` — {s.name}\n\n{s.schema_context}" for s in sql_sources()
    )
    routing = _BOTH_SOURCES_NOTE if len(sql_sources()) > 1 else ""
    return f"# Available sources\n\n{catalogue}\n{routing}\n{schemas}"
