"""Create the Snowflake objects the analyst needs, and load the dataset.

Run once, with admin credentials:

    make snowflake-setup

What it builds, and why it is shaped this way:

The analyst writes its own SQL, so the privilege boundary has to belong to the
warehouse, not to this application — the same principle as `analyst_ro` on
Postgres. Snowflake expresses it differently, and one difference matters:

  * Postgres has `default_transaction_read_only` and `BEGIN TRANSACTION READ
    ONLY`. Snowflake has neither. Read-only is *only* the absence of write
    grants, so the grants below are the whole guarantee.
  * A dedicated user is not decoration. Snowflake SQL can contain
    `USE ROLE ...`, so giving the analyst a login whose only role is the
    read-only one is what makes escalation impossible — a regex could not.
  * `STATEMENT_TIMEOUT_IN_SECONDS` replaces Postgres's role-level
    `statement_timeout`.
"""

import argparse
import pathlib
import re
import sys

import snowflake.connector

ROOT = pathlib.Path(__file__).resolve().parent.parent
SNOWFLAKE_DATA = ROOT / "data" / "snowflake"
SEED = ROOT / "data" / "seed_data.sql"

STATEMENT_TIMEOUT_SECONDS = 15

# Identifiers are interpolated into DDL, so they are constrained rather than
# escaped: anything outside this shape is rejected before it reaches Snowflake.
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def ident(value: str, name: str) -> str:
    if not IDENTIFIER.match(value):
        sys.exit(f"{name} is not a valid Snowflake identifier: {value!r}")
    return value.upper()


