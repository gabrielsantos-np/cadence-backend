"""The retrieval arms under test.

One protocol, one class per strategy, so the benchmark can swap them without
knowing what any of them does. Arms compose: rerankers and query transforms
wrap another retriever rather than reimplementing search.

The baseline is deliberately the *current* production ranking, ported verbatim
from sources/notes.py. A benchmark whose floor is a straw man tells you nothing
about whether replacing what you have is worth doing.
"""

import asyncio
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from cadence_backend.retrieval.chunking import Chunk
from cadence_backend.retrieval.embeddings import Embedder, embed_all, normalise

logger = logging.getLogger(__name__)

#: How much of a candidate passage the reranker is shown. Large enough to cover
#: a 1024-token chunk whole; twenty of these is roughly 10k tokens of prompt,
#: which is the real cost of reranking and is charged to the arm.
PASSAGE_CHARS = 4200


@dataclass
class Hit:
    chunk: Chunk
    score: float


@dataclass
class Corpus:
    """Chunks under one chunking strategy, plus whatever an arm needs to index."""

    chunks: list[Chunk]
    strategy: str
    vectors: np.ndarray | None = None

    @property
    def texts(self) -> list[str]:
        return [c.text for c in self.chunks]


class Retriever(Protocol):
    name: str

    async def search(self, query: str, k: int) -> list[Hit]: ...


# --- lexical ----------------------------------------------------------------

_WORD = re.compile(r"\W+")


def tokenise(text: str) -> list[str]:
    return [t for t in _WORD.split(text.lower()) if t]


class TermOverlap:
    """The current production ranking, ported from sources/notes.py.

    Counts how many distinct query terms longer than three characters appear
    anywhere in the text, by substring. No term frequency, no inverse document
    frequency, no length normalisation — so a 16KB earnings call matches almost
    any query, which is precisely the weakness the benchmark should expose.
    """

    name = "term-overlap (current)"

    def __init__(self, corpus: Corpus) -> None:
        self.corpus = corpus
        self._lower = [c.text.lower() for c in corpus.chunks]

    async def search(self, query: str, k: int) -> list[Hit]:
        terms = [t for t in tokenise(query) if len(t) > 3]
        if not terms:
            return []
        scored = []
        for i, text in enumerate(self._lower):
            score = sum(1 for t in terms if t in text)
            if score:
                scored.append((score, i))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [Hit(self.corpus.chunks[i], float(s)) for s, i in scored[:k]]


class BM25:
    """Okapi BM25 — proper lexical weighting.

    The interesting comparison against TermOverlap: same signal, but weighted
    by rarity and normalised for length. If BM25 wins by a lot, the gap is
    length normalisation, not vocabulary.
    """

    name = "bm25"

    def __init__(self, corpus: Corpus, k1: float = 1.5, b: float = 0.75) -> None:
        self.corpus = corpus
        self.k1, self.b = k1, b
        docs = [tokenise(t) for t in corpus.texts]
        self.lengths = np.array([len(d) for d in docs], dtype=np.float32)
        self.avg_len = float(self.lengths.mean()) if len(docs) else 1.0

        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, doc in enumerate(docs):
            for term, count in Counter(doc).items():
                self.postings.setdefault(term, []).append((i, count))
        n = len(docs)
        self.idf = {
            term: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5))
            for term, p in self.postings.items()
        }

    async def search(self, query: str, k: int) -> list[Hit]:
        scores: dict[int, float] = {}
        for term in tokenise(query):
            posting = self.postings.get(term)
            if not posting:
                continue
            idf = self.idf[term]
            for i, freq in posting:
                norm = 1 - self.b + self.b * self.lengths[i] / self.avg_len
                scores[i] = scores.get(i, 0.0) + idf * freq * (self.k1 + 1) / (
                    freq + self.k1 * norm
                )
        top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:k]
        return [Hit(self.corpus.chunks[i], s) for i, s in top]


# --- dense ------------------------------------------------------------------


