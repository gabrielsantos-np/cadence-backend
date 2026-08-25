"""The read-only guard.

Defence in depth: the `analyst_ro` role is the real boundary. This exists so a
bad query fails with a message the model can act on, and so multi-statement
injection never reaches the driver.
"""

import pytest

from cadence_backend.db import assert_read_only
from cadence_backend.db.readonly import render


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select service_name from streaming_service",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "  select 1  ",
        "SELECT 1;",  # a single trailing semicolon is stripped, not rejected
    ],
)
def test_allows_single_reads(sql: str) -> None:
    assert_read_only(sql)


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("", "Empty query."),
        ("   ", "Empty query."),
        ("SELECT 1; DROP TABLE x", "Only one statement per query. Remove the extra `;`."),
        ("INSERT INTO x VALUES (1)", "Only SELECT and WITH queries are allowed."),
        ("UPDATE x SET y = 1", "Only SELECT and WITH queries are allowed."),
        ("DELETE FROM x", "Only SELECT and WITH queries are allowed."),
        ("DROP TABLE x", "Only SELECT and WITH queries are allowed."),
        ("GRANT ALL ON x TO y", "Only SELECT and WITH queries are allowed."),
        ("SET ROLE postgres", "Only SELECT and WITH queries are allowed."),
    ],
)
def test_rejects_writes_and_injection(sql: str, reason: str) -> None:
    with pytest.raises(ValueError, match=reason.replace("`", "").split(".")[0]):
        assert_read_only(sql)


def test_rejects_a_write_hidden_after_a_read() -> None:
    """A CTE that starts with SELECT must still not smuggle in DDL."""
    with pytest.raises(ValueError):
        assert_read_only("SELECT 1 FROM x WHERE y = (CREATE TABLE z)")


def test_render_stringifies_every_cell_type() -> None:
    from datetime import date
    from decimal import Decimal

    assert render(None) == "NULL"
    assert render(date(2026, 8, 25)) == "2026-08-25"
    assert render(Decimal("5.15")) == "5.15"
    assert render({"a": 1}) == '{"a": 1}'
    assert render(42) == "42"


@pytest.mark.parametrize(
    "sql",
    [
        "USE ROLE ACCOUNTADMIN",
        "MERGE INTO x USING y ON x.id = y.id WHEN MATCHED THEN UPDATE SET a = 1",
        "PUT file:///tmp/x @stage",
        "UNDROP TABLE streaming_service",
        "EXECUTE IMMEDIATE 'drop table x'",
    ],
)
def test_rejects_snowflake_only_escalation(sql: str) -> None:
    """Snowflake has no read-only transaction, and its SQL can switch role.

    The dedicated least-privilege login is what actually prevents escalation;
    this keeps the attempt from reaching the driver at all.
    """
    with pytest.raises(ValueError):
        assert_read_only(sql)


def test_select_referencing_a_use_like_column_is_still_allowed() -> None:
    """The keyword list must not reject ordinary column names."""
    assert_read_only("SELECT usage_hours, listed_price FROM t")
    assert_read_only("WITH used AS (SELECT 1) SELECT * FROM used")
