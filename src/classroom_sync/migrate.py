"""Apply the SQL migrations. Run as `python -m classroom_sync.migrate`.

Exists because the image has no mysql client — it is `python:3.12-slim` — and Tutor's
init task has to create the schema from inside this container, where the .sql files
live. Keeping a mysql client in the image just to run four CREATE TABLEs would be a
bigger dependency than this module.

Safe to run repeatedly: `001_init.sql` is written with `create table if not exists`
throughout, which is what lets Tutor run this on every `init` rather than once.
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
        for path in files:
            parts = statements(path.read_text())
            async with conn.cursor() as cur:
                for statement in parts:
                    # MySQL raises a warning for each `create table if not exists`
                    # whose table is already there. That is the normal case — Tutor
                    # runs this on every init — and four warnings per run reads as
                    # something being wrong when nothing is.
                    with warnings.catch_warnings():
                        warnings.filterwarnings(
                            "ignore", message=r".*already exists.*"
                        )
                        await cur.execute(statement)
            await conn.commit()
            print(f"{path.name}: {len(parts)} statement(s) applied")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(apply()))
