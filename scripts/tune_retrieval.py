"""Compare retrieval arms offline, without the analyst in the loop.

    uv run python scripts/tune_retrieval.py --arms bm25
    uv run python scripts/tune_retrieval.py --arms bm25,expand,hybrid

End-to-end runs conflate two things: whether the retriever can find the planted
passage, and whether the model happened to word its query well. This measures
only the first. It loads the cached index (a tenth of a second) so an arm can be
changed and re-scored in seconds.

Ground truth comes from the planted spans. Several eggs share a question with
different planted locations — the same fact is corroborated in more than one
document — so gold spans are unioned per question. Scoring each row separately
would count a correct retrieval of one copy as a miss on the others.
"""

import argparse
import collections
import json
import pathlib
import re
import statistics
import time

from cadence_backend.retrieval import index_cache

ROOT = pathlib.Path(__file__).resolve().parent.parent
EGGS = ROOT / "data" / "corpus" / "easter_eggs.json"
REWRITES = ROOT / "data" / "corpus" / "rewrites.json"
DEPTH = 10


def gold_by_question() -> dict[str, set[tuple[int, int, int]]]:
    eggs = json.loads(EGGS.read_text())
    out: dict[str, set[tuple[int, int, int]]] = collections.defaultdict(set)
    for e in eggs:
        for s in e["spans"]:
            out[e["question"]].add((int(s[0]), int(s[1]), int(s[2])))
    return dict(out)


def first_hit(ranked, gold: set[tuple[int, int, int]]) -> int | None:
    """1-based rank of the first chunk overlapping any gold span."""
    for i, r in enumerate(ranked, 1):
        for doc, start, end in gold:
            if r.doc_id == doc and r.span_start < end and r.span_end > start:
                return i
    return None


# --------------------------------------------------------------------------
# Arms. Each takes (index, question, k) and returns a ranked list.
# --------------------------------------------------------------------------
def arm_bm25(index, question, k):
    return index.rank(question, k)


def rrf(runs: list[list], k: int, kappa: int = 60) -> list:
    """Reciprocal-rank fusion: rank position only, so incomparable scores from
    different retrievers can be combined without calibrating them."""
    score: dict[int, float] = collections.defaultdict(float)
    seen: dict[int, object] = {}
    for run in runs:
        for i, r in enumerate(run, 1):
            score[r.chunk_id] += 1.0 / (kappa + i)
            seen.setdefault(r.chunk_id, r)
    best = sorted(score, key=lambda c: -score[c])[:k]
    return [seen[c] for c in best]


_STOP = {
    "the",
    "a",
    "an",
    "our",
    "we",
    "for",
    "on",
    "and",
    "or",
    "is",
    "are",
    "do",
    "does",
    "don",
    "t",
    "why",
    "what",
    "which",
    "should",
    "i",
    "in",
    "of",
    "that",
}


def arm_expand_static(index, question, k):
    """Cheap deterministic expansion: the question plus term-dropped variants.

    No model involved — this is the floor for what expansion can buy, and it
    isolates the fusion mechanism from the quality of generated paraphrases.
    """
    terms = [t for t in re.findall(r"[A-Za-z0-9]+", question.lower()) if t not in _STOP]
    variants = [question, " ".join(terms)]
    if len(terms) > 3:
        variants.append(" ".join(terms[:3]))
        variants.append(" ".join(terms[-3:]))
    return rrf([index.rank(v, k * 3) for v in variants], k)


def _rewrites(model: str) -> dict:
    if not REWRITES.exists():
        raise SystemExit("no rewrites — run scripts/gen_rewrites.py first")
    data = json.loads(REWRITES.read_text())
    out = {}
    for q, by_model in data.items():
        if model in by_model:
            out[q] = by_model[model]
    if not out:
        raise SystemExit(f"no rewrites cached for model {model}")
    return out


def make_rewrite_arm(model: str, use: str):
    """Build an arm that fuses the cached rewrites for one model.

    `use` selects which generated forms enter the fusion, so paraphrasing and
    HyDE can be credited separately rather than as one undifferentiated win.
    """
    cache = _rewrites(model)

    def arm(index, question, k):
        r = cache.get(question, {})
        queries = [question]
        if use in ("para", "all"):
            queries += r.get("paraphrases", [])
        if use in ("hyde", "all"):
            if r.get("hyde"):
                queries.append(r["hyde"])
        queries = [q for q in queries if q.strip()]
        if len(queries) == 1:
            return index.rank(queries[0], k)
        return rrf([index.rank(q, k * 3) for q in queries], k)

    return arm


ARMS = {"bm25": arm_bm25, "expand-static": arm_expand_static}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--arms", default="bm25")
    p.add_argument("--depth", type=int, default=DEPTH)
    args = p.parse_args()

    index = index_cache.load()
    if index is None:
        raise SystemExit("no cached index — run scripts/cache_index.py first")
    gold = gold_by_question()
    print(
        f"index {len(index.chunk_ids):,} chunks · {len(gold)} distinct questions "
        f"({sum(len(v) for v in gold.values())} planted spans)\n"
    )

    rows = []
    for name in args.arms.split(","):
        name = name.strip()
        if ":" in name:  # e.g. rewrite:para:anthropic/claude-haiku-4.5
            _, use, model = name.split(":", 2)
            fn = make_rewrite_arm(model, use)
        else:
            fn = ARMS[name]
        ranks, times = [], []
        for q, spans in gold.items():
            t = time.perf_counter()
            ranked = fn(index, q, args.depth)
            times.append((time.perf_counter() - t) * 1000)
            ranks.append(first_hit(ranked, spans))
        n = len(ranks)
        row = {
            "arm": name.strip(),
            "mrr": sum(1 / r for r in ranks if r) / n,
            "p50_ms": statistics.median(times),
        }
        for cut in (1, 5, 10, 50, 100, 500, 1000):
            if cut <= args.depth:
                row[f"recall_{cut}"] = sum(1 for r in ranks if r and r <= cut) / n
        rows.append(row)

    cuts = [c for c in (1, 5, 10, 50, 100, 500, 1000) if f"recall_{c}" in rows[0]]
    head = "".join(f"{'R@' + str(c):>8}" for c in cuts)
    print(f"{'arm':<16}{head}{'MRR':>8}{'p50ms':>9}")
    for r in rows:
        cells = "".join(f"{r['recall_' + str(c)]:>8.3f}" for c in cuts)
        print(f"{r['arm']:<16}{cells}{r['mrr']:>8.3f}{r['p50_ms']:>9.1f}")
    (ROOT / "data" / "tune_results.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