class Dense:
    """Cosine similarity over embeddings, brute force.

    Exact rather than approximate on purpose: an ANN index would confound
    ranking quality with index recall, and 96k vectors of 256 dimensions is a
    25MB matrix that numpy scans in milliseconds. The pgvector/HNSW arm is
    where approximation gets measured.
    """

    def __init__(self, corpus: Corpus, embedder: Embedder) -> None:
        if corpus.vectors is None:
            raise ValueError("Dense needs an embedded corpus")
        self.corpus = corpus
        self.embedder = embedder
        self.matrix = normalise(corpus.vectors)
        self.name = f"dense-{embedder.dimensions}d"

    async def search(self, query: str, k: int) -> list[Hit]:
        q = normalise(await self.embedder.embed([query]))[0]
        scores = self.matrix @ q
        top = np.argpartition(-scores, min(k, len(scores) - 1))[:k]
        top = top[np.argsort(-scores[top])]
        return [Hit(self.corpus.chunks[i], float(scores[i])) for i in top]


# --- fusion -----------------------------------------------------------------


class HybridRRF:
    """Reciprocal rank fusion over several retrievers.

    RRF rather than score averaging because BM25 scores and cosine similarities
    are not on comparable scales, and normalising them introduces a tuning knob
    that would need its own arm. RRF only reads rank order.
    """

    def __init__(self, arms: list[Retriever], k_rrf: int = 60) -> None:
        self.arms = arms
        self.k_rrf = k_rrf
        self.name = "hybrid-rrf(" + "+".join(a.name.split()[0] for a in arms) + ")"

    async def search(self, query: str, k: int) -> list[Hit]:
        # Over-fetch: fusion only helps if each arm contributes candidates the
        # other missed, and a top-k slice from each rarely disagrees.
        results = await asyncio.gather(*(arm.search(query, k * 4) for arm in self.arms))
        fused: dict[tuple[int, int], float] = {}
        chunks: dict[tuple[int, int], Chunk] = {}
        for hits in results:
            for rank, hit in enumerate(hits):
                key = (hit.chunk.doc_id, hit.chunk.ordinal)
                fused[key] = fused.get(key, 0.0) + 1.0 / (self.k_rrf + rank + 1)
                chunks[key] = hit.chunk
        top = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:k]
        return [Hit(chunks[key], score) for key, score in top]


# --- query transforms -------------------------------------------------------


@dataclass
class QueryRewrite:
    """One LLM call turning a question into a retrieval query.

    Costs a round trip per query, which the benchmark charges to latency and
    to money. Whether it earns that back is the entire point of the arm.
    """

    inner: Retriever
    model: str = "anthropic/claude-haiku-4.5"
    name: str = field(init=False)
    calls: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.name = f"{self.inner.name} + rewrite"

    PROMPT = (
        "Rewrite this analyst question as a search query for a corpus of market "
        "research documents. Keep entity names and dates. Drop question words and "
        "filler. Reply with the query only.\n\nQuestion: "
    )

    async def _rewrite(self, query: str) -> str:
        from cadence_backend.llm import llm

        try:
            completion = await llm().chat.completions.create(
                model=self.model,
                max_tokens=64,
                messages=[{"role": "user", "content": self.PROMPT + query}],
            )
            self.calls += 1
            return (completion.choices[0].message.content or query).strip() or query
        except Exception:
            logger.warning("rewrite failed; falling back to the raw query", exc_info=True)
            return query

    async def search(self, query: str, k: int) -> list[Hit]:
        return await self.inner.search(await self._rewrite(query), k)


