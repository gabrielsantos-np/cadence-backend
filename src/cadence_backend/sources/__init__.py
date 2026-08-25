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
from cadence_backend.sources.notes import notes_source
from cadence_backend.sources.types import DataSource, DocumentSource, SqlSource


def _market_source() -> SqlSource:
    """The market dataset, from whichever warehouse is configured.

    One SQL source at a time, deliberately. Registering both would put both
    schemas in every prompt and make the model choose, which changes how it
    answers — the point of the switch is to compare like with like.
    """
    if get_settings().market_source == "snowflake":
        from cadence_backend.sources.snowflake import snowflake_source

        return snowflake_source

    from cadence_backend.sources.market import market_source

    return market_source


SOURCES: list[DataSource] = [_market_source(), notes_source]

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


def build_source_context() -> str:
    """The source catalogue and every SQL schema, assembled for the prompt."""
    catalogue = "\n".join(f"- {s.id} ({s.kind}) — {s.name}. {s.description}" for s in SOURCES)
    schemas = "\n\n".join(
        f"## Source `{s.id}` — {s.name}\n\n{s.schema_context}" for s in sql_sources()
    )
    return f"# Available sources\n\n{catalogue}\n\n{schemas}"
