"""Generate the document corpus and its evaluation ground truth.

    uv run python scripts/generate_corpus.py --dry-run    # counts and size first
    uv run python scripts/generate_corpus.py --load       # COPY into Supabase

Follows scripts/generate_internal_seed.py: a fixed PRNG seed, and every figure
quoted in a document derived from data/seed_data.sql. The corpus and the
warehouse have to tell one consistent story, because the easter eggs are facts
that can only be answered by joining the two — a document that contradicts the
ledger would make the planted question unanswerable rather than hard.

Lexical diversity is not decoration here. A corpus built from one template makes
keyword search win trivially and the benchmark measures nothing, so there are
seven document types, each with several structures and phrase banks that vary
hedging, ordering and vocabulary independently of the numbers.
"""

import argparse
import asyncio
import gzip
import json
import pathlib
import random
import re
import sys
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "seed_data.sql"
OUT = ROOT / "data" / "corpus"

RNG = random.Random(20260826)

# Sized to the Supabase free tier. Text plus 256-dim vectors plus an HNSW index
# lands near 380MB against a 500MB ceiling; see data/corpus_schema.sql.
DEFAULT_DOCS = 20_000

SERVICES = {
    1: "Lumora+",
    2: "Northgate Stream",
    3: "Kestrel",
    4: "Solstice TV",
    5: "Tidepool",
    6: "Harborlight",
    7: "Grovehouse",
    8: "Kinoloft",
    9: "Meridian Play",
    10: "Cinder",
}
BELLWEATHER = {5, 6}

GENRES = ["Anime", "Kids & Family", "Drama", "Documentary", "Reality & Lifestyle", "Sport"]

PUBLISHERS = {
    "research_note": [
        "Meridian Panel Analytics",
        "Cobalt Research Group",
        "Streamscope Telemetry Co-op",
    ],
    "earnings_call": ["Bellweather Media", "Lumora Holdings", "Northgate Group"],
    "press_release": ["Bellweather Media", "Kestrel Media", "Cinder Entertainment"],
    "methodology": ["Meridian Panel Analytics", "Cobalt Research Group"],
    "kb_article": ["Bellweather Support"],
    "internal_memo": ["Bellweather Media"],
    "trade_press": ["Streaming Ledger", "The Carriage Report", "Panel Weekly"],
}

PREFIX = {
    "research_note": "MPA",
    "earnings_call": "EC",
    "press_release": "PR",
    "methodology": "MTH",
    "kb_article": "KB",
    "internal_memo": "MEMO",
    "trade_press": "TP",
}

# --- phrase banks -----------------------------------------------------------
# Each slot varies independently, so two documents quoting the same figure
# rarely share surface form. This is what stops lexical overlap from being a
# free win for the keyword baseline.

HEDGE = [
    "on the panel's current read",
    "subject to the usual revision window",
    "before any restatement",
    "on unrounded figures",
    "as reported",
    "using the provider's published basis",
    "prior to seasonal adjustment",
]
MOVE_UP = ["climbed", "advanced", "improved to", "rose to", "strengthened to", "ticked up to"]
MOVE_DOWN = ["slipped to", "declined to", "softened to", "fell to", "eased to", "retreated to"]
ATTRIB = [
    "which the panel attributes to",
    "largely a function of",
    "driven principally by",
    "consistent with",
    "we read this as",
    "the provider links this to",
]
CAUSES = [
    "a repricing of the standard tier",
    "a franchise release landing mid-quarter",
    "a change in promotional mix",
    "a catalogue rotation",
    "a shift in acquisition spend",
    "the entrant's launch window",
    "a measurement threshold change",
]
CAVEAT = [
    "Figures are not comparable across the July 2024 coverage break.",
    "Cohort retention tracks joiners, not the installed base.",
    "Genre attribution is single-assignment; totals do not sum to subscribers.",
    "Bundled disclosures cannot be decomposed to service level.",
    "Revenue is reported annually; part-year figures are not annualised.",
]


@dataclass
class Doc:
    doc_id: int
    doc_type: str
    title: str
    publisher: str
    reference: str
    published_on: str
    body: str


@dataclass
class Egg:
    egg_id: int
    question: str
    sql_source: str
    fact: str
    # (doc_id, span_start, span_end, grade)
    spans: list[tuple[int, int, int, int]] = field(default_factory=list)


# --- read what the warehouse already asserts --------------------------------


