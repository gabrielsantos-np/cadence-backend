"""Retrieval approaches that were measured and are not used.

Nothing in the running app imports anything here. These exist so the comparison
behind the shipped ranking stays reproducible — `scripts/bench_retrieval.py` is
their only caller — and so that a future attempt at the same ideas starts from
the measurements rather than from scratch.

What was tried, and what it scored on 98 planted questions:

    postgres FTS         recall@5 0.023 at 4,248ms   — nothing selective to narrow on
    dense embeddings     recall@10 0.041             — finds 1 question of 98 BM25 misses
    hybrid BM25 + dense  ceiling 0.469               — nothing to fuse
    cross-encoder rerank  —                          — target absent from the pool
    multi-query rewrite  recall@5 0.408              — dilutes the top five
    HyDE                 recall@5 0.439              — invents plausible wrong specifics

Against the shipped ranking's 0.459. The winner was a deletion, not an addition:
searching on the user's question rather than a paraphrase the model wrote.

Those figures were measured against the planted corpus, whose MARKET and SUPPORT
claims are now known to be false. The ranking between approaches still holds,
since all were scored on the same data; the absolute numbers do not describe
product accuracy.

See docs/retrieval-study.html.
"""
