"""The database layer's logic that does not need a database.

Connecting, storing and the foreign-key guards are exercised by
`scripts/check_register.py` against a real MySQL — CI has no Tutor stack, and a
stubbed MySQL would prove nothing about InnoDB's actual behaviour.
"""

import pytest

from classroom_sync.db import parse_database_url


def test_parses_a_tutor_style_url():
    params = parse_database_url("mysql://classroom:secret@mysql:3306/classroom")
    assert params["host"] == "mysql"
    assert params["port"] == 3306
    assert params["user"] == "classroom"
    assert params["password"] == "secret"
    assert params["db"] == "classroom"
    assert params["charset"] == "utf8mb4"


def test_autocommit_is_off():
    """register_text writes a segmentation and its segments in one transaction; with
    autocommit on, a failure halfway would leave a text with a partial id list."""
    assert parse_database_url("mysql://u:p@h/db")["autocommit"] is False


def test_a_generated_password_survives_url_encoding():
    """Tutor generates passwords from a random alphabet, so one can contain `@` or
    `/`. Encoded in the URL, it must come back out intact or the connection fails
    with a confusing auth error."""
    params = parse_database_url("mysql://classroom:p%40ss%2Fword@mysql:3306/classroom")
    assert params["password"] == "p@ss/word"


def test_the_port_defaults_to_3306():
    assert parse_database_url("mysql://u:p@mysql/classroom")["port"] == 3306


@pytest.mark.parametrize(
    "url, reason",
    [
        ("postgres://u:p@h:5432/db", "wrong scheme"),
        ("mysql://u:p@h:3306/", "no database name"),
        ("mysql://u:p@h:3306", "no database name at all"),
    ],
)
def test_a_bad_url_fails_at_startup_not_at_first_query(url, reason):
    with pytest.raises(ValueError):
        parse_database_url(url)


def test_the_migration_splitter_handles_our_sql():
    """The init task runs this inside the container, which has no mysql client."""
    import pathlib

    from classroom_sync.migrate import statements

    sql = (pathlib.Path(__file__).resolve().parents[1] / "migrations" / "001_init.sql").read_text()
    parts = statements(sql)
    assert len(parts) == 4, f"expected four CREATE TABLEs, got {len(parts)}"
    assert all(p.lower().startswith("create table if not exists") for p in parts), \
        "every statement must be re-runnable — Tutor applies this on every init"
    assert not any("--" in p for p in parts), "comments must be stripped"