def load_market() -> dict:
    seed = SEED.read_text()

    def rows(table: str) -> list[str]:
        m = re.search(rf"INSERT INTO {table} \([^)]*\) VALUES(.*?);", seed, re.S)
        if m is None:
            sys.exit(f"could not find INSERT INTO {table} in {SEED}")
        return [ln.strip().rstrip(",") for ln in m.group(1).strip().splitlines() if ln.strip()]

    subs: dict[tuple[int, str], dict] = {}
    for line in rows("monthly_subscribers"):
        m = re.match(r"\((\d+),\s*'([\d-]+)',\s*(\d+),\s*(\d+),\s*(\d+),", line)
        if m:
            subs[(int(m.group(1)), m.group(2))] = {
                "eom": int(m.group(3)),
                "gross_adds": int(m.group(4)),
                "cancellations": int(m.group(5)),
            }

    events = []
    for line in rows("market_events"):
        m = re.match(r"\((\d+),\s*'([\d-]+)',\s*'([^']*)',\s*(\d+|NULL),\s*'((?:[^']|'')*)'", line)
        if m:
            events.append(
                {
                    "event_id": int(m.group(1)),
                    "date": m.group(2),
                    "type": m.group(3),
                    "service_id": None if m.group(4) == "NULL" else int(m.group(4)),
                    "headline": m.group(5).replace("''", "'"),
                }
            )

    months = sorted({month for _, month in subs})
    if len(months) != 30:
        sys.exit(f"expected 30 months in the market seed, found {len(months)}")
    return {"subs": subs, "events": events, "months": months}


# --- document builders ------------------------------------------------------


def _fig(n: int) -> str:
    return f"{n:,}"


def _direction(net: int, base: int) -> str:
    """Phrasing that follows the arithmetic.

    Choosing this at random reads fine sentence by sentence and is nonsense in
    aggregate — "net movement of -37,000 leaves the service ahead of" is the
    kind of contradiction that makes a generated corpus obviously generated.
    """
    share = net / max(base, 1)
    if share > 0.004:
        return "ahead of"
    if share < -0.004:
        return "behind"
    return "broadly level with"


def _para_metrics(market: dict, sid: int, month: str) -> str:
    """A paragraph of real numbers for one service-month.

    Three or four sentences rather than one: a corpus of terse, uniform
    paragraphs gives the chunking sweep nothing to separate, because every
    chunk boundary falls in the same place regardless of strategy.
    """
    row = market["subs"].get((sid, month))
    if row is None:
        return ""
    name = SERVICES[sid]
    base = max(row["eom"] + row["cancellations"] - row["gross_adds"], 1)
    churn = row["cancellations"] / base * 100
    net = row["gross_adds"] - row["cancellations"]
    verb = RNG.choice(MOVE_UP if net > 0 else MOVE_DOWN)
    genre = RNG.choice(GENRES)

    parts = [
        f"{name} {verb} {_fig(row['eom'])} subscribers at the end of {month[:7]}, "
        f"on {_fig(row['gross_adds'])} gross additions against {_fig(row['cancellations'])} "
        f"cancellations — a monthly churn of {churn:.2f}% {RNG.choice(HEDGE)}.",
        f"Net movement of {net:+,} against an opening base of {_fig(base)} leaves the "
        f"service {_direction(net, base)} the trajectory implied by the prior quarter.",
        f"{RNG.choice(ATTRIB).capitalize()} {RNG.choice(CAUSES)}.",
    ]
    if RNG.random() < 0.6:
        parts.append(
            f"Within {genre}, the same period shows "
            f"{RNG.choice(['a broadly stable', 'a softening', 'a strengthening'])} share of "
            f"category hours, though attribution is single-assignment and should not be "
            f"summed against the subscriber count above."
        )
    if RNG.random() < 0.35:
        parts.append(RNG.choice(CAVEAT))
    return " ".join(parts) + " "


def build_body(doc_type: str, market: dict, sid: int, months: list[str], paras: int) -> str:
    """Assemble a document body from real figures and varied prose."""
    name = SERVICES[sid]
    out: list[str] = []

    if doc_type == "earnings_call":
        out.append(
            f"Operator: Thank you for standing by, and welcome to the {name} results "
            f"call. I now hand over to the Chief Financial Officer.\n\n"
        )
        out.append(
            "CFO: Thank you. Before we begin, a reminder that these remarks contain "
            "forward-looking statements. Turning to the quarter.\n\n"
        )
    elif doc_type == "press_release":
        out.append(
            f"FOR IMMEDIATE RELEASE — {name} today reported operating metrics for the period.\n\n"
        )
    elif doc_type == "kb_article":
        out.append(f"Applies to: {name} subscribers on the standard tier.\n\n")
    elif doc_type == "internal_memo":
        out.append("INTERNAL — not for distribution outside Finance and Operations.\n\n")

    for month in months[:paras]:
        block = _para_metrics(market, sid, month)
        if block:
            out.append(block + "\n\n")

    if doc_type in ("methodology", "research_note"):
        out.append(RNG.choice(CAVEAT) + " " + RNG.choice(CAVEAT) + "\n\n")
    if doc_type == "earnings_call":
        out.append(
            "Analyst: Thank you for taking the question. Could you say more about the "
            "trajectory into the second half?\n\n"
            "CFO: We are not guiding beyond the current period, but the direction of travel "
            "is consistent with what we have described.\n\n"
        )
    return "".join(out)


