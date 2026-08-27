"""Persist a built BM25 index to disk so iteration does not pay to rebuild it.

Building from Supabase reads 119MB of tsvectors over a pooled connection and
takes seventy-odd seconds. That is fine once at startup and intolerable in a
tuning loop, where the index is the constant and the ranking is the variable.

The arrays are already the compact form the index scores against, so caching is
mostly `np.savez` — the only work is flattening the two Python dicts (`terms`
and `meta`) into parallel arrays and rebuilding them on load.
"""

import json
import pathlib
import time

import numpy as np

from cadence_backend.retrieval.supabase_bm25 import SupabaseBM25

DEFAULT = pathlib.Path(__file__).resolve().parents[3] / "data" / "corpus" / "bm25_index.npz"


def save(index: SupabaseBM25, path: pathlib.Path = DEFAULT) -> pathlib.Path:
    if not index.loaded:
        raise ValueError("refusing to cache an index that was never built")
    terms = list(index.terms)
    bounds = np.array([index.terms[t] for t in terms], dtype=np.int64)
    chunk_ids = index.chunk_ids
    meta = np.array([index.meta[int(c)] for c in chunk_ids], dtype=np.int32)

    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        path,
        chunk_pos=index.chunk_pos,
        freqs=index.freqs,
        lengths=index.lengths,
        chunk_ids=chunk_ids,
        term_bounds=bounds,
        idf=np.array([index.idf[t] for t in terms], dtype=np.float64),
        meta=meta,
        avg_len=np.array([index.avg_len]),
        params=np.array([index.k1, index.b]),
    )
    # Terms are strings; json keeps them readable and out of the npz object path,
    # which would otherwise need allow_pickle on load.
    path.with_suffix(".terms.json").write_text(json.dumps(terms))
    return path


def load(path: pathlib.Path = DEFAULT) -> SupabaseBM25 | None:
    """Return a ready index, or None if no cache exists."""
    if not path.exists() or not path.with_suffix(".terms.json").exists():
        return None
    started = time.perf_counter()
    z = np.load(path)
    terms = json.loads(path.with_suffix(".terms.json").read_text())

    index = SupabaseBM25(k1=float(z["params"][0]), b=float(z["params"][1]))
    index.chunk_pos = z["chunk_pos"]
    index.freqs = z["freqs"]
    index.lengths = z["lengths"]
    index.chunk_ids = z["chunk_ids"]
    bounds, idf = z["term_bounds"], z["idf"]
    index.terms = {t: (int(lo), int(hi)) for t, (lo, hi) in zip(terms, bounds, strict=True)}
    index.idf = dict(zip(terms, (float(v) for v in idf), strict=True))
    index.meta = {
        int(c): (int(d), int(s), int(e))
        for c, (d, s, e) in zip(index.chunk_ids, z["meta"], strict=True)
    }
    index.avg_len = float(z["avg_len"][0])
    index.loaded = True
    index.load_seconds = time.perf_counter() - started
    return index
