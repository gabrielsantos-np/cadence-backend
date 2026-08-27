"""What a data source is.

Adding a source is two steps: write a module exporting a SqlSource or a
DocumentSource, then add it to SOURCES in this package's registry. The tool
schemas, the source enums the model picks from, and the schema context in the
system prompt are all derived from that list — nothing else needs editing.
"""

from typing import Literal, Protocol, runtime_checkable

from cadence_backend.db import SqlOutcome
from cadence_backend.schemas.trace import SearchResult


@runtime_checkable
class SqlSource(Protocol):
    """A relational source the analyst queries with SQL."""

    id: str
    kind: Literal["sql"]
    name: str
    description: str
    #: Schema description injected into the system prompt. Include the traps —
    #: an introspected table list is exactly what omits them, and those
    #: omissions are what produce confident wrong answers.
    schema_context: str
    #: Label shown on the trace row, e.g. "Queried market dataset".
    trace_label: str

    async def query(self, sql: str) -> SqlOutcome: ...


@runtime_checkable
class DocumentSource(Protocol):
    """A document source the analyst searches with free text."""

    id: str
    kind: Literal["documents"]
    name: str
    description: str

    async def search(self, query: str, question: str | None = None) -> list[SearchResult]:
        """`query` is the analyst's own wording; `question` is the user's, verbatim.

        Both are passed because they are not interchangeable: the analyst tends
        to paraphrase away the entity and period tokens that make a lexical
        search selective, and those are exactly the terms worth keeping.
        """
        ...


DataSource = SqlSource | DocumentSource