def generate(n_docs: int, market: dict) -> tuple[list[Doc], list[Egg]]:
    docs: list[Doc] = []
    months = market["months"]
    types = list(PREFIX)

    i = -1
    while len(docs) < n_docs:
        i += 1
        doc_type = types[i % len(types)]
        sid = RNG.choice(list(SERVICES))
        # Long transcripts, short releases — the length spread matters for the
        # chunking sweep, which is meaningless on a corpus of uniform documents.
        # The spread is deliberate. A transcript runs long, a release runs
        # short, and the chunking sweep only means something when documents
        # differ enough that chunk boundaries land differently.
        paras = {
            "earnings_call": 30,
            "internal_memo": 24,
            "research_note": 20,
            "methodology": 16,
            "trade_press": 12,
            "kb_article": 9,
            "press_release": 6,
        }[doc_type]
        # Only months this service actually reports. Cinder launches in Feb
        # 2025, so a naive window produced near-empty documents for the
        # entrant — 54 bytes in the earlier run.
        available = [m for m in months if (sid, m) in market["subs"]]
        if len(available) < 3:
            continue
        start = RNG.randrange(0, len(available))
        # Wrap: paragraph counts exceed the window for the longer document
        # types, and a transcript revisiting earlier months reads naturally.
        window = [available[(start + k) % len(available)] for k in range(paras)]
        body = build_body(doc_type, market, sid, window, paras)
        # The latest month discussed, not the last one emitted — the window
        # wraps, so those differ and a title dated before its own contents
        # reads as broken.
        published = max(window)
        docs.append(
            Doc(
                doc_id=i + 1,
                doc_type=doc_type,
                title=f"{SERVICES[sid]} — {doc_type.replace('_', ' ')}, {published[:7]}",
                publisher=RNG.choice(PUBLISHERS[doc_type]),
                reference=f"{PREFIX[doc_type]}-{published[:4]}-{i + 1:06d}",
                published_on=published,
                body=body,
            )
        )

    eggs = plant_eggs(docs, market)
    return docs, eggs


# --- easter eggs ------------------------------------------------------------

EGG_TEMPLATES = [
    # (sql_source, fact, question)
    #
    # The question must NOT share surface form with the fact. An earlier draft
    # asked "why did X's Q3 billings diverge?" against a fact containing "Q3
    # billing divergence", and substring matching turned the ground truth into
    # a giveaway: the term-overlap baseline scored R@10 0.74 and beat BM25,
    # which is backwards. Paraphrasing is what makes the comparison mean
    # anything — and it is what a real question looks like anyway.
    (
        "FINANCE",
        "The {q} FY{yr} billing divergence for {svc} traces to a one-off catalogue "
        "licensing true-up posted to account 5000; no subscriber movement explains it.",
        "Why don't our books and the provider agree on {svc} for {q} FY{yr}?",
    ),
    (
        "MARKET",
        "{svc}'s {mon} cancellation spike was a deliberate migration of legacy annual "
        "plans, not organic churn; the panel counts them as cancellations regardless.",
        "Did {svc} really lose that many customers around {mon}, or is something else going on?",
    ),
    (
        "SUPPORT",
        "The {mon} escalation surge for {svc} was driven by a single payment-provider "
        "outage lasting under six hours, which is why resolution time barely moved.",
        "What happened at {svc} in {mon} that support felt but the service metrics never showed?",
    ),
    (
        "multi",
        "{svc}'s {q} FY{yr} advertising revenue was restated after a measurement change; "
        "the ledger carries the restated figure while the panel carries the original.",
        "Which {svc} ad figure should I trust for {q} FY{yr}?",
    ),
]


