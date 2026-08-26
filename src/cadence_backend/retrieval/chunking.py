"""Chunking strategies.

Chunking is applied to `corpus.document.body` at benchmark time rather than
stored, so a sweep costs nothing but CPU and the ground truth — recorded as
character spans against the document — stays valid across every strategy.

Every chunk carries its own span, which is what makes a hit decidable: a chunk
is relevant when its span overlaps a planted fact's span.
"""

import re
from dataclasses import dataclass

#: Rough characters-per-token for English prose. Good enough for sizing chunks;
#: the benchmark reports real token counts from the embedding API where it
#: matters for cost.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Chunk:
    doc_id: int
    ordinal: int
    span_start: int
    span_end: int
    text: str

    def overlaps(self, start: int, end: int) -> bool:
        """Whether this chunk covers any of a planted fact's span."""
        return self.span_start < end and start < self.span_end


def fixed(body: str, doc_id: int, tokens: int, overlap: float = 0.0) -> list[Chunk]:
    """Fixed-size chunks, optionally overlapping.

    Overlap trades index size for a lower chance of splitting a fact across a
    boundary — which is exactly the failure mode the planted facts are built to
    expose, since each is a single sentence that a naive split can bisect.
    """
    size = tokens * CHARS_PER_TOKEN
    stride = max(int(size * (1 - overlap)), 1)
    out: list[Chunk] = []
    for ordinal, start in enumerate(range(0, max(len(body), 1), stride)):
        end = min(start + size, len(body))
        if start >= end:
            break
        out.append(Chunk(doc_id, ordinal, start, end, body[start:end]))
        if end == len(body):
            break
    return out


_PARA = re.compile(r"\n\n+")


def paragraph(body: str, doc_id: int, max_tokens: int = 512) -> list[Chunk]:
    """Split on paragraph boundaries, packing up to a size ceiling.

    Respects the structure the corpus actually has. The planted facts sit at
    paragraph boundaries, so this strategy should never bisect one — the
    benchmark exists to show whether that advantage is real or whether the
    resulting uneven chunk sizes cost more than the clean boundaries gain.
    """
    limit = max_tokens * CHARS_PER_TOKEN
    out: list[Chunk] = []
    start = 0
    buf_start = 0
    buf: list[str] = []

    def flush(end: int) -> None:
        if buf:
            out.append(Chunk(doc_id, len(out), buf_start, end, body[buf_start:end]))

    for part in _PARA.split(body):
        if not part:
            start += 2
            continue
        end = start + len(part)
        if buf and (end - buf_start) > limit:
            flush(start)
            buf, buf_start = [], start
        if not buf:
            buf_start = start
        buf.append(part)
        start = end + 2

    flush(min(start, len(body)))
    return out


#: The arms of the chunking sweep. Names are what appear in the results table.
STRATEGIES = {
    "fixed-256": lambda body, doc_id: fixed(body, doc_id, 256),
    "fixed-512": lambda body, doc_id: fixed(body, doc_id, 512),
    "fixed-1024": lambda body, doc_id: fixed(body, doc_id, 1024),
    "fixed-512-overlap": lambda body, doc_id: fixed(body, doc_id, 512, overlap=0.25),
    "paragraph": lambda body, doc_id: paragraph(body, doc_id, 512),
}