def run(cursor, sql: str, *, quiet: bool = False) -> None:
    if not quiet:
        print(f"  {sql.strip().splitlines()[0][:96]}")
    cursor.execute(sql)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-data",
        action="store_true",
        help="Create roles and grants only; do not reload schema or seed data.",
    )
    args = parser.parse_args()

    from cadence_backend.core.config import get_settings

    settings = get_settings()
    database = ident(settings.snowflake_database, "SNOWFLAKE_DATABASE")
    schema = ident(settings.snowflake_schema, "SNOWFLAKE_SCHEMA")
    warehouse = ident(settings.snowflake_warehouse, "SNOWFLAKE_WAREHOUSE")
    role = ident(settings.snowflake_role, "SNOWFLAKE_ROLE")
    analyst_user = ident(settings.snowflake_user or "", "SNOWFLAKE_USER")

    # The same value the service authenticates with: this script sets the
    # password, the analyst connects with it. One variable, so the two cannot
    # drift apart and leave the analyst unable to log in.
    if settings.snowflake_password is None:
        sys.exit("SNOWFLAKE_PASSWORD is not set. Choose one and add it to .env.")
    analyst_password = settings.snowflake_password.get_secret_value()

    connection = snowflake.connector.connect(**settings.snowflake_admin_connect_args())
    cursor = connection.cursor()
    try:
        print(f"connected as {settings.snowflake_admin_user} / {settings.snowflake_admin_role}")
        print(f"account: {settings.snowflake_account}\n")

        print("infrastructure")
        run(
            cursor,
            f"CREATE WAREHOUSE IF NOT EXISTS {warehouse} "
            "WAREHOUSE_SIZE = XSMALL AUTO_SUSPEND = 60 AUTO_RESUME = TRUE "
            "INITIALLY_SUSPENDED = TRUE",
        )
        run(cursor, f"CREATE DATABASE IF NOT EXISTS {database}")
        run(cursor, f"CREATE SCHEMA IF NOT EXISTS {database}.{schema}")
        # A statement timeout on the warehouse applies even if a session
        # forgets to set one — the closest thing to Postgres's role-level
        # statement_timeout.
        run(
            cursor,
            f"ALTER WAREHOUSE {warehouse} SET "
            f"STATEMENT_TIMEOUT_IN_SECONDS = {STATEMENT_TIMEOUT_SECONDS}",
        )

        print("\nread-only role")
        run(cursor, f"CREATE ROLE IF NOT EXISTS {role}")
        run(cursor, f"GRANT USAGE ON WAREHOUSE {warehouse} TO ROLE {role}")
        run(cursor, f"GRANT USAGE ON DATABASE {database} TO ROLE {role}")
        run(cursor, f"GRANT USAGE ON SCHEMA {database}.{schema} TO ROLE {role}")
        # SELECT and nothing else. No INSERT, no UPDATE, no CREATE, no
        # OWNERSHIP: writes fail as a privilege error, not as a regex match.
        for on in ("TABLES", "VIEWS"):
            run(cursor, f"GRANT SELECT ON ALL {on} IN SCHEMA {database}.{schema} TO ROLE {role}")
            run(cursor, f"GRANT SELECT ON FUTURE {on} IN SCHEMA {database}.{schema} TO ROLE {role}")

        print("\ndedicated analyst login")
        cursor.execute(f"SHOW USERS LIKE '{analyst_user}'")
        user_exists = bool(cursor.fetchall())

        if user_exists:
            print(f"  {analyst_user} already exists")
        else:
            run(
                cursor,
                f"CREATE USER {analyst_user} "
                f"PASSWORD = '{analyst_password}' "
                f"DEFAULT_ROLE = {role} DEFAULT_WAREHOUSE = {warehouse} "
                f"DEFAULT_NAMESPACE = {database}.{schema} "
                # LEGACY_SERVICE, not the default PERSON: Snowflake enforces
                # MFA on password sign-ins for human users, which a background
                # service cannot satisfy. This type exists for exactly this
                # case and is exempt. It also cannot sign in to Snowsight,
                # which is correct — this login only ever runs analyst queries.
                "TYPE = LEGACY_SERVICE "
                "MUST_CHANGE_PASSWORD = FALSE",
                quiet=True,
            )
            print(f"  CREATE USER {analyst_user} (password not echoed)")

        # Properties are safe to reapply; the password is not. Setting it to
        # the value it already holds trips Snowflake's reuse policy with
        # PRIOR_USE, which means "already correct" — not a failure.
        run(
            cursor,
            f"ALTER USER {analyst_user} SET "
            f"DEFAULT_ROLE = {role} DEFAULT_WAREHOUSE = {warehouse} "
            f"DEFAULT_NAMESPACE = {database}.{schema} "
            "TYPE = LEGACY_SERVICE "
            f"STATEMENT_TIMEOUT_IN_SECONDS = {STATEMENT_TIMEOUT_SECONDS}",
        )
        if user_exists:
            try:
                cursor.execute(f"ALTER USER {analyst_user} SET PASSWORD = '{analyst_password}'")
                print("  password updated")
            except snowflake.connector.errors.ProgrammingError as error:
                if "PRIOR_USE" not in str(error):
                    raise
                print("  password already matches .env")

        run(cursor, f"GRANT ROLE {role} TO USER {analyst_user}")

        if not args.skip_data:
            print("\ndataset")
            run(cursor, f"USE DATABASE {database}")
            run(cursor, f"USE SCHEMA {schema}")
            for path in (SNOWFLAKE_DATA / "schema.sql", SEED):
                sql = path.read_text()
                print(f"  applying {path.name} ({len(sql):,} bytes) ...", end=" ", flush=True)
                for _ in connection.execute_string(sql):
                    pass
                print("ok")
            # New objects need the grant re-applied; FUTURE covers later ones.
            for on in ("TABLES", "VIEWS"):
                run(
                    cursor,
                    f"GRANT SELECT ON ALL {on} IN SCHEMA {database}.{schema} TO ROLE {role}",
                    quiet=True,
                )

        print("\nverification")
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_schema = '{schema}' AND table_type = 'BASE TABLE'"
        )
        print(f"  tables  : {cursor.fetchone()[0]}")
        cursor.execute(
            f"SELECT COUNT(*) FROM information_schema.views WHERE table_schema = '{schema}'"
        )
        print(f"  views   : {cursor.fetchone()[0]}")
        cursor.execute(f"SELECT COUNT(*) FROM {database}.{schema}.streaming_service")
        print(f"  services: {cursor.fetchone()[0]}")
        print("\nDone. Set MARKET_SOURCE=snowflake in .env to use it.")
    finally:
        cursor.close()
        connection.close()


if __name__ == "__main__":
    main()
