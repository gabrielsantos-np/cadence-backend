"""Embeddings, behind a small interface with a disk cache.

The existing `llm/client.py` points at OpenRouter, which is a chat-completions
gateway and does not serve `/embeddings`. So this needs its own client, its own
key and its own base URL — that separation is the point of the module, not an
accident of layout.

Everything is cached to disk keyed by (model, dimensions, text). The benchmark
re-embeds the corpus once per chunking strategy, and five strategies over 96k
chunks is real money and real minutes if nothing is reused. Chunk text repeats
heavily across strategies — a 512-token chunk and a 1024-token chunk starting
at the same offset share a prefix, and paragraph chunks often match fixed ones
exactly — so the cache hit rate is high in practice.
"""

import asyncio
import hashlib
import json
import logging
import pathlib
import sqlite3
import struct
from typing import Protocol

import numpy as np

from cadence_backend.core.config import get_settings

logger = logging.getLogger(__name__)

CACHE = pathlib.Path(__file__).resolve().parents[3] / "data" / "corpus" / "embeddings.sqlite"

#: OpenAI's small model supports dimension reduction. 256 keeps the corpus
#: inside the Supabase free tier; whether that costs accuracy is an arm of the
#: benchmark, not an assumption.
DEFAULT_MODEL = "text-embedding-3-small"
DEFAULT_DIMENSIONS = 256

#: Published price per million tokens. Used to report cost per 1k queries
#: rather than to bill anything.
PRICE_PER_MTOK = {"text-embedding-3-small": 0.02, "text-embedding-3-large": 0.13}

#: The API caps a single request; batching well below it keeps retries cheap.
BATCH = 256


class Embedder(Protocol):
    model: str
    dimensions: int

    async def embed(self, texts: list[str]) -> np.ndarray: ...


def _key(model: str, dims: int, text: str) -> str:
    return hashlib.sha256(f"{model}|{dims}|{text}".encode()).hexdigest()


class _Cache:
    """SQLite rather than a dict of files: 96k rows of 1KB blobs each."""

    def __init__(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS vec (k TEXT PRIMARY KEY, v BLOB)")
        self.db.commit()

    def get_many(self, keys: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for i in range(0, len(keys), 900):  # SQLite variable limit
            batch = keys[i : i + 900]
            marks = ",".join("?" * len(batch))
            for k, v in self.db.execute(f"SELECT k, v FROM vec WHERE k IN ({marks})", batch):
                out[k] = np.frombuffer(v, dtype=np.float32)
        return out

    def put_many(self, items: list[tuple[str, np.ndarray]]) -> None:
        self.db.executemany(
            "INSERT OR REPLACE INTO vec (k, v) VALUES (?, ?)",
            [(k, v.astype(np.float32).tobytes()) for k, v in items],
        )
        self.db.commit()


class ApiEmbedder:
    """An OpenAI-compatible embeddings endpoint."""

    def __init__(self, model: str = DEFAULT_MODEL, dimensions: int = DEFAULT_DIMENSIONS) -> None:
        self.model = model
        self.dimensions = dimensions
        self.cache = _Cache(CACHE)
        self.tokens_used = 0
        self._client = None

    def _api(self):
        if self._client is None:
            from openai import AsyncOpenAI

            settings = get_settings()
            self._client = AsyncOpenAI(
                api_key=settings.require_embedding_api_key(),
                base_url=settings.embedding_base_url,
                max_retries=3,
                timeout=60.0,
            )
        return self._client

    async def embed(self, texts: list[str]) -> np.ndarray:
        keys = [_key(self.model, self.dimensions, t) for t in texts]
        cached = self.cache.get_many(keys)

        missing = [
            (i, t) for i, (k, t) in enumerate(zip(keys, texts, strict=True)) if k not in cached
        ]
        if missing:
            logger.info("embedding %d texts (%d cached)", len(missing), len(cached))
            fresh: list[tuple[str, np.ndarray]] = []
            for start in range(0, len(missing), BATCH):
                batch = missing[start : start + BATCH]
                response = await self._api().embeddings.create(
                    model=self.model,
                    input=[t for _, t in batch],
                    dimensions=self.dimensions,
                )
                self.tokens_used += response.usage.total_tokens
                for (i, _), item in zip(batch, response.data, strict=True):
                    fresh.append((keys[i], np.asarray(item.embedding, dtype=np.float32)))
            self.cache.put_many(fresh)
            cached.update(dict(fresh))

        return np.vstack([cached[k] for k in keys])

    @property
    def cost_usd(self) -> float:
        return self.tokens_used / 1e6 * PRICE_PER_MTOK.get(self.model, 0.0)


def normalise(matrix: np.ndarray) -> np.ndarray:
    """Unit-length rows, so a dot product is cosine similarity."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-12)


async def embed_all(embedder: Embedder, texts: list[str], concurrency: int = 4) -> np.ndarray:
    """Embed a large list, a few batches at a time.

    Chunked rather than one gather over 96k texts: the API rejects oversized
    requests and unbounded concurrency just converts a rate limit into retries.
    """
    step = BATCH * concurrency
    parts = []
    for start in range(0, len(texts), step):
        window = texts[start : start + step]
        results = await asyncio.gather(
            *(embedder.embed(window[i : i + BATCH]) for i in range(0, len(window), BATCH))
        )
        parts.extend(results)
    return np.vstack(parts) if parts else np.zeros((0, embedder.dimensions), dtype=np.float32)


def available() -> tuple[bool, str]:
    """Whether an embeddings backend is configured, and why not if it isn't."""
    settings = get_settings()
    if settings.embedding_api_key is None:
        return False, (
            "EMBEDDING_API_KEY is not set. OpenRouter does not serve /embeddings, "
            "so the dense, hybrid and rerank arms need a separate key "
            "(OpenAI or any OpenAI-compatible endpoint)."
        )
    return True, ""


def load_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def pack(vec: np.ndarray) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)
