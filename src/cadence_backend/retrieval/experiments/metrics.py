"""Ranking metrics, scored by span overlap rather than chunk identity.

A retrieved chunk counts as relevant when its character span overlaps a planted
fact's span in the same document. That indirection is what lets the chunking
sweep be fair: every strategy produces different chunk ids for the same text,
so an id-based ground truth would silently favour whichever strategy the ids
were recorded under.
"""

import math
from dataclasses import dataclass

from cadence_backend.retrieval.retrievers import Hit


@dataclass(frozen=True)
class Relevant:
    doc_id: int
    span_start: int
    span_end: int
    grade: int


def graded(hits: list[Hit], truth: list[Relevant]) -> list[int]:
    """Gain per rank position: 2 for the primary passage, 1 corroborating, 0 miss.

    A hit can overlap more than one planted span; the highest grade wins, which
    matches how a reader would judge it.
    """
    out = []
    for hit in hits:
        best = 0
        for rel in truth:
            if hit.chunk.doc_id == rel.doc_id and hit.chunk.overlaps(rel.span_start, rel.span_end):
                best = max(best, rel.grade)
        out.append(best)
    return out


def recall_at_k(gains: list[int], truth: list[Relevant], k: int) -> float:
    """Share of planted passages found in the top k.

    Counted over distinct relevant *passages*, not hits: two chunks overlapping
    the same fact are one find, not two.
    """
    if not truth:
        return 0.0
    return min(sum(1 for g in gains[:k] if g > 0), len(truth)) / len(truth)


def precision_at_k(gains: list[int], k: int) -> float:
    if k == 0:
        return 0.0
    return sum(1 for g in gains[:k] if g > 0) / k


def mrr(gains: list[int]) -> float:
    for i, g in enumerate(gains):
        if g > 0:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(gains: list[int], truth: list[Relevant], k: int) -> float:
    def dcg(values: list[int]) -> float:
        return sum(v / math.log2(i + 2) for i, v in enumerate(values))

    ideal = sorted((r.grade for r in truth), reverse=True)[:k]
    best = dcg(ideal)
    return dcg(gains[:k]) / best if best else 0.0


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(p / 100 * len(ordered)), len(ordered) - 1)
    return ordered[idx]
