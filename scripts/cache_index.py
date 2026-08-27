"""Build the BM25 index once and cache it to disk.

    uv run python scripts/cache_index.py

Everything downstream — the tuning harness especially — loads the cache in
about a second instead of rebuilding from Supabase in about seventy.
"""

import asyncio
import time

from cadence_backend.db import app_pool
from cadence_backend.retrieval import index_cache
from cadence_backend.retrieval.supabase_bm25 import SupabaseBM25, index_bytes


async def main() -> None:
    index = SupabaseBM25()
    started = time.perf_counter()
    await index.load(await app_pool())
    print(f"built from Supabase in {time.perf_counter() - started:.1f}s")

    path = index_cache.save(index)
    print(f"cached -> {path}  ({path.stat().st_size / 1e6:.0f}MB on disk)")

    started = time.perf_counter()
    back = index_cache.load(path)
    assert back is not None
    print(f"reload  {time.perf_counter() - started:.2f}s  arrays {index_bytes(back):.0f}MB")

    # A cache that ranks differently from the index it came from is worse than
    # no cache, so prove equality rather than assuming it.
    q = "Harborlight billing divergence catalogue licensing true-up"
    a = [(r.chunk_id, round(r.score, 6)) for r in index.rank(q, 10)]
    b = [(r.chunk_id, round(r.score, 6)) for r in back.rank(q, 10)]
    print("identical ranking:", a == b)
    if a != b:
        raise SystemExit("cache does not reproduce the source index")


if __name__ == "__main__":
    asyncio.run(main())
