-- =============================================================================
-- The document corpus and its evaluation ground truth.
--
-- Loads into Supabase alongside the market data. Sized to stay inside the
-- 500MB free tier, which is the binding constraint on the whole design:
-- vectors are expensive, text is moderate, narrow rows are nearly free.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS corpus;
REVOKE ALL ON SCHEMA corpus FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Documents. The durable artefact: chunking is a strategy applied *to* this,
-- so the corpus survives re-chunking and the benchmark can sweep chunk sizes
-- without regenerating anything.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus.document (
    doc_id       INTEGER PRIMARY KEY,
    doc_type     TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    publisher    TEXT    NOT NULL,
    -- Human-facing citation, e.g. "MPA-2026-Q1-014". Mirrors the `reference`
    -- field the analyst already shows on a trace row.
    reference    TEXT    NOT NULL UNIQUE,
    published_on DATE    NOT NULL,
    body         TEXT    NOT NULL
);

COMMENT ON TABLE corpus.document IS
    'Generated market-research corpus. Every figure quoted in a body is derived from data/seed_data.sql, so the documents and the warehouse tell one consistent story.';

-- ---------------------------------------------------------------------------
-- Chunks. Only the WINNING strategy is persisted; the sweep chunks locally
-- from document.body. Offsets rather than a copy of the text, which saves
-- ~100MB of duplication and keeps the corpus inside the tier.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus.chunk (
    chunk_id    INTEGER PRIMARY KEY,
    doc_id      INTEGER NOT NULL REFERENCES corpus.document (doc_id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    span_start  INTEGER NOT NULL,
    span_end    INTEGER NOT NULL,
    token_count INTEGER NOT NULL,
    -- 256 dimensions, not 1536. text-embedding-3-small supports reduction, and
    -- at 1536 the vectors alone would exceed the free tier. Whether that costs
    -- accuracy is arm 5 of the benchmark, not an assumption.
    embedding   vector(256),
    UNIQUE (doc_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_chunk_doc ON corpus.chunk (doc_id, ordinal);

-- ---------------------------------------------------------------------------
-- Ground truth.
--
-- Relevance points at a document and a CHARACTER SPAN, never at a chunk id.
-- A chunk counts as a hit when it overlaps the span. This is what lets the
-- chunking sweep stay honest: re-chunking changes every chunk id, but the
-- planted fact is still in the same place in the same document.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corpus.eval_query (
    query_id       INTEGER PRIMARY KEY,
    egg_id         INTEGER NOT NULL,
    question       TEXT    NOT NULL,
    -- Which warehouse holds the other half of the answer.
    sql_source     TEXT    NOT NULL,
    -- True when the fact is corroborated across several documents. Precision
    -- and nDCG are reported on this subset only: with a single relevant chunk,
    -- precision@k is bounded by construction and means little.
    multi_relevant BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS corpus.eval_relevance (
    query_id   INTEGER NOT NULL REFERENCES corpus.eval_query (query_id) ON DELETE CASCADE,
    doc_id     INTEGER NOT NULL REFERENCES corpus.document (doc_id) ON DELETE CASCADE,
    span_start INTEGER NOT NULL,
    span_end   INTEGER NOT NULL,
    -- 2 = the primary passage, 1 = corroborating. Feeds nDCG's gain.
    grade      SMALLINT NOT NULL DEFAULT 2,
    PRIMARY KEY (query_id, doc_id, span_start)
);

CREATE INDEX IF NOT EXISTS idx_relevance_query ON corpus.eval_relevance (query_id);

COMMIT;
