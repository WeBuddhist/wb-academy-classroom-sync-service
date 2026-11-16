"""MySQL, for the things that must outlive a restart.

Rooms and the id lists they are checked against. **No text.** The `segments` table has
three columns and none of them holds a word; see `migrations/001_init.sql`.
"""

from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import aiomysql

from .library import TextContext

#: `on duplicate key update ... = new.col` rather than `values(col)`. MySQL 8.4
#: deprecates the VALUES() function and warns on every insert; the row alias is the
#: replacement and works from 8.0.20 on.

#: Rows per executemany when writing a segment list. A long text runs past a thousand
#: segments and MySQL's max_allowed_packet is the limit that bites; this stays far
#: under it while keeping the write to a couple of round trips.
INSERT_CHUNK = 500


@dataclass(frozen=True)
class RegisteredText:
    """What a room points at, as stored."""

    segmentation_id: str
    edition_id: str
    text_id: str
    title: str
    segment_count: int


def parse_database_url(url: str) -> dict:
    """`mysql://user:password@host:3306/classroom` -> aiomysql connect kwargs.

    Passwords are percent-decoded: a generated password may contain `@` or `/`, which
    must be encoded in the URL and decoded back out here.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("mysql", "mysql+aiomysql"):
        raise ValueError(f"DATABASE_URL must be a mysql:// url, got {parsed.scheme!r}")
    if not parsed.path.lstrip("/"):
        raise ValueError("DATABASE_URL has no database name")
    return {
        "host": parsed.hostname or "mysql",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "db": parsed.path.lstrip("/"),
        "charset": "utf8mb4",
        "autocommit": False,
    }


class Database:
    """A connection pool and the handful of queries this service makes."""

    def __init__(self, url: str) -> None:
        self._params = parse_database_url(url)
        self._pool: aiomysql.Pool | None = None

    async def connect(self, *, minsize: int = 1, maxsize: int = 10) -> None:
        self._pool = await aiomysql.create_pool(
            minsize=minsize, maxsize=maxsize, **self._params
        )

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    @property
    def pool(self) -> aiomysql.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() has not been called")
        return self._pool

    # -- registration ------------------------------------------------------

    async def get_text(self, segmentation_id: str) -> RegisteredText | None:
        """What is stored for a segmentation, or None if it was never registered."""
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                select s.id, s.edition_id, s.text_id, s.title,
                       (select count(*) from segments
                         where segmentation_id = s.id) as segment_count
                  from segmentations s
                 where s.id = %s
                """,
                (segmentation_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return RegisteredText(*row)

    async def register_text(
        self,
        context: TextContext,
        segment_ids: list[str],
        *,
        title: str,
        language: str | None = None,
    ) -> RegisteredText:
        """Store a segmentation and its id list, once.

        **Idempotent, and deliberately does not re-sync.** If the segmentation already
        has segments, this returns what is stored and touches nothing.

        That is not laziness. `rooms.current_segment_id` carries a foreign key onto
        `segments`, with no ON DELETE, so deleting a segment that a live room sits on
        is refused by the database — a re-sync during a class would fail halfway. Ids
        only change when the pipeline re-segments a text, which is not something to
        discover mid-lesson. A deliberate re-sync belongs in its own operation, run
        when no room is live.
        """
        existing = await self.get_text(context.segmentation_id)
        if existing is not None and existing.segment_count > 0:
            return existing

        async with self.pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    # Two teachers may register the same text at the same moment; the
                    # second must not fail on the primary key.
                    await cur.execute(
                        """
                        insert into segmentations
                               (id, edition_id, text_id, title, language)
                        values (%s, %s, %s, %s, %s) as new
                        on duplicate key update
                               title = new.title,
                               language = new.language,
                               synced_at = current_timestamp
                        """,
                        (
                            context.segmentation_id,
                            context.edition_id,
                            context.text_id,
                            title,
                            language,
                        ),
                    )
                    rows = [
                        (segment_id, context.segmentation_id, index)
                        for index, segment_id in enumerate(segment_ids)
                    ]
                    for start in range(0, len(rows), INSERT_CHUNK):
                        await cur.executemany(
                            """
                            insert into segments (id, segmentation_id, idx)
                            values (%s, %s, %s) as new
                            on duplicate key update idx = new.idx
                            """,
                            rows[start : start + INSERT_CHUNK],
                        )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

        stored = await self.get_text(context.segmentation_id)
        assert stored is not None  # just written, in a committed transaction
        return stored

    async def has_segment(self, segmentation_id: str, segment_id: str) -> bool:
        """Is this segment part of this text?

        The only question this service asks of its id lists, and the reason they are
        stored at all — the alternative puts the library on the live path of every
        teacher tap.
        """
        async with self.pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "select 1 from segments where segmentation_id = %s and id = %s",
                (segmentation_id, segment_id),
            )
            return await cur.fetchone() is not None