@dataclass
class MultiQuery:
    """Fan a question into several paraphrases and fuse the results.

    Aimed at recall rather than precision: three phrasings of the same question
    reach chunks that any one phrasing misses. Costs one LLM call plus N
    searches per query.
    """

    inner: Retriever
    n: int = 3
    model: str = "anthropic/claude-haiku-4.5"
    name: str = field(init=False)
    calls: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.name = f"{self.inner.name} + multi-query"

    async def search(self, query: str, k: int) -> list[Hit]:
        from cadence_backend.llm import llm

        variants = [query]
        try:
            completion = await llm().chat.completions.create(
                model=self.model,
                max_tokens=160,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Write {self.n} different search queries for this question, one per "
                            f"line, no numbering. Vary the wording but keep entities and dates.\n\n"
                            f"{query}"
                        ),
                    }
                ],
            )
            self.calls += 1
            text = completion.choices[0].message.content or ""
            variants += [ln.strip() for ln in text.splitlines() if ln.strip()][: self.n]
        except Exception:
            logger.warning("expansion failed; using the raw query alone", exc_info=True)

        results = await asyncio.gather(*(self.inner.search(v, k * 3) for v in variants))
        fused: dict[tuple[int, int], float] = {}
        chunks: dict[tuple[int, int], Chunk] = {}
        for hits in results:
            for rank, hit in enumerate(hits):
                key = (hit.chunk.doc_id, hit.chunk.ordinal)
                fused[key] = fused.get(key, 0.0) + 1.0 / (60 + rank + 1)
                chunks[key] = hit.chunk
        top = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:k]
        return [Hit(chunks[key], score) for key, score in top]


# --- reranking --------------------------------------------------------------


@dataclass
class LLMRerank:
    """Re-score the top candidates with a cross-encoder-style LLM pass.

    A true cross-encoder reads query and passage together, which is what a
    bi-encoder cannot do — it should help precision at the very top and cost
    latency in proportion to the candidate count. Fetches `depth` candidates
    and returns the best `k`.
    """

    inner: Retriever
    depth: int = 20
    model: str = "anthropic/claude-haiku-4.5"
    name: str = field(init=False)
    calls: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.name = f"{self.inner.name} + rerank@{self.depth}"

    async def search(self, query: str, k: int) -> list[Hit]:
        from cadence_backend.llm import llm

        candidates = await self.inner.search(query, self.depth)
        if len(candidates) <= 1:
            return candidates[:k]

        # The whole passage, not a preview. Truncating to 600 characters showed
        # the reranker under a third of a 512-token chunk, so a planted fact
        # past that point was invisible and the arm scored as though reranking
        # hurts. A cross-encoder that cannot see the passage is not a
        # cross-encoder.
        listing = "\n\n".join(
            f"[{i}] {h.chunk.text[:PASSAGE_CHARS]}" for i, h in enumerate(candidates)
        )
        try:
            completion = await llm().chat.completions.create(
                model=self.model,
                max_tokens=120,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Question: {query}\n\nPassages:\n{listing}\n\n"
                            f"Reply with the {k} passage numbers that best answer the question, "
                            f"most relevant first, comma-separated. Numbers only."
                        ),
                    }
                ],
            )
            self.calls += 1
            order = [
                int(x) for x in re.findall(r"\d+", completion.choices[0].message.content or "")
            ]
        except Exception:
            logger.warning("rerank failed; keeping the original order", exc_info=True)
            return candidates[:k]

        seen, out = set(), []
        for idx in order:
            if 0 <= idx < len(candidates) and idx not in seen:
                seen.add(idx)
                # Descending synthetic score: the LLM gives an order, not a scale.
                out.append(Hit(candidates[idx].chunk, 1.0 - len(out) / max(k, 1)))
        # Backfill from the original ranking if the model returned too few.
        for i, hit in enumerate(candidates):
            if len(out) >= k:
                break
            if i not in seen:
                out.append(hit)
        return out[:k]


# --- timing -----------------------------------------------------------------


async def timed(retriever: Retriever, query: str, k: int) -> tuple[list[Hit], float]:
    start = time.perf_counter()
    hits = await retriever.search(query, k)
    return hits, (time.perf_counter() - start) * 1000


async def build_vectors(corpus: Corpus, embedder: Embedder) -> Corpus:
    corpus.vectors = await embed_all(embedder, corpus.texts)
    return corpus
