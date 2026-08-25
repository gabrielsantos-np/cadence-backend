"""Engine trace — what the analyst did on its way to an answer.

What the analyst did on its way to an answer, shown in the trace.
"""

from typing import Annotated, Literal

from pydantic import Field

from cadence_backend.schemas.base import CamelModel


class SearchResult(CamelModel):
    title: str
    source: str
    reference: str
    snippet: str


class SqlStep(CamelModel):
    """A text-to-SQL call against a market dataset."""

    id: str
    kind: Literal["sql"] = "sql"
    #: Summary shown on the collapsed row.
    label: str
    duration_ms: int
    #: Where the query ran, e.g. "Queried market dataset".
    source: str
    sql: str
    columns: list[str]
    #: Every cell is stringified by the read-only query layer, which is why
    #: this is not a list of native values.
    rows: list[list[str]]
    #: Total rows matched — may exceed len(rows) when the preview truncates.
    row_count: int


class SearchStep(CamelModel):
    """A search across indexed research notes and provider documentation."""

    id: str
    kind: Literal["search"] = "search"
    label: str
    duration_ms: int
    query: str
    results: list[SearchResult]


class NoteStep(CamelModel):
    """Non-retrieval work: charting, reconciling, checking documented gaps."""

    id: str
    kind: Literal["note"] = "note"
    label: str
    duration_ms: int
    detail: str


TraceStep = Annotated[SqlStep | SearchStep | NoteStep, Field(discriminator="kind")]
