"""Apply the SQL migrations. Run as `python -m classroom_sync.migrate`.

Exists because the image has no mysql client — it is `python:3.12-slim` — and Tutor's
init task has to create the schema from inside this container, where the .sql files
live. Keeping a mysql client in the image just to run four CREATE TABLEs would be a
bigger dependency than this module.

Safe to run repeatedly: each file is applied once and recorded in `schema_migrations`,
so Tutor can run this on every `init`. Files up to 002 predate that table and are also
written with `create table if not exists`, so applying them once more is harmless.
"""

import asyncio
import pathlib
import re
import sys
import warnings

import aiomysql

from .config import config
from .db import parse_database_url

MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "migrations"


def statements(sql: str) -> list[str]:
    """Split a migration file into statements.

    Line comments are stripped first, then the text is split on semicolons. That is
    enough **for these files and only these files**: they contain no semicolon inside
    a string literal, no stored procedure and no DELIMITER block. A migration that
    needs any of those needs a real parser, not this.
    """
    without_comments = re.sub(r"--[^\n]*", "", sql)
    return [s.strip() for s in without_comments.split(";") if s.strip()]


async def apply(directory: pathlib.Path = MIGRATIONS) -> int:
    if not config.database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    files = sorted(directory.glob("*.sql"))
    if not files:
        print(f"No .sql files in {directory}", file=sys.stderr)
        return 2

    params = parse_database_url(config.database_url)
    conn = await aiomysql.connect(**params)
    try:
        # Each file is applied **once**, and recorded. Until now every file was written to
        # be safe to re-run (`create table if not exists`) and re-run on every init, which
        # cannot express "add a column": MySQL has no `add column if not exists`.
        # Files already applied before this table existed are simply run once more and
        # recorded; they are idempotent, so that is safe.
        async with conn.cursor() as cur:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", message=r".*already exists.*")
                await cur.execute(
                    """
                    create table if not exists schema_migrations (
                      name       varchar(255) not null,
                      applied_at timestamp    not null default current_timestamp,
                      primary key (name)
                    ) engine=InnoDB default charset=utf8mb4
                    """
                )
            await cur.execute("select name from schema_migrations")
            applied = {row[0] for row in await cur.fetchall()}
        await conn.commit()

        for path in files:
            if path.name in applied:
                print(f"{path.name}: already applied")
                continue
            parts = statements(path.read_text())
            async with conn.cursor() as cur:
                for statement in parts:
                    # MySQL raises a warning for `create table if not exists` whose table
                    # is already there: the normal case for the first two files.
                    with warnings.catch_warnings():
                        warnings.filterwarnings(
                            "ignore", message=r".*already exists.*"
                        )
                        await cur.execute(statement)
                await cur.execute(
                    "insert into schema_migrations (name) values (%s)", (path.name,)
                )
            await conn.commit()
            print(f"{path.name}: {len(parts)} statement(s) applied")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(apply()))
