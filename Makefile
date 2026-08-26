# Development commands for the Cadence backend.
#
#   make dev     run the API on :8000 with reload
#   make test    run the test suite
#   make check   everything CI would run

.PHONY: install dev test lint fmt check bench db-up db-down db-reset db-logs db-load snowflake-setup snowflake-internal snowflake-check

PORT ?= 8000

install:
	uv sync

# Binds to 127.0.0.1 deliberately: 0.0.0.0 would expose the API to everyone
# routable to this machine. Override with HOST=0.0.0.0 only on purpose.
HOST ?= 127.0.0.1

dev:
	uv run uvicorn cadence_backend.main:app --reload --host $(HOST) --port $(PORT)

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

check: lint test

# --- database -------------------------------------------------------------
# The backend owns Postgres; the frontend is only a browser client.

db-up:
	docker compose up -d db
	@printf 'waiting for postgres'
	@until docker compose exec -T db pg_isready -U postgres -d market >/dev/null 2>&1; do \
		printf '.'; sleep 1; \
	done; echo ' ready'

db-down:
	docker compose down

# Destroys the volume, so the three SQL files reload from scratch.
db-reset:
	docker compose down -v
	$(MAKE) db-up

db-logs:
	docker compose logs -f db

# Load the dataset into a database that has no docker init hook — a hosted
# Postgres such as Supabase. Reads DATABASE_URL from the environment or .env.
#   make db-load
#   make db-load URL=postgres://...
db-load:
	uv run python scripts/load_dataset.py $(if $(URL),--url $(URL),)

# --- Snowflake ------------------------------------------------------------
# One-time: create the database, warehouse, read-only role and the dedicated
# analyst login, then load the dataset. Needs the SNOWFLAKE_ADMIN_* settings.
snowflake-setup:
	uv run python scripts/setup_snowflake.py

# Add the FINANCE and SUPPORT schemas to an account that already has MARKET.
snowflake-internal:
	uv run python scripts/setup_snowflake.py --only-internal

# Re-run the grants without reloading data.
snowflake-grants:
	uv run python scripts/setup_snowflake.py --skip-data

# Prove the read-only boundary actually holds.
snowflake-check:
	uv run python scripts/check_snowflake_boundary.py

# --- retrieval benchmark ---------------------------------------------------
# Compares retrieval strategies offline against the planted-fact ground truth.
#   make bench
#   make bench ARGS="--arms bm25,term-overlap"
bench:
	uv run python scripts/bench_retrieval.py $(ARGS)
