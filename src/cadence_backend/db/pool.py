"""Connection pools.

`app_pool` owns conversation storage and connects as the schema owner.
`analyst_pool` connects as a role the database restricts to reads — the analyst
writes its own SQL, so the privilege boundary has to be in Postgres, not here.
See data/app_schema.sql.

Pools are created lazily so the service boots without a database.
"""

import asyncio
import logging

import asyncpg

from cadence_backend.core.config import get_settings

logger = logging.getLogger(__name__)

# asyncpg caches prepared statements per connection. A transaction-mode pooler
# (Supabase's Supavisor, PgBouncer) hands the same client different server
# connections between statements, so a cached statement prepared on one is
# missing on the next — the query fails with a "prepared statement does not
# exist" or a duplicate-name error, usually on the very first request.
#
# Disabling the cache costs nothing here: the analyst writes novel SQL for every
# question, so there is almost nothing to reuse, and the conversation queries are
# a handful of cheap statements.
STATEMENT_CACHE_SIZE = 0

# A pool opens a new connection whenever it needs one, which on a laptop means
# whenever the network happened to blink. That is not hypothetical: a request
# died on `socket.gaierror: nodename nor servname provided` against a hostname
# that resolved fine seconds earlier and seconds later.
#
# Retries wrap connection *establishment* only, so there is no work to duplicate
# — either a connection was opened or it was not. Authentication and permission
# failures are permanent and re-raise immediately; retrying those would turn a
# clear error into a slow one, and could trip account lockout on a real server.
CONNECT_ATTEMPTS = 3
CONNECT_BACKOFF = 0.5


async def _connect(*args: object, **kwargs: object) -> asyncpg.Connection:
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return await asyncpg.connect(*args, **kwargs)  # type: ignore[arg-type]
        except (OSError, asyncpg.PostgresConnectionError) as exc:
            if attempt == CONNECT_ATTEMPTS:
                raise
            logger.warning(
                "database connection attempt %d/%d failed (%s); retrying",
                attempt,
                CONNECT_ATTEMPTS,
                type(exc).__name__,
            )
            await asyncio.sleep(CONNECT_BACKOFF * attempt)
    raise RuntimeError("unreachable")


_app_pool: asyncpg.Pool | None = None
_analyst_pool: asyncpg.Pool | None = None


async def app_pool() -> asyncpg.Pool:
    global _app_pool
    if _app_pool is None:
        _app_pool = await asyncpg.create_pool(
            get_settings().require_database_url(),
            connect=_connect,
            min_size=1,
            max_size=5,
            command_timeout=30,
            statement_cache_size=STATEMENT_CACHE_SIZE,
        )
    return _app_pool


async def analyst_pool() -> asyncpg.Pool:
    global _analyst_pool
    if _analyst_pool is None:
        _analyst_pool = await asyncpg.create_pool(
            get_settings().require_analyst_database_url(),
            connect=_connect,
            min_size=1,
            max_size=4,
            statement_cache_size=STATEMENT_CACHE_SIZE,
            # Belt and braces: the role already sets this, but a pool-level
            # timeout survives someone loosening the role.
            server_settings={"statement_timeout": "15000"},
        )
    return _analyst_pool


async def close_pools() -> None:
    """Close both pools on shutdown."""
    global _app_pool, _analyst_pool
    for pool in (_app_pool, _analyst_pool):
        if pool is not None:
            await pool.close()
    _app_pool = None
    _analyst_pool = None
