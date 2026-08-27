"""Research notes, searched by naive term overlap.

The corpus is six notes — anything more sophisticated is overkill.
"""

import re
from typing import Literal

from cadence_backend.schemas.trace import SearchResult
from cadence_backend.sources.notes_data import RESEARCH_NOTES, ResearchNote


def rank(query: str) -> list[ResearchNote]:
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 3]

    scored = []
    for note in RESEARCH_NOTES:
        haystack = f"{note.title} {note.body}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score > 0:
            scored.append((score, note))

    # Sorting on the score alone keeps ties in corpus order; sorting on the
    # note itself would fail, since ResearchNote is not orderable.
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = [note for _, note in scored[:3]]

    # An empty result is worse than a broad one — fall back to the coverage
    # notes, which are the ones most often relevant when a query finds nothing.
    return top or RESEARCH_NOTES[:2]


class NotesSource:
    id = "notes"
    kind: Literal["documents"] = "documents"
    name = "Research notes"
    description = (
        "Provider methodology and coverage notes explaining how the market data was "
        "collected, what each source does and does not measure, and why certain figures "
        "cannot be compared or decomposed."
    )

    async def search(self, query: str, question: str | None = None) -> list[SearchResult]:
        # `question` is accepted for protocol compatibility and deliberately
        # unused. These six notes carry the methodology guidance behind the
        # refusals, and they are matched by word overlap over 1,508 characters —
        # widening the query here changes which note wins for reasons unrelated
        # to the guardrails it protects.
        return [
            SearchResult(
                title=note.title,
                source=note.source,
                reference=note.reference,
                snippet=note.body,
            )
            for note in rank(query)
        ]


notes_source = NotesSource()