def plant_eggs(docs: list[Doc], market: dict, count: int = 120) -> list[Egg]:
    """Insert planted facts and record where they landed.

    The span is recorded against the document, never a chunk, so re-chunking
    for the sweep cannot invalidate the ground truth.
    """
    eggs: list[Egg] = []
    quarters = ["Q1", "Q2", "Q3", "Q4"]
    # A third get corroborating passages, which is the only subset where
    # precision and nDCG carry information.
    multi_ids = set(RNG.sample(range(1, count + 1), count // 3))

    for egg_id in range(1, count + 1):
        source, fact_t, q_t = EGG_TEMPLATES[egg_id % len(EGG_TEMPLATES)]
        sid = RNG.choice(list(BELLWEATHER if source in ("FINANCE", "SUPPORT") else SERVICES))
        subs = {
            "svc": SERVICES[sid],
            "q": RNG.choice(quarters),
            "yr": RNG.choice([2024, 2025, 2026]),
            "mon": RNG.choice(market["months"])[:7],
        }
        fact = fact_t.format(**subs)
        egg = Egg(egg_id=egg_id, question=q_t.format(**subs), sql_source=source, fact=fact)

        n_homes = RNG.randint(2, 4) if egg_id in multi_ids else 1
        homes = RNG.sample(docs, n_homes)
        for rank, doc in enumerate(homes):
            # Land it at a paragraph boundary so the sentence reads naturally
            # rather than splitting an existing one.
            breaks = [m.start() for m in re.finditer(r"\n\n", doc.body)]
            at = RNG.choice(breaks) + 2 if breaks else len(doc.body)
            doc.body = doc.body[:at] + fact + "\n\n" + doc.body[at:]
            egg.spans.append((doc.doc_id, at, at + len(fact), 2 if rank == 0 else 1))
        eggs.append(egg)

    return eggs


# --- output -----------------------------------------------------------------


def report(docs: list[Doc], eggs: list[Egg], chunk_chars: int = 2000) -> None:
    text_bytes = sum(len(d.body.encode()) for d in docs)
    chunks = sum(max(1, -(-len(d.body) // chunk_chars)) for d in docs)
    vector_bytes = chunks * 256 * 4
    index_bytes = vector_bytes  # HNSW is roughly the vector size again
    total = text_bytes + vector_bytes + index_bytes

    print(f"documents      {len(docs):>10,}")
    print(f"chunks (~{chunk_chars}c) {chunks:>10,}")
    print(
        f"easter eggs    {len(eggs):>10,}   ({sum(len(e.spans) > 1 for e in eggs)} multi-relevant)"
    )
    print(f"relevance rows {sum(len(e.spans) for e in eggs):>10,}")
    print()
    print(f"  text         {text_bytes / 1e6:>8.1f} MB")
    print(f"  vectors      {vector_bytes / 1e6:>8.1f} MB")
    print(f"  hnsw index   {index_bytes / 1e6:>8.1f} MB")
    print(f"  TOTAL        {total / 1e6:>8.1f} MB    (Supabase free tier: 500 MB)")
    if total > 460e6:
        print("\n  OVER BUDGET — reduce --docs")


def write_files(docs: list[Doc], eggs: list[Egg]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT / "documents.jsonl.gz", "wt") as fh:
        for d in docs:
            fh.write(json.dumps(d.__dict__) + "\n")
    (OUT / "easter_eggs.json").write_text(json.dumps([e.__dict__ for e in eggs], indent=1))
    print(f"wrote {OUT / 'documents.jsonl.gz'} and {OUT / 'easter_eggs.json'}")


async def load(docs: list[Doc], eggs: list[Egg]) -> None:
    import asyncpg

    from cadence_backend.core.config import get_settings

    con = await asyncpg.connect(get_settings().require_database_url(), statement_cache_size=0)
    try:
        await con.execute((ROOT / "data" / "corpus_schema.sql").read_text())
        await con.execute(
            "TRUNCATE corpus.eval_relevance, corpus.eval_query, corpus.chunk, corpus.document"
        )
        await con.copy_records_to_table(
            "document",
            schema_name="corpus",
            columns=[
                "doc_id",
                "doc_type",
                "title",
                "publisher",
                "reference",
                "published_on",
                "body",
            ],
            records=[
                (
                    d.doc_id,
                    d.doc_type,
                    d.title,
                    d.publisher,
                    d.reference,
                    __import__("datetime").date.fromisoformat(d.published_on),
                    d.body,
                )
                for d in docs
            ],
        )
        await con.copy_records_to_table(
            "eval_query",
            schema_name="corpus",
            columns=["query_id", "egg_id", "question", "sql_source", "multi_relevant"],
            records=[
                (e.egg_id, e.egg_id, e.question, e.sql_source, len(e.spans) > 1) for e in eggs
            ],
        )
        await con.copy_records_to_table(
            "eval_relevance",
            schema_name="corpus",
            columns=["query_id", "doc_id", "span_start", "span_end", "grade"],
            records=[(e.egg_id, *s) for e in eggs for s in e.spans],
        )
        size = await con.fetchval("select pg_size_pretty(pg_database_size(current_database()))")
        print(f"loaded. database is now {size}")
    finally:
        await con.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--docs", type=int, default=DEFAULT_DOCS)
    p.add_argument("--dry-run", action="store_true", help="Report counts and size; write nothing.")
    p.add_argument("--load", action="store_true", help="COPY into Supabase.")
    args = p.parse_args()

    market = load_market()
    docs, eggs = generate(args.docs, market)
    report(docs, eggs)

    if args.dry_run:
        return
    write_files(docs, eggs)
    if args.load:
        asyncio.run(load(docs, eggs))


if __name__ == "__main__":
    main()
