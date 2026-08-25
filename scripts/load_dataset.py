"""Load the market dataset into any Postgres database.

Local development gets this for free: docker compose mounts the SQL files into
the Postgres init directory. A hosted database (Supabase, RDS) has no such hook,
so this applies the same files, in the same order, over a normal connection.

    uv run python scripts/load_dataset.py                  # uses DATABASE_URL
    uv run python scripts/load_dataset.py --url postgres://...

Order matters: schema, then seed data, then the app schema — the grants in
app_schema.sql reference tables the first file creates.
"""

import argparse
import asyncio
import pathlib
import sys

import asyncpg

DATA = pathlib.Path(__file__).resolve().parent.parent / "data"
FILES = ("schema.sql", "seed_data.sql", "app_schema.sql")


async def load(url: str, files: tuple[str, ...], analyst_password: str | None) -> None:
    connection = await asyncpg.connect(url, statement_cache_size=0)
    try:
        version = await connection.fetchval("SELECT version()")
        print(f"connected: {version.split(',')[0]}")

        # app_schema.sql reads this to set the analyst role's password. Without
        # it the file falls back to the local-development default.
        if analyst_password:
            await connection.execute(
                "SELECT set_config('cadence.analyst_password', $1, false)",
                analyst_password,
            )
            print("analyst_ro password: supplied")
        else:
            print("analyst_ro password: local default (set ANALYST_DB_PASSWORD to change)")
        for name in files:
            path = DATA / name
            if not path.exists():
                raise SystemExit(f"missing {path}")
            sql = path.read_text()
            print(f"applying {name} ({len(sql):,} bytes) ...", end=" ", flush=True)
            await connection.execute(sql)
            print("ok")

        tables = await connection.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        services = await connection.fetchval("SELECT count(*) FROM streaming_service")
        app_tables = await connection.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'app'"
        )
        role = await connection.fetchval(
            "SELECT rolconfig FROM pg_roles WHERE rolname = 'analyst_ro'"
        )
        print(f"\npublic tables : {tables}")
        print(f"services      : {services}")
        print(f"app tables    : {app_tables}")
        print(f"analyst_ro    : {role or 'MISSING'}")
    finally:
        await connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=None,
        help="Postgres connection string. Defaults to DATABASE_URL from the environment or .env.",
    )
    parser.add_argument(
        "--analyst-password",
        default=None,
        help=(
            "Password for the read-only analyst role. Defaults to "
            "$ANALYST_DB_PASSWORD. Always set this on a hosted database."
        ),
    )
    parser.add_argument(
        "--only",
        choices=FILES,
        help="Apply just one file, e.g. after editing app_schema.sql.",
    )
    args = parser.parse_args()

    # Fall back to the application's own settings, so this reads .env exactly
    # the way the service does rather than keeping a second copy of the rules.
    url = args.url
    analyst_password = args.analyst_password
    if url is None or analyst_password is None:
        from cadence_backend.core.config import get_settings

        settings = get_settings()
        if url is None:
            url = settings.database_url.get_secret_value() if settings.database_url else None
        if analyst_password is None:
            analyst_password = (
                settings.analyst_db_password.get_secret_value()
                if settings.analyst_db_password
                else None
            )

    if not url:
        sys.exit("No connection string. Pass --url or set DATABASE_URL in .env.")

    # Connect as the owner: this creates roles and grants.
    asyncio.run(load(url, (args.only,) if args.only else FILES, analyst_password))


if __name__ == "__main__":
    main()
