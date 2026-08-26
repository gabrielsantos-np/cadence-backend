"""Build the chunk-level full-text index the analyst searches.

    uv run python scripts/build_chunk_index.py --load

Document-level FTS was measured and lost badly — recall@5 of 0.023 against
in-memory BM25's 0.419 — because `ts_rank_cd` scores whole documents and these
run to 15KB, so a one-sentence fact is a rounding error inside one. The engine
was never the problem; the granularity was. This indexes chunks instead.

Storage is the binding constraint, so chunks store a tsvector and their offsets
but **not** their text. The text is sliced out of `corpus.document.body` for the
handful of rows that actually get returned, which costs one substring per hit
and saves roughly 200MB — the difference between fitting the free tier and not.

The document-level `fts` column is dropped as part of this. It is 144MB of a
213MB table and it is now known not to work.
"""

import argparse
import asyncio
import gzip
import json
import pathlib
import sys
import time

import asyncpg

from cadence_backend.core.config import get_settings
from cadence_backend.retrieval.chunking import STRATEGIES

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCUMENTS = ROOT / "data" / "corpus" / "documents.jsonl.gz"

#: paragraph chunking won recall@5 in the sweep (0.476) and respects the
#: structure the corpus actually has, so planted facts are less likely to be
#: split across a boundary.
DEFAULT_STRATEGY = "paragraph"

DDL = """
ALTER TABLE corpus.chunk DROP COLUMN IF EXISTS embedding;
ALTER TABLE corpus.chunk ADD COLUMN IF NOT EXISTS fts tsvector;
DROP INDEX IF EXISTS corpus.idx_chunk_fts;
"""

# Built after the load, not before: maintaining a GIN index across 100k inserts
# is far slower than building it once at the end.
INDEX = "CREATE INDEX idx_chunk_fts ON corpus.chunk USING GIN (fts)"

RECLAIM = """
ALTER TABLE corpus.document DROP COLUMN IF EXISTS fts;
"""


async def connect(dsn: str, attempts: int = 5) -> asyncpg.Connection:
    for attempt in range(1, attempts + 1):
        try:
            return await asyncpg.connect(dsn, statement_cache_size=0, timeout=60)
        except OSError:
            if attempt == attempts:
                raise
            await asyncio.sleep(2**attempt)
    raise RuntimeError("unreachable")


def read_documents() -> list[tuple[int, str]]:
    if not DOCUMENTS.exists():
        sys.exit(f"{DOCUMENTS} is missing. Run scripts/generate_corpus.py first.")
    out = []
    with gzip.open(DOCUMENTS, "rt") as fh:
        for line in fh:
            d = json.loads(line)
            out.append((d["doc_id"], d["body"]))
    return out


async def main_async(args: argparse.Namespace) -> None:
    documents = read_documents()
    split = STRATEGIES[args.strategy]

    rows: list[tuple] = []
    chunk_id = 0
    for doc_id, body in documents:
        for chunk in split(body, doc_id):
            chunk_id += 1
            rows.append(
                (
                    chunk_id,
                    doc_id,
                    chunk.ordinal,
                    chunk.span_start,
                    chunk.span_end,
                    max(len(chunk.text) // 4, 1),
                    chunk.text,
                )
            )
    print(f"{len(documents):,} documents -> {len(rows):,} chunks ({args.strategy})")
    if not args.load:
        return

    con = await connect(get_settings().require_database_url())
    try:
        size_sql = "select pg_size_pretty(pg_database_size(current_database()))"
        print(f"size before: {await con.fetchval(size_sql)}")
        await con.execute(DDL)
        await con.execute("TRUNCATE corpus.chunk")

        # The tsvector is computed server-side from the text, and the text is
        # then discarded — only the vector and the offsets are stored.
        await con.execute("""
            CREATE TEMP TABLE staging (
                chunk_id INTEGER, doc_id INTEGER, ordinal INTEGER,
                span_start INTEGER, span_end INTEGER, token_count INTEGER, body TEXT
            )
        """)
        started = time.perf_counter()
        for start in range(0, len(rows), 20_000):
            await con.copy_records_to_table(
                "staging",
                columns=[
                    "chunk_id",
                    "doc_id",
                    "ordinal",
                    "span_start",
                    "span_end",
                    "token_count",
                    "body",
                ],
                records=rows[start : start + 20_000],
            )
            print(f"  staged {min(start + 20_000, len(rows)):,} / {len(rows):,}")
        # In batches: to_tsvector over 119k chunks in one statement runs past
        # the statement timeout.
        moved = 0
        while True:
            moved_rows = await con.fetch("""
                WITH batch AS (
                    DELETE FROM staging
                     WHERE chunk_id IN (SELECT chunk_id FROM staging LIMIT 20000)
                 RETURNING *
                )
                INSERT INTO corpus.chunk
                    (chunk_id, doc_id, ordinal, span_start, span_end, token_count, fts)
                SELECT chunk_id, doc_id, ordinal, span_start, span_end, token_count,
                       to_tsvector('english', body)
                  FROM batch
                RETURNING 1
            """)
            n = len(moved_rows)
            moved += n
            if not n:
                break
            print(f"  vectorised {moved:,} / {len(rows):,}")
        await con.execute("DROP TABLE IF EXISTS staging")
        print(f"  indexed in {time.perf_counter() - started:.1f}s")

        started = time.perf_counter()
        await con.execute(INDEX)
        print(f"  GIN index built in {time.perf_counter() - started:.1f}s")

        # Only now: the document-level index is 144MB and does not work.
        await con.execute(RECLAIM)
        await con.execute("VACUUM FULL corpus.document")
        print(f"size after : {await con.fetchval(size_sql)}")
        print(f"chunks     : {await con.fetchval('select count(*) from corpus.chunk'):,}")
    finally:
        await con.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--strategy", default=DEFAULT_STRATEGY, choices=list(STRATEGIES))
    p.add_argument("--load", action="store_true")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
