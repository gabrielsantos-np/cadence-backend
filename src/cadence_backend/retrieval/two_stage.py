"""Postgres full-text search, then BM25 over the candidates' chunks.

The benchmark's BM25 arm holds every chunk in memory. At 108,595 chunks that
is roughly 216MB of text in a web process, which is not something to ship.

This gets the same unit of answer without the memory. Postgres narrows 20,000
documents to a few dozen using a GIN index, and only those are chunked and
ranked in process — a few hundred kilobytes rather than a few hundred
megabytes. Two-stage retrieval is what production systems do for exactly this
reason.

It is also why this needs measuring rather than assuming. `ts_rank_cd` is not
BM25: it weights term proximity and document length differently, and the first
stage can drop a document the second stage would have ranked highly. Whether
the benchmarked result survives the substitution is a question with an answer,
so `make bench ARGS="--arms fts"` produces it.
"""

import logging

import asyncpg

from cadence_backend.retrieval.chunking import STRATEGIES, Chunk
from cadence_backend.retrieval.retrievers import BM25, Corpus, Hit

logger = logging.getLogger(__name__)

#: Documents pulled from Postgres before chunk-level ranking. Recall past this
#: point is capped by the first stage, so it trades directly against latency:
#: too few and the answer never reaches stage two.
DEFAULT_CANDIDATES = 60

# websearch_to_tsquery ANDs unquoted terms, which for a natural question means
# demanding every content word appear in one document — it matched zero of
# 20,000 and the arm scored a flat zero at every depth. Ranking wants OR
# semantics: match anything, then let ts_rank_cd sort by how much and how
# closely. Each lexeme is quoted so a token like '-04' cannot break the parse.
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
SELECT d.doc_id, d.body, ts_rank_cd(d.fts, q.tsq) AS rank
  FROM corpus.document d, q
 WHERE d.fts @@ q.tsq
 ORDER BY rank DESC
 LIMIT $2
"""


class PostgresTwoStage:
    """FTS for candidate documents, BM25 for the chunk inside them."""

    def __init__(
        self,
        pool_or_conn: asyncpg.Connection | asyncpg.Pool,
        chunking: str = "fixed-512",
        candidates: int = DEFAULT_CANDIDATES,
    ) -> None:
        self.db = pool_or_conn
        self.chunking = chunking
        self.candidates = candidates
        self.name = f"postgres-fts+bm25@{candidates}"

    async def search(self, query: str, k: int) -> list[Hit]:
        try:
            rows = await self.db.fetch(SEARCH, query, self.candidates)
        except asyncpg.PostgresSyntaxError:
            # websearch_to_tsquery is forgiving, but a query of nothing but
            # stop words yields an empty tsquery and matches nothing. Falling
            # through to an empty result is correct; raising would burn an
            # analyst turn on a retryable-looking error that never recovers.
            logger.warning("unparseable full-text query: %r", query)
            return []

        if not rows:
            return []

        split = STRATEGIES[self.chunking]
        chunks: list[Chunk] = []
        for row in rows:
            chunks.extend(split(row["body"], row["doc_id"]))

        # Stage two ranks only what stage one returned, so this index is built
        # per query over a few hundred chunks rather than over the corpus.
        return await BM25(Corpus(chunks=chunks, strategy=self.chunking)).search(query, k)
