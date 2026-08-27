"""Retrieval regression tests — offline, free, and fast.

These exist because the ranking regressed once in a way no unit test could see:
the corpus was being ranked on the query the analyst invented rather than the
question the user asked, which cost 4.5x recall and showed up only as "the
answer is a bit vague sometimes". Nothing here calls a model or the network, so
it can run on every change rather than when someone remembers.

The floors are pinned to measured behaviour, deliberately a little below it. A
failure means one of two things happened: retrieval got worse, or it got better
and the floor is now stale. Both are worth a human looking.

Requires the cached index (`uv run python scripts/cache_index.py`), which is
built from the generated corpus and therefore absent on a clean checkout.
"""

import json
import pathlib

import pytest

from cadence_backend.retrieval import index_cache

ROOT = pathlib.Path(__file__).resolve().parent.parent
EGGS = ROOT / "data" / "corpus" / "easter_eggs.json"

#: What the app actually returns; measuring at any other depth measures fiction.
TOP_K = 6


@pytest.fixture(scope="module")
def index():
    idx = index_cache.load()
    if idx is None:
        pytest.skip("no cached index — run scripts/cache_index.py")
    return idx


@pytest.fixture(scope="module")
def eggs():
    if not EGGS.exists():
        pytest.skip("no generated corpus")
    gold: dict[str, set] = {}
    source: dict[str, set] = {}
    for e in json.loads(EGGS.read_text()):
        gold.setdefault(e["question"], set()).update(
            (int(s[0]), int(s[1]), int(s[2])) for s in e["spans"]
        )
        source.setdefault(e["question"], set()).add(e["sql_source"])
    return gold, source


def _found(index, question: str, spans: set, k: int = TOP_K) -> bool:
    return any(
        r.doc_id == d and r.span_start < e and r.span_end > s
        for r in index.rank(question, k)
        for d, s, e in spans
    )


def _recall(index, gold: dict, questions: list[str]) -> float:
    if not questions:
        return 0.0
    return sum(_found(index, q, gold[q]) for q in questions) / len(questions)


def test_index_is_intact(index):
    """A truncated or half-written cache would quietly degrade every result."""
    assert len(index.chunk_ids) > 100_000
    assert len(index.terms) > 1_000
    assert index.avg_len > 0


def test_overall_recall_floor(index, eggs):
    gold, _ = eggs
    assert _recall(index, gold, list(gold)) >= 0.42


def test_precise_period_questions_are_reliable(index, eggs):
    """Questions carrying an exact period label retrieve essentially always.

    'Q1 FY2024' survives tokenisation as two terms occurring in 24 and 40 of
    119,207 chunks. This is the class the product can make promises about.
    """
    gold, source = eggs
    precise = [q for q in gold if source[q] & {"FINANCE", "multi"}]
    assert len(precise) >= 40
    assert _recall(index, gold, precise) >= 0.95


def test_user_question_beats_a_paraphrase_of_it(index, eggs):
    """The regression that motivated this file.

    Ranking on a paraphrase measured 0.102 against 0.459 for the user's own
    words, because paraphrasing spends the selective entity and period tokens
    on synonyms that match everything. If this inverts, someone has reintroduced
    a rewriting step ahead of retrieval.
    """
    gold, source = eggs
    precise = [q for q in gold if source[q] & {"FINANCE", "multi"}]

    def vaguer(q: str) -> str:
        # Strip the period label — the single most selective thing in the query.
        import re

        return re.sub(r"\bQ[1-4]\b|\bFY\d{4}\b", "the period", q)

    direct = _recall(index, gold, precise)
    paraphrased = sum(_found(index, vaguer(q), gold[q]) for q in precise) / len(precise)
    assert direct > paraphrased, (
        f"ranking on the user's question ({direct:.3f}) must beat a paraphrase "
        f"that drops the period ({paraphrased:.3f})"
    )


def test_known_limitation_conversational_questions(index, eggs):
    """Documents a real gap so that closing it is noticed, not so it stays.

    Questions phrased conversationally ('lose that many customers around
    2025-04') share no selective term with the passage that answers them, which
    says 'cancellation spike' and 'migration'. Those two terms occur in 53
    chunks each; neither appears in the question. Lexical retrieval cannot
    bridge that, and on this corpus dense retrieval was measured no better.

    If this starts passing at a higher rate, the gap has been closed and the
    bound below should be raised to lock the improvement in.
    """
    gold, source = eggs
    conversational = [q for q in gold if source[q] == {"MARKET"}]
    assert len(conversational) >= 20
    rate = _recall(index, gold, conversational)
    assert rate <= 0.15, f"this improved to {rate:.3f} — raise the bound"


def test_ranking_is_deterministic(index, eggs):
    """Two identical queries must rank identically, or nothing above is stable."""
    gold, _ = eggs
    q = next(iter(gold))
    assert [r.chunk_id for r in index.rank(q, TOP_K)] == [
        r.chunk_id for r in index.rank(q, TOP_K)
    ]


def test_empty_and_junk_queries_do_not_raise(index):
    """Search degrades to nothing rather than failing the analyst's turn."""
    for junk in ("", "   ", "!!!", "zzzzzzzz nonexistentword"):
        assert index.rank(junk, TOP_K) == [] or isinstance(index.rank(junk, TOP_K), list)
