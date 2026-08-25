-- =============================================================================
-- Application schema: conversation persistence + the analyst's read-only role.
--
-- Loads AFTER schema.sql and seed_data.sql — the GRANTs below reference the
-- market tables, so they must already exist.
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- The analyst role.
--
-- The analyst writes its own SQL, so the database — not the application — is
-- what makes that safe. This role can read the market data and nothing else:
-- no writes, no DDL, no access to the app schema, and a hard statement timeout
-- so a runaway query cannot pin a connection.
-- ---------------------------------------------------------------------------
-- The password comes from the `cadence.analyst_password` setting when the
-- loader supplies one, and falls back to the local-development default. A
-- hosted database is reachable from the internet, so pass a real one there:
--   ANALYST_DB_PASSWORD=... make db-load
DO $$
DECLARE
    password TEXT := COALESCE(
        NULLIF(current_setting('cadence.analyst_password', TRUE), ''),
        'analyst_ro'
    );
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analyst_ro') THEN
        EXECUTE format('ALTER ROLE analyst_ro LOGIN PASSWORD %L', password);
    ELSE
        EXECUTE format('CREATE ROLE analyst_ro LOGIN PASSWORD %L', password);
    END IF;
END
$$;

-- Not a literal database name: this file also runs against a hosted database
-- called something else (Supabase names it `postgres`).
DO $$
BEGIN
    EXECUTE format(
        'GRANT CONNECT ON DATABASE %I TO analyst_ro', current_database()
    );
END
$$;
GRANT USAGE ON SCHEMA public TO analyst_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO analyst_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO analyst_ro;

-- Belt and braces: no object creation, every transaction read-only, and a
-- statement timeout that applies even if the application forgets to set one.
REVOKE CREATE ON SCHEMA public FROM analyst_ro;
ALTER ROLE analyst_ro SET default_transaction_read_only = on;
ALTER ROLE analyst_ro SET statement_timeout = '15s';
ALTER ROLE analyst_ro SET idle_in_transaction_session_timeout = '30s';

-- ---------------------------------------------------------------------------
-- Conversation storage. Owned by postgres; analyst_ro is deliberately not
-- granted anything here, so a prompt-injected query cannot read chat history.
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS app;
REVOKE ALL ON SCHEMA app FROM PUBLIC;

CREATE TABLE IF NOT EXISTS app.conversation (
    id         UUID PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app.message (
    id              UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES app.conversation (id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('user', 'engine')),
    -- The whole message payload: text for a user turn, or {steps, blocks} for
    -- an engine turn. Kept as jsonb so the answer-block vocabulary can evolve
    -- without a migration.
    payload         JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (conversation_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_message_conversation
    ON app.message (conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_conversation_updated
    ON app.conversation (updated_at DESC);

COMMIT;
