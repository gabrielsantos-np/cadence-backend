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
    #: Relevance, where the source computes one. Optional and omitted rather
    #: than sent as null, so a source that does not score stays byte-identical
    #: on the wire — the same pattern as Finding.so_what. Adding it required a
    #: real field: CamelModel forbids extras, so a score cannot be smuggled
    #: through undeclared.
    score: float | None = None


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
    #: Which document source answered, e.g. "Market research corpus". The
    #: corpus and the six curated notes are different things — one is 119,207
    #: ranked chunks, the other is hand-written methodology guidance — and a
    #: trace that renders them identically hides which one a claim came from.
    #: Optional so the field is omitted rather than sent as null.
    source: str | None = None
    query: str
    results: list[SearchResult]


class NoteStep(CamelModel):
    """Non-retrieval work: composing the answer, or recovering from a failure."""

    id: str
    kind: Literal["note"] = "note"
    label: str
    duration_ms: int
    #: What this step was. Composing the answer and retrying a failed query are
    #: both "not a query and not a search", but they mean opposite things to a
    #: reader — one is the result, the other is a stumble. Optional so the
    #: field is omitted rather than sent as null.
    outcome: Literal["composed", "retry"] | None = None
    detail: str


TraceStep = Annotated[SqlStep | SearchStep | NoteStep, Field(discriminator="kind")]
