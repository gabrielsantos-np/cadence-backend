# cadence-backend

The Python backend for **Cadence**, an AI market analyst.

Cadence answers questions about a market dataset by writing its own SQL, running
it, and composing the result into typed answer blocks — every answer shows its
work: the queries it ran, the rows they returned, and how long it took.

The backend is a standalone service, deployed and scaled independently of the
frontend. The integration boundary is HTTP: the frontend depends on this API
and its schemas, never on the database, the model provider, or anything behind
them.

## Requirements

- Python 3.12 (pinned in `.python-version`)
- [uv](https://docs.astral.sh/uv/)

No database and no API key are needed to run this phase.

## Setup

```sh
uv sync
cp .env.example .env    # optional — every setting has a working default
```

## Development server

```sh
uv run uvicorn cadence_backend.main:app --reload
```

Or, equivalently, through the FastAPI CLI:

```sh
uv run fastapi dev src/cadence_backend/main.py
```

The service listens on `http://127.0.0.1:8000`.

```sh
curl http://127.0.0.1:8000/health          # {"status":"ok"}
```

Interactive API documentation is at [`/docs`](http://127.0.0.1:8000/docs), and
the raw schema at `/openapi.json`.

## Running the full stack locally

Two repositories, one command each. The backend owns the database; the frontend
is only a browser client.

**Terminal 1 — database and API** (this repo):

```sh
make db-up     # Postgres on :5432, dataset loaded on first boot
make dev       # API on :8000
```

**Terminal 2 — frontend** (the `cadence-frontend` repo):

```sh
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 pnpm dev:web    # :3000
```

Then open <http://localhost:3000>. Set `NEXT_PUBLIC_API_BASE_URL` permanently in
the frontend's `apps/web/.env.local` if you would rather not repeat it — it tells
the browser where this API lives, so the frontend needs it to reach anything.

Other database commands:

```sh
make db-reset  # destroy the volume and reload the dataset from scratch
make db-down   # stop it
make db-logs   # follow Postgres logs
```

Adminer is on <http://localhost:8080> (server `db`, user/password `postgres`)
for browsing the dataset.

### The dataset

`data/` holds the synthetic market dataset: `schema.sql` (10 tables, 8 views),
`seed_data.sql` (ten invented services, Jan 2024 – Jun 2026), `app_schema.sql`
(the `app` schema for conversations, plus the restricted `analyst_ro` role) and
`data_dictionary.md`. Read the dictionary before writing queries — it documents
the three grains, the bundled-revenue trap and the known data gaps.

`data/snowflake/schema.sql` is **generated** from `data/schema.sql` by
`scripts/translate_schema.py`; edit the Postgres file and regenerate rather than
editing it directly. The translation is not cosmetic — Snowflake rejects `CHECK`
constraints, `CREATE INDEX`, `DISTINCT ON`, `FILTER (WHERE ...)`, `BOOL_OR`,
`STRING_AGG`, `AGE()` and implicit string concatenation, all of which the views
use. Each rule fails loudly if it stops matching, because a silently
mistranslated view is a wrong answer rather than a crash.

## Where the data lives

Two independent choices.

**Conversation storage** follows `DATABASE_URL` — local Postgres, or Supabase by
pointing it at the session pooler. Use the **session** pooler (port 5432), not
transaction mode: this is a long-lived process with connection pools, which is
what session mode is for.

**Market data** follows `MARKET_SOURCE`:

| `MARKET_SOURCE` | The analyst queries | Configured by |
|---|---|---|
| `postgres` (default) | Postgres, as the read-only `analyst_ro` role | `ANALYST_DATABASE_URL` |
| `snowflake` | Snowflake, as the read-only `CADENCE_ANALYST` login | the `SNOWFLAKE_*` settings |

One SQL source is active at a time, deliberately. Registering both would put
both schemas in every prompt and make the model choose, which changes how it
answers — the switch exists to compare like with like.

### Hosting conversations on Supabase

```sh
# .env
DATABASE_URL=postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
ANALYST_DB_PASSWORD=<choose one>

make db-load     # applies schema, seed and app schema; creates analyst_ro
```

Then set `ANALYST_DATABASE_URL` to the same host with the custom-role username
form — `analyst_ro.<project-ref>` — and the password you chose.

Two things that will bite otherwise: the pooler does not support prepared
statements (the pools already set `statement_cache_size=0`), and Supabase's
Connect dialog shows a literal `[YOUR-PASSWORD]` placeholder — leaving the
brackets in produces a baffling *"does not appear to be an IPv4 or IPv6
address"* error, because Python reads `[...]` as an IPv6 literal.

### Using Snowflake for market data

```sh
# .env — admin credentials are used ONCE and never to serve a request
SNOWFLAKE_ACCOUNT=<org>-<account>
SNOWFLAKE_ADMIN_USER=...
SNOWFLAKE_ADMIN_PASSWORD=...
SNOWFLAKE_ANALYST_PASSWORD=<choose one>
SNOWFLAKE_PASSWORD=<the same value>

make snowflake-setup     # database, warehouse, read-only role, analyst login, data
make snowflake-check     # prove the boundary holds
```

Then set `MARKET_SOURCE=snowflake` and restart. Other targets:
`make snowflake-grants` re-applies grants without reloading data.

The analyst login is created as `TYPE = LEGACY_SERVICE`: Snowflake enforces MFA
on password sign-ins for human users, which a background service cannot satisfy,
and this type is exempt. It also cannot sign in to Snowsight, which is correct
for a login that only ever runs analyst queries.

## The API

### `GET /health`

Returns `{"status": "ok"}`. Deliberately depends on nothing — not the database,
not the model provider — so it reports on this process only.

### `POST /api/chat`

```json
{
  "question": "Which service leads on revenue?",
  "conversationId": null,
  "model": "anthropic/claude-opus-5"
}
```

`question` is required. `conversationId` is optional; omitting it means "start a
new conversation". `model` is optional and is **ignored unless it is on the
allowlist** in `core/models.py` — a client-supplied model string must never
reach the provider.

The response is `text/event-stream`. SSE is the transport; every payload is
JSON:

```
event: conversation
data: {"id":"...","isNew":true}

event: step
data: {"step":{"id":"s1","kind":"sql","label":"Revenue by service","durationMs":12,"source":"Queried market dataset","sql":"SELECT ...","columns":["service"],"rows":[["Grovehouse"]],"rowCount":1},"elapsedMs":900}

event: answer
data: {"blocks":[],"elapsedMs":1200}

event: done
data: {}
```

`step.kind` is one of `sql`, `search` or `note`. The ordering is fixed:
`conversation` is always first, `done` always last, and `answer` is emitted even
when a run fails — an `error` event accompanies it rather than replacing it.

The payload vocabulary (`AnswerBlock`, `TraceStep`) lives in
`src/cadence_backend/schemas/`. The wire format is **camelCase**, matching what
the browser client consumes; Python stays snake_case internally. Absent optional
fields are omitted rather than sent as `null`, on both the stream and the REST
routes — the client distinguishes the two.

Because the request is a POST with a JSON body, clients consume this with
`fetch()` and a stream reader, not the browser's `EventSource`.

Failures that happen *before* the stream starts are ordinary HTTP errors:

```json
{ "error": { "code": "BAD_REQUEST", "message": "Invalid JSON body." } }
```

Once the stream has started, failures are reported with the `error` event
instead.

## Configuration

Settings are read from the environment (or `.env`) via `pydantic-settings`; see
`.env.example`. Nothing here is required to boot.

| Variable | Default | Notes |
|---|---|---|
| `APP_NAME` | `Cadence API` | |
| `APP_VERSION` | `0.1.0` | |
| `ENVIRONMENT` | `development` | |
| `LOG_LEVEL` | `INFO` | |
| `FRONTEND_ORIGINS` | `http://localhost:3000` | Comma-separated. Never `*`. |
| `OPENROUTER_API_KEY` | unset | Required to answer questions. Absent, the service still starts and `/health` passes. |
| `OPENROUTER_MODEL` | `anthropic/claude-opus-5` | |
| `DATABASE_URL` | unset | Conversation storage. Local Postgres or Supabase. |
| `MARKET_SOURCE` | `postgres` | `postgres` or `snowflake`. |
| `ANALYST_DATABASE_URL` | unset | The read-only market connection, when `MARKET_SOURCE=postgres`. |
| `ANALYST_DB_PASSWORD` | unset | Password `make db-load` gives the `analyst_ro` role it creates. |
| `SNOWFLAKE_ACCOUNT` | unset | The identifier (`org-account`), not the hostname. |
| `SNOWFLAKE_USER` / `SNOWFLAKE_PASSWORD` | unset | The analyst login, when `MARKET_SOURCE=snowflake`. |
| `SNOWFLAKE_ROLE` | `CADENCE_ANALYST_RO` | Read-only role the analyst connects with. |
| `SNOWFLAKE_DATABASE` / `_SCHEMA` / `_WAREHOUSE` | `CADENCE` / `MARKET` / `COMPUTE_WH` | |
| `SNOWFLAKE_ADMIN_*` | unset | Setup only. Never used to serve a request — safe to blank afterwards. |

Every secret is a `SecretStr`, so a settings object in a log line or traceback
renders it as `**********` rather than the value.

`.env` is gitignored and must never be committed.

## Testing

```sh
uv run pytest
```

The tests assert the HTTP contract, not implementation internals: that `/health`
answers, that `/api/chat` returns well-formed SSE frames with recognised event
names terminating in `done`, that invalid requests produce the coded error
shape, and that the payload models serialise to the exact camelCase JSON the
frontend expects.

## Linting

```sh
uv run ruff check .
uv run ruff format --check .
```

## Layout

```
src/cadence_backend/
├── main.py                 app factory, CORS, logging, error handlers
├── api/
│   ├── health.py           GET /health
│   ├── chat.py             POST /api/chat — the SSE stream
│   └── conversations.py    GET /api/conversations[/{id}]
├── analyst/
│   ├── engine.py           the two-pass loop
│   ├── prompts.py          system prompt, compose prompt, answer schema
│   └── tools.py            tool schemas, derived from the source registry
├── sources/
│   ├── types.py            SqlSource / DocumentSource
│   ├── market.py           the SQL source, connecting as analyst_ro
│   ├── market_schema.py    curated schema context for the prompt
│   └── notes.py            document source over the research notes
├── conversations/store.py  persistence for app.conversation / app.message
├── db/
│   ├── pool.py             the app pool and the analyst pool
│   └── readonly.py         read-only guard, row cap, cell rendering
├── llm/                    OpenRouter client + defensive JSON parsing
├── core/                   settings, model allowlist, SSE framing
└── schemas/                request, SSE events, TraceStep, AnswerBlock

scripts/
├── load_dataset.py         apply the dataset to any Postgres URL
├── translate_schema.py     regenerate data/snowflake/schema.sql
├── setup_snowflake.py      database, warehouse, read-only role, analyst login
└── check_snowflake_boundary.py   prove the read-only boundary holds
```

`sources/` also holds `snowflake.py` and `snowflake_schema.py`, used when
`MARKET_SOURCE=snowflake`. The Snowflake schema context reuses the Postgres one
verbatim and appends dialect notes — the grains and traps are facts about the
data, not the engine, and duplicating them is how the two drift.

## Security

The frontend and backend are separate applications; the only thing joining them
is HTTP. What protects that boundary:

**In place now**

- **CORS is an allowlist, never `*`.** `FRONTEND_ORIGINS` names the origins
  allowed to call this API. A browser discards a cross-origin response that
  lacks the matching header, so this is what stops any other page from calling
  the API with a visitor's browser.
- **Credentials are off** (`allow_credentials=False`). There are no cookies and
  no session; turning this on would require pinning exact origins and would
  open a CSRF surface.
- **The model id is an allowlist** (`core/models.py`). A client-supplied model
  string is dropped unless it is one of the three known ids, so nobody can
  redirect spend onto an arbitrary model.
- **Errors say nothing useful to an attacker.** Unhandled exceptions log the
  traceback server-side and return a flat `Internal server error.`; validation
  errors name the offending fields without echoing their values. Connection
  strings, keys and paths cannot escape in a response body.
- **Secrets stay out of logs.** `OPENROUTER_API_KEY` is a `SecretStr`, so a
  settings object in a log line or traceback renders it as `**********`. Request
  logging records the question's *length*, never its text.
- **`.env` is gitignored**; only `.env.example`, with empty placeholders, is
  committed.

**Local development**

Binding to `127.0.0.1` (the default for `uvicorn` and `fastapi dev`) keeps the
API off your local network. `--host 0.0.0.0` exposes it to anyone routable to
your machine — on a café or office network, that is everyone. Prefer the
default unless you are deliberately testing from another device.

**Before this is exposed beyond localhost**

None of the following is needed to run locally, and none of it is implemented:

- **Authentication.** `POST /api/chat` is currently unauthenticated. Once it
  reaches a real model provider it becomes an open, billable LLM proxy —
  anyone who can reach the URL can spend the OpenRouter budget. This is the
  single most important gap to close before any public deployment; the frontend
  should pass a session token or the API should sit behind an authenticating
  proxy.
- **Rate limiting / request quotas**, for the same reason. An analyst run is
  several model calls and can last a minute or more.
- **TLS.** SSE over plain HTTP is readable in transit; terminate TLS at a proxy
  and set `FRONTEND_ORIGINS` to `https://` origins.
- **A request body size limit and a run timeout**, so one client cannot pin a
  worker open indefinitely.
- **Trusted-host / proxy headers** if it runs behind a load balancer.
- **Never `allow_origins=["*"]`** in a deployed configuration, whatever a
  browser error suggests.

The one boundary that is *not* the application's job is the analyst's SQL: see
the note below.

## Architecture notes

The analyst runs two model calls per question, deliberately kept separate:

```
question → gather/evidence tool loop → SQL → database → results
         → composition pass → AnswerBlocks
```

Merging them makes the gathering step worse at deciding what to query next.

**The security boundary for analyst SQL belongs to the warehouse, not to this
application.** The analyst writes its own SQL, so a regex could never be the
guarantee. `db/readonly.py` is defence in depth — it exists so a bad query fails
with a message the model can act on, and so multi-statement injection never
reaches a driver.

On **Postgres**, the analyst connects as `analyst_ro`: `SELECT` on market data
and nothing else. Writes and DDL fail as read-only violations, the `app` schema
is permission-denied so a prompt-injected query cannot read chat history, and a
15s statement timeout applies at the role level.

On **Snowflake** none of those mechanisms exist — there is no
`default_transaction_read_only`, no read-only transaction, and no role-level
statement timeout. The equivalent had to be built rather than translated:

- a **dedicated login** whose only role is `CADENCE_ANALYST_RO`. This is the
  load-bearing part: Snowflake SQL can contain `USE ROLE ...`, and a login that
  simply *has* no admin role makes escalation impossible in a way no keyword
  list can.
- `SELECT` on tables and views only — no `INSERT`, `CREATE` or `OWNERSHIP`, so
  writes fail as privilege errors.
- `STATEMENT_TIMEOUT_IN_SECONDS = 15` on both the warehouse and the user.

`make snowflake-check` runs eight escalation attempts as the analyst — write,
update, delete, create, drop, `USE ROLE ACCOUNTADMIN`, reading
`SNOWFLAKE.ACCOUNT_USAGE`, creating a role — and exits non-zero if any succeeds.
It runs the real statements rather than the validator, so it tests Snowflake's
refusal rather than ours. Run it after any change to grants.

**Adding a source** is two steps: a module in `sources/` exporting a `SqlSource`
or a `DocumentSource`, then a line in `SOURCES`. Tool schemas, the source enums
the model picks from, and the prompt's schema context all derive from that list.
Each SQL source supplies a *curated* schema description including its traps — an
`information_schema` dump omits exactly the things that cause wrong answers. At
three or more SQL sources, add a routing call before the loop; below three it
costs more than it saves. Sources cannot be joined in SQL.

**Failures must still persist.** A model error, a network error or a client
disconnecting mid-run is caught so the engine turn is written either way —
letting one escape leaves a conversation with a question and no reply.
