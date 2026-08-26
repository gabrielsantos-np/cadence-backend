"""The market-research corpus, searched with BM25.

Replaces counting word overlap across six hardcoded notes with ranked retrieval
over twenty thousand documents. BM25 was the benchmark winner — 1.9x the recall
of the term-overlap ranking it replaces, at a fraction of the latency — and
`retrieval/supabase_bm25.py` explains why it is served this way rather than
through Postgres full-text search.

The six curated notes stay registered alongside this, deliberately. They are
methodology guidance the analyst leans on for the refusal cases, and burying
them in a hundred thousand chunks of market prose would likely cost the
guardrails that make a wrong answer a refusal instead.
"""

import asyncio
import logging
from typing import Literal

from cadence_backend.db import app_pool
from cadence_backend.retrieval.supabase_bm25 import SupabaseBM25
from cadence_backend.schemas.trace import SearchResult

logger = logging.getLogger(__name__)

#: Enough for the model to corroborate a claim across documents without
#: crowding out the SQL results the answer is actually built from. Every tool
#: result stays in the context for the rest of the run.
TOP_K = 6

#: A chunk runs to a couple of thousand characters; the analyst needs the
#: passage, not the surrounding paragraph it happened to be packed with.
SNIPPET_LIMIT = 900


class CorpusSource:
    id = "corpus"
    kind: Literal["documents"] = "documents"
    name = "Market research corpus"
    description = (
        "Analyst research notes, earnings-call transcripts, press releases, provider "
        "methodology documentation, support articles, internal memos and trade press "
        "covering the US streaming market. Search it when a figure needs explaining, "
        "when a question asks why something happened, or when the warehouse shows an "
        "effect but not its cause."
    )

    def __init__(self) -> None:
        self._index = SupabaseBM25()
        # One build, not one per request. Two concurrent first requests would
        # otherwise each spend three minutes building the same index.
        self._ready = asyncio.Lock()

    async def _index_ready(self) -> SupabaseBM25 | None:
        if self._index.loaded:
            return self._index
        async with self._ready:
            if self._index.loaded:
                return self._index
            try:
                await self._index.load(await app_pool())
            except Exception:
                # Degrade to no corpus rather than failing the search. The
                # engine turns a raised error into a retry the model cannot
                # recover from, burning turns from a budget of fourteen.
                logger.exception("could not build the corpus index; corpus search disabled")
                return None
        return self._index

    async def search(self, query: str) -> list[SearchResult]:
        index = await self._index_ready()
        if index is None:
            return []

        ranked = index.rank(query, TOP_K)
        if not ranked:
            return []

        rows = await index.fetch_text(await app_pool(), ranked)
        results = []
        for hit in ranked:
            row = rows.get(hit.chunk_id)
            if row is None:
                continue
            results.append(
                SearchResult(
                    title=row["title"],
                    source=row["publisher"],
                    reference=row["reference"],
                    snippet=row["text"][:SNIPPET_LIMIT].strip(),
                    score=round(hit.score, 4),
                )
            )
        return results


corpus_source = CorpusSource()
