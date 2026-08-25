"""Prove the Snowflake read-only boundary actually holds.

Snowflake has no `default_transaction_read_only` and no read-only transaction,
so "the analyst cannot write" is a claim about grants alone. This checks the
claim rather than trusting it:

    make snowflake-check

Every line marked MUST FAIL is an escalation the analyst could attempt through
generated SQL. A PASS means Snowflake refused it — not that our regex caught it
first, which is why the checks run the statements rather than the validator.
"""

import sys

import snowflake.connector

from cadence_backend.core.config import get_settings


def attempt(cursor, sql: str) -> str:
    try:
        cursor.execute(sql)
        row = cursor.fetchone()
        return f"ALLOWED -> {row[0] if row else 'ok'}"
    except Exception as error:  # noqa: BLE001 - the message is the result
        first = str(error).strip().splitlines()[0]
        return f"refused: {first[:110]}"


def main() -> None:
    settings = get_settings()
    args = settings.snowflake_analyst_connect_args()
    database = settings.snowflake_database
    schema = settings.snowflake_schema

    print(f"connecting as {args['user']} with role {args['role']}\n")
    connection = snowflake.connector.connect(**args, login_timeout=30)
    cursor = connection.cursor()
    failures = 0
    try:
        cursor.execute("SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE()")
        user, role, warehouse = cursor.fetchone()
        print(f"  session: user={user} role={role} warehouse={warehouse}")
        cursor.execute("SHOW PARAMETERS LIKE 'STATEMENT_TIMEOUT_IN_SECONDS'")
        rows = cursor.fetchall()
        print(f"  statement timeout: {rows[0][1] if rows else 'unset'}s\n")

        print("MUST WORK")
        result = attempt(cursor, f"SELECT COUNT(*) FROM {database}.{schema}.streaming_service")
        print(f"  read market data     : {result}")
        if not result.startswith("ALLOWED"):
            failures += 1

        result = attempt(
            cursor,
            f"SELECT service_name FROM {database}.{schema}.v_revenue_per_subscriber "
            "WHERE fiscal_year = 2025 AND NOT any_line_bundled "
            "ORDER BY revenue_per_subscriber_usd DESC LIMIT 1",
        )
        print(f"  read a view          : {result}")
        if not result.startswith("ALLOWED"):
            failures += 1

        print("\nMUST FAIL")
        escalations = [
            (
                "write a row",
                f"INSERT INTO {database}.{schema}.streaming_service(service_id) VALUES (999)",
            ),
            ("update a row", f"UPDATE {database}.{schema}.streaming_service SET tier = 'x'"),
            ("delete rows", f"DELETE FROM {database}.{schema}.streaming_service"),
            ("create a table", f"CREATE TABLE {database}.{schema}.pwned (x INT)"),
            ("drop a table", f"DROP TABLE {database}.{schema}.streaming_service"),
            ("switch to admin", "USE ROLE ACCOUNTADMIN"),
            ("read account metadata", "SELECT COUNT(*) FROM SNOWFLAKE.ACCOUNT_USAGE.USERS"),
            ("create a role", "CREATE ROLE escalated"),
        ]
        for label, sql in escalations:
            result = attempt(cursor, sql)
            print(f"  {label:21}: {result}")
            if result.startswith("ALLOWED"):
                failures += 1
    finally:
        cursor.close()
        connection.close()

    print("\nauthentication")
    bad = dict(args, password="definitely-not-the-password")
    try:
        snowflake.connector.connect(**bad, login_timeout=30).close()
        print("  wrong password       : ALLOWED — authentication is not enforced")
        failures += 1
    except Exception as error:  # noqa: BLE001
        print(f"  wrong password       : refused: {str(error).splitlines()[0][:90]}")

    print()
    if failures:
        sys.exit(f"{failures} boundary check(s) FAILED — do not use this configuration.")
    print("All boundary checks passed.")


if __name__ == "__main__":
    main()
