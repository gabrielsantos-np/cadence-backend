"""BM25 over the Supabase corpus, without holding the corpus in memory.

Three things had to be true at once: the data lives in Supabase, the ranking is
the BM25 that won the benchmark, and the process does not carry 216MB of text.

Postgres full-text search cannot do it here. The corpus has only 1,339 distinct
lexemes, so nothing is selective: OR semantics match 73% of chunks and take 30
seconds to rank, AND semantics match none. That is a property of the corpus
vocabulary, not of Postgres — and it is exactly the condition under which an
inverted index stops helping.

What works is to split the job. Postgres already parsed every chunk into a
tsvector, so the *terms* come from the database while the *text* stays there:
the index is built from tsvectors at startup (tens of megabytes, because there
are only 1,339 terms) and the text of the handful of ranked chunks is fetched
on demand. Scoring is exact BM25 over the whole corpus, which is what the
in-memory arm measured at 165ms.
"""

import logging
import math
import re
import time
from array import array
from dataclasses import dataclass

import asyncpg
import numpy as np

logger = logging.getLogger(__name__)

#: tsvector renders as `'lexeme':1,4 'other':7`. Positions are irrelevant here;
#: BM25 needs the term and how often it occurs.
_ENTRY = re.compile(r"'((?:[^']|'')+)':([\d,ABCD]+)")
_WORD = re.compile(r"\W+")

# Paginated. The tsvectors total roughly 119MB, and asking the pooler for all
# of them in one fetch drops the connection mid-operation.
LOAD = """
SELECT chunk_id, doc_id, span_start, span_end, fts::text
  FROM corpus.chunk
 WHERE chunk_id > $1
 ORDER BY chunk_id
 LIMIT $2
"""
LOAD_PAGE = 20_000

FETCH_TEXT = """
SELECT c.chunk_id,
       substr(d.body, c.span_start + 1, c.span_end - c.span_start) AS text,
       d.title, d.publisher, d.reference
  FROM corpus.chunk c JOIN corpus.document d USING (doc_id)
 WHERE c.chunk_id = ANY($1::int[])
"""


@dataclass(frozen=True)
class Ranked:
    chunk_id: int
    doc_id: int
    span_start: int
    span_end: int
    score: float


class SupabaseBM25:
    """Exact BM25 scored in process, over an index built from Supabase.

    Postings live in numpy arrays, not Python lists of tuples. That is not a
    micro-optimisation: this corpus has 12.6 million postings, and holding them
    as tuples measured at 1,217MB resident — more than holding the raw text,
    which was the thing this design existed to avoid. The same data as two
    typed arrays is about 76MB.
    """

    name = "supabase-bm25"

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.terms: dict[str, tuple[int, int]] = {}
        self.chunk_pos = np.zeros(0, dtype=np.int32)
        self.freqs = np.zeros(0, dtype=np.int16)
        self.idf: dict[str, float] = {}
        self.lengths = np.zeros(0, dtype=np.int32)
        self.chunk_ids = np.zeros(0, dtype=np.int32)
        self.meta: dict[int, tuple[int, int, int]] = {}
        self.avg_len = 1.0
        self.loaded = False

    async def load(self, db: asyncpg.Connection | asyncpg.Pool) -> None:
        """Build the index from the parsed tsvectors already in the database."""
        started = time.perf_counter()

        # Accumulate into array('i'), not lists of tuples, and discard each
        # page as it is consumed. The tuple version peaked at 1,302MB resident
        # during the build — more than holding the raw text, which is the cost
        # this design exists to avoid. These are four bytes per posting.
        positions: dict[str, array] = {}
        counts: dict[str, array] = {}
        ids = array("i")
        lengths = array("i")

        cursor_id = 0
        position = 0
        while True:
            page = await db.fetch(LOAD, cursor_id, LOAD_PAGE)
            if not page:
                break
            for row in page:
                chunk_id = row["chunk_id"]
                ids.append(chunk_id)
                self.meta[chunk_id] = (row["doc_id"], row["span_start"], row["span_end"])
                length = 0
                for lexeme, seen_at in _ENTRY.findall(row["fts"] or ""):
                    count = seen_at.count(",") + 1
                    term = lexeme.replace("''", "'")
                    if term not in positions:
                        positions[term] = array("i")
                        counts[term] = array("i")
                    positions[term].append(position)
                    counts[term].append(count)
                    length += count
                lengths.append(length)
                position += 1
            cursor_id = page[-1]["chunk_id"]
            del page

        self.chunk_ids = np.frombuffer(ids, dtype=np.int32).copy()
        self.lengths = np.frombuffer(lengths, dtype=np.int32).copy()
        n = len(self.chunk_ids)
        self.avg_len = float(self.lengths.mean()) if n else 1.0

        total = sum(len(v) for v in positions.values())
        self.chunk_pos = np.empty(total, dtype=np.int32)
        self.freqs = np.empty(total, dtype=np.int16)
        cursor = 0
        for term in list(positions):
            width = len(positions[term])
            self.terms[term] = (cursor, cursor + width)
            self.chunk_pos[cursor : cursor + width] = np.frombuffer(
                positions.pop(term), dtype=np.int32
            )
            self.freqs[cursor : cursor + width] = np.minimum(
                np.frombuffer(counts.pop(term), dtype=np.int32), 32767
            )
            cursor += width
            self.idf[term] = math.log(1 + (n - width + 0.5) / (width + 0.5))

        self.loaded = True
        logger.info(
            "BM25 index: %d chunks, %d terms, %d postings, built in %.1fs",
            n,
            len(self.terms),
            total,
            time.perf_counter() - started,
        )

    def rank(self, query: str, k: int) -> list[Ranked]:
        if not self.loaded or not len(self.chunk_ids):
            return []
        scores = np.zeros(len(self.chunk_ids), dtype=np.float32)

        # Postgres stems the index but not this query, so a short prefix walk
        # stands in for the stemmer: 'cancellations' should reach 'cancel'.
        seen: set[str] = set()
        for raw_term in (t.lower() for t in _WORD.split(query) if t):
            for term in (raw_term, raw_term[:-1], raw_term[:-2]):
                if term in self.terms and term not in seen:
                    seen.add(term)
                    lo, hi = self.terms[term]
                    positions = self.chunk_pos[lo:hi]
                    freq = self.freqs[lo:hi].astype(np.float32)
                    norm = 1 - self.b + self.b * self.lengths[positions] / self.avg_len
                    np.add.at(
                        scores,
                        positions,
                        self.idf[term] * freq * (self.k1 + 1) / (freq + self.k1 * norm),
                    )
                    break

        hits = int(min(k, np.count_nonzero(scores)))
        if not hits:
            return []
        top = np.argpartition(-scores, hits - 1)[:hits]
        top = top[np.argsort(-scores[top])]
        return [
            Ranked(int(self.chunk_ids[i]), *self.meta[int(self.chunk_ids[i])], float(scores[i]))
            for i in top
        ]

    async def fetch_text(
        self, db: asyncpg.Connection | asyncpg.Pool, ranked: list[Ranked]
    ) -> dict[int, asyncpg.Record]:
        """Text for the ranked chunks only — never the whole corpus."""
        if not ranked:
            return {}
        rows = await db.fetch(FETCH_TEXT, [r.chunk_id for r in ranked])
        return {row["chunk_id"]: row for row in rows}


def index_bytes(index: SupabaseBM25) -> float:
    """Bytes actually held by the arrays, in MB."""
    return (
        index.chunk_pos.nbytes + index.freqs.nbytes + index.lengths.nbytes + index.chunk_ids.nbytes
    ) / 1e6
