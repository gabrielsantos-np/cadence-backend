"""Connection pools.

`app_pool` owns conversation storage and connects as the schema owner.
`analyst_pool` connects as a role the database restricts to reads — the analyst
writes its own SQL, so the privilege boundary has to be in Postgres, not here.
See data/app_schema.sql.

Pools are created lazily so the service boots without a database.
"""

import asyncpg

from cadence_backend.core.config import get_settings

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

_app_pool: asyncpg.Pool | None = None
_analyst_pool: asyncpg.Pool | None = None


async def app_pool() -> asyncpg.Pool:
    global _app_pool
    if _app_pool is None:
        _app_pool = await asyncpg.create_pool(
            get_settings().require_database_url(),
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
