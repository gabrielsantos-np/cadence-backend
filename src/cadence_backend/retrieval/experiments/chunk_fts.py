"""Chunk-level full-text search in Postgres.

The retriever the analyst actually uses. In-memory BM25 won the benchmark but
holds every chunk in the process — 216MB of text — which is not shippable.
Document-level FTS was the obvious substitute and lost badly (recall@5 0.023
against 0.419) because `ts_rank_cd` scored whole 15KB documents.

So the index is on chunks. Same engine, right granularity.

Chunks store a tsvector and their offsets but not their text; the text for the
handful of returned rows is sliced out of the document. That is what keeps the
corpus inside the free tier.
"""

import logging
import re

import asyncpg

logger = logging.getLogger(__name__)

#: OR semantics, not AND. `websearch_to_tsquery` ANDs unquoted terms, so a
#: natural question demands every content word in one chunk and matches
#: nothing — measured at a flat zero across every candidate depth. Ranking
#: wants "match anything, then sort by how much".
SEARCH = """
WITH q AS (
    SELECT to_tsquery(
        'english',
        array_to_string(
            ARRAY(SELECT quote_literal(lexeme)
                    FROM unnest(to_tsvector('english', $1))),
            ' | '
        )
    ) AS tsq
)
SELECT c.doc_id,
       c.span_start,
       c.span_end,
       ts_rank_cd(c.fts, q.tsq) AS rank,
       substr(d.body, c.span_start + 1, c.span_end - c.span_start) AS text,
       d.title,
       d.publisher,
       d.reference
  FROM corpus.chunk c
  JOIN corpus.document d USING (doc_id),
       q
 WHERE c.fts @@ q.tsq
 ORDER BY rank DESC
 LIMIT $2
"""

_WORD = re.compile(r"\W+")


class ChunkFTS:
    """Rank chunks by Postgres full-text relevance."""

    name = "postgres-chunk-fts"

    def __init__(self, db: asyncpg.Connection | asyncpg.Pool) -> None:
        self.db = db

    async def rows(self, query: str, k: int) -> list[asyncpg.Record]:
        if not [t for t in _WORD.split(query) if t]:
            return []
        try:
            return await self.db.fetch(SEARCH, query, k)
        except asyncpg.PostgresError:
            # A query of only stop words yields an empty tsquery. Returning
            # nothing is correct; raising would burn an analyst turn on an
            # error that looks retryable and never recovers.
            logger.warning("full-text query failed for %r", query, exc_info=True)
            return []
