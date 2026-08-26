"""Compare retrieval strategies on the planted-fact ground truth.

    make bench                    # every arm that is configured
    make bench ARGS="--arms bm25,term-overlap"

Runs offline, against the retriever directly, never through the analyst. The
system prompt tells the model to search only "when a result looks surprising",
so an end-to-end comparison would mostly measure whether the model chose to
search — at ninety seconds and real spend per question. The winner gets an
end-to-end confirmation; the sweep does not.

Arms that need a key that is not configured are reported as skipped, with the
reason, rather than silently omitted.
"""

import argparse
import asyncio
import gzip
import json
import pathlib
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field

import asyncpg

from cadence_backend.core.config import get_settings
from cadence_backend.retrieval import embeddings as emb
from cadence_backend.retrieval.chunking import STRATEGIES, Chunk
from cadence_backend.retrieval.metrics import (
    Relevant,
    graded,
    mrr,
    ndcg_at_k,
    percentile,
    precision_at_k,
    recall_at_k,
)
from cadence_backend.retrieval.retrievers import (
    BM25,
    Corpus,
    Dense,
    HybridRRF,
    LLMRerank,
    MultiQuery,
    QueryRewrite,
    Retriever,
    TermOverlap,
    timed,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS = ROOT / "data" / "bench_results.json"

K = 10
DEFAULT_CHUNKING = "fixed-512"


@dataclass
class Result:
    arm: str
    chunking: str
    queries: int
    recall_1: float = 0.0
    recall_5: float = 0.0
    recall_10: float = 0.0
    mrr_10: float = 0.0
    ndcg_10: float = 0.0
    precision_5_multi: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    usd_per_1k: float = 0.0
    skipped: str = ""
    notes: list[str] = field(default_factory=list)


DOCUMENTS = ROOT / "data" / "corpus" / "documents.jsonl.gz"


def read_documents() -> list[tuple[int, str]]:
    """Document bodies, from the local file the generator wrote.

    Not from Supabase. The corpus is 171MB of text and the sweep re-chunks all
    of it once per strategy — pulling that through the pooler on every run
    exceeds the statement timeout, and would be wasteful even if it did not.
    Supabase stays the source of truth for the ground truth tables, which are
    small.
    """
    if not DOCUMENTS.exists():
        sys.exit(f"{DOCUMENTS} is missing. Run scripts/generate_corpus.py first.")
    out = []
    with gzip.open(DOCUMENTS, "rt") as fh:
        for line in fh:
            doc = json.loads(line)
            out.append((doc["doc_id"], doc["body"]))
    return out


def load_corpus(documents: list[tuple[int, str]], strategy: str) -> Corpus:
    """Chunk every document under one strategy, in memory."""
    split = STRATEGIES[strategy]
    chunks: list[Chunk] = []
    for doc_id, body in documents:
        chunks.extend(split(body, doc_id))
    return Corpus(chunks=chunks, strategy=strategy)


async def load_truth(con: asyncpg.Connection) -> tuple[list[dict], dict[int, list[Relevant]]]:
    queries = [
        dict(r)
        for r in await con.fetch(
            "SELECT query_id, question, sql_source, multi_relevant "
            "FROM corpus.eval_query ORDER BY query_id"
        )
    ]
    truth: dict[int, list[Relevant]] = {}
    for r in await con.fetch(
        "SELECT query_id, doc_id, span_start, span_end, grade FROM corpus.eval_relevance"
    ):
        truth.setdefault(r["query_id"], []).append(
            Relevant(r["doc_id"], r["span_start"], r["span_end"], r["grade"])
        )
    return queries, truth


async def evaluate(
    retriever: Retriever,
    queries: list[dict],
    truth: dict[int, list[Relevant]],
    chunking: str,
    usd_per_1k: float = 0.0,
) -> Result:
    res = Result(arm=retriever.name, chunking=chunking, queries=len(queries), usd_per_1k=usd_per_1k)
    r1, r5, r10, mrrs, ndcgs, p5, latencies = [], [], [], [], [], [], []

    for q in queries:
        want = truth.get(q["query_id"], [])
        hits, ms = await timed(retriever, q["question"], K)
        latencies.append(ms)
        gains = graded(hits, want)
        r1.append(recall_at_k(gains, want, 1))
        r5.append(recall_at_k(gains, want, 5))
        r10.append(recall_at_k(gains, want, 10))
        mrrs.append(mrr(gains))
        ndcgs.append(ndcg_at_k(gains, want, 10))
        # Precision only where several passages are genuinely relevant. With a
        # single planted passage, precision@5 cannot exceed 0.2 by construction
        # and comparing arms on it would be comparing noise.
        if q["multi_relevant"]:
            p5.append(precision_at_k(gains, 5))

    res.recall_1 = statistics.mean(r1)
    res.recall_5 = statistics.mean(r5)
    res.recall_10 = statistics.mean(r10)
    res.mrr_10 = statistics.mean(mrrs)
    res.ndcg_10 = statistics.mean(ndcgs)
    res.precision_5_multi = statistics.mean(p5) if p5 else 0.0
    res.p50_ms = percentile(latencies, 50)
    res.p95_ms = percentile(latencies, 95)
    return res


def table(results: list[Result]) -> str:
    head = (
        f"{'arm':38} {'chunking':18} {'R@1':>6} {'R@5':>6} {'R@10':>6} "
        f"{'MRR':>6} {'nDCG':>6} {'P@5*':>6} {'p50ms':>8} {'p95ms':>8} {'$/1k':>7}"
    )
    lines = [head, "-" * len(head)]
    for r in results:
        if r.skipped:
            lines.append(f"{r.arm:38} {r.chunking:18} SKIPPED — {r.skipped[:60]}")
            continue
        lines.append(
            f"{r.arm:38} {r.chunking:18} {r.recall_1:>6.3f} {r.recall_5:>6.3f} "
            f"{r.recall_10:>6.3f} {r.mrr_10:>6.3f} {r.ndcg_10:>6.3f} "
            f"{r.precision_5_multi:>6.3f} {r.p50_ms:>8.1f} {r.p95_ms:>8.1f} {r.usd_per_1k:>7.3f}"
        )
    lines.append("")
    lines.append("* P@5 is over the 40 multi-relevant queries only; with one planted")
    lines.append("  passage, precision@5 is capped at 0.2 and compares nothing.")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> None:
    settings = get_settings()
    con = await asyncpg.connect(settings.require_database_url(), statement_cache_size=0)
    try:
        queries, truth = await load_truth(con)
        if not queries:
            sys.exit("No eval queries. Run scripts/generate_corpus.py --load first.")
        print(f"{len(queries)} queries, {sum(len(v) for v in truth.values())} relevant passages\n")

        results: list[Result] = []
        wanted = set(args.arms.split(",")) if args.arms else None

        def include(name: str) -> bool:
            return wanted is None or name in wanted

        # --- lexical arms, on the default chunking ------------------------
        started = time.perf_counter()
        documents = read_documents()
        corpus = load_corpus(documents, DEFAULT_CHUNKING)
        print(
            f"chunked {len(documents):,} documents into {len(corpus.chunks):,} "
            f"chunks ({DEFAULT_CHUNKING}) in {time.perf_counter() - started:.1f}s"
        )

        if include("term-overlap"):
            results.append(await evaluate(TermOverlap(corpus), queries, truth, DEFAULT_CHUNKING))
        if include("bm25"):
            bm = BM25(corpus)
            results.append(await evaluate(bm, queries, truth, DEFAULT_CHUNKING))

        # --- arms that only need the chat model ---------------------------
        # Rewriting, expansion and reranking call the analyst's OpenRouter
        # client, not an embeddings endpoint. Gating them behind an embeddings
        # key would have left most of the study unrunnable for no reason. They
        # wrap the best available base retriever, which is BM25 without a key.
        base: Retriever = bm if include("bm25") else BM25(corpus)

        if include("chunking"):
            for strategy in STRATEGIES:
                if strategy == DEFAULT_CHUNKING:
                    continue
                alt = load_corpus(documents, strategy)
                results.append(await evaluate(BM25(alt), queries, truth, strategy))

        if include("rewrite"):
            results.append(await evaluate(QueryRewrite(base), queries, truth, DEFAULT_CHUNKING))
        if include("multi-query"):
            results.append(await evaluate(MultiQuery(base), queries, truth, DEFAULT_CHUNKING))
        if include("rerank"):
            results.append(await evaluate(LLMRerank(base), queries, truth, DEFAULT_CHUNKING))

        # --- arms that genuinely need embeddings --------------------------
        ok, why = emb.available()
        if not ok:
            for arm in ("dense", "hybrid", "dimensions"):
                if include(arm):
                    results.append(Result(arm=arm, chunking="—", queries=len(queries), skipped=why))
        else:
            embedder = emb.ApiEmbedder(dimensions=args.dimensions)
            print(f"embedding {len(corpus.chunks):,} chunks ...")
            corpus.vectors = await emb.embed_all(embedder, corpus.texts)
            dense = Dense(corpus, embedder)
            per_1k = embedder.cost_usd / max(len(queries), 1) * 1000

            if include("dense"):
                results.append(await evaluate(dense, queries, truth, DEFAULT_CHUNKING, per_1k))
            if include("hybrid"):
                hybrid = HybridRRF([BM25(corpus), dense])
                results.append(await evaluate(hybrid, queries, truth, DEFAULT_CHUNKING, per_1k))
                if include("rerank"):
                    results.append(
                        await evaluate(LLMRerank(hybrid), queries, truth, DEFAULT_CHUNKING, per_1k)
                    )
            if include("dimensions"):
                for dims in (512, 1536):
                    alt_embedder = emb.ApiEmbedder(dimensions=dims)
                    alt = load_corpus(documents, DEFAULT_CHUNKING)
                    alt.vectors = await emb.embed_all(alt_embedder, alt.texts)
                    results.append(
                        await evaluate(
                            Dense(alt, alt_embedder),
                            queries,
                            truth,
                            DEFAULT_CHUNKING,
                            alt_embedder.cost_usd / max(len(queries), 1) * 1000,
                        )
                    )

        print(table(results))
        RESULTS.write_text(json.dumps([asdict(r) for r in results], indent=1))
        print(f"\nwrote {RESULTS.relative_to(ROOT)}")
    finally:
        await con.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--arms", help="Comma-separated subset, e.g. bm25,dense")
    p.add_argument("--dimensions", type=int, default=emb.DEFAULT_DIMENSIONS)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
