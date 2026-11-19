"""A student's private notes and highlights.

The rules live here, in one place, because they are what stands between one student and
another's reading: every query is for the **owner** and the class, never for a note id
alone, so guessing an id reaches nothing.

Only **students** may write notes, for now. That is one check, `NOTES_ROLE`, so allowing
teachers later is a single line.
"""

import uuid
from dataclasses import dataclass

import aiomysql

#: Who may keep notes. Teachers are left out for now; widen this to allow them.
NOTES_ROLE = "student"

#: A small set of highlight colours, named rather than free-form so a bad value cannot be
#: stored and the reader can style each one.
COLORS = ("yellow", "green", "blue", "pink")

#: Which text on a line a note is on.
LAYERS = ("source", "translation")

#: Limits. Generous for real use, tight enough that nobody fills the database.
MAX_QUOTE = 2000
MAX_CONTEXT = 200
MAX_BODY = 4000
MAX_OFFSET = 200_000
MAX_NOTES_PER_STUDENT = 5000


@dataclass(frozen=True)
class Note:
    id: str
    segment_id: str
    layer: str
    start: int
    end: int
    quote: str
    prefix: str
    suffix: str
    color: str
    body: str | None
    created_at: object
    updated_at: object


_COLUMNS = (
    "id, segment_id, layer, start_offset, end_offset, quote, prefix, suffix, "
    "color, body, created_at, updated_at"
)


async def list_notes(pool: aiomysql.Pool, room_id: str, user_id: str) -> list[Note]:
    """Everything this student has marked in this class, in no particular order: the
    reader puts them in reading order, which only it knows."""
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            f"select {_COLUMNS} from room_notes where room_id = %s and user_id = %s "
            "order by created_at",
            (room_id, user_id),
        )
        return [Note(*row) for row in await cur.fetchall()]


async def create_note(
    pool: aiomysql.Pool,
    room_id: str,
    user_id: str,
    *,
    segment_id: str,
    layer: str,
    start: int,
    end: int,
    quote: str,
    prefix: str,
    suffix: str,
    color: str,
    body: str | None,
) -> Note | None:
    """Keep a new note, or return None if the student is already at their limit.

    The limit is checked **inside** the insert (a conditional `insert … select`), not by a
    separate count beforehand: with two requests in flight both would pass a count, and the
    cap would be a suggestion.
    """
    note_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                inserted = await cur.execute(
                    """
                    insert into room_notes
                           (id, room_id, user_id, segment_id, layer, start_offset,
                            end_offset, quote, prefix, suffix, color, body)
                    select %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                      from dual
                     where (select count(*) from room_notes
                             where room_id = %s and user_id = %s) < %s
                    """,
                    (note_id, room_id, user_id, segment_id, layer, start, end,
                     quote, prefix, suffix, color, body,
                     room_id, user_id, MAX_NOTES_PER_STUDENT),
                )
                if not inserted:
                    await conn.rollback()
                    return None
                await cur.execute(
                    f"select {_COLUMNS} from room_notes where id = %s", (note_id,)
                )
                row = await cur.fetchone()
            await conn.commit()
            return Note(*row)
        except Exception:
            await conn.rollback()
            raise


async def update_note(
    pool: aiomysql.Pool,
    room_id: str,
    user_id: str,
    note_id: str,
    *,
    color: str | None,
    set_body: bool,
    body: str | None,
) -> Note | None:
    """Change a note's colour and/or text. Where it is anchored never changes: a note on
    different words is a different note.

    Matches on the note, the class **and the owner** together; anything else is "not
    found", which is also all anyone else's id will ever say.
    """
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                sets, args = [], []
                if color is not None:
                    sets.append("color = %s")
                    args.append(color)
                if set_body:
                    # `body` None clears the words, leaving a plain highlight.
                    sets.append("body = %s")
                    args.append(body)
                if sets:
                    await cur.execute(
                        f"update room_notes set {', '.join(sets)} "
                        "where id = %s and room_id = %s and user_id = %s",
                        (*args, note_id, room_id, user_id),
                    )
                await cur.execute(
                    f"select {_COLUMNS} from room_notes "
                    "where id = %s and room_id = %s and user_id = %s",
                    (note_id, room_id, user_id),
                )
                row = await cur.fetchone()
            await conn.commit()
            return None if row is None else Note(*row)
        except Exception:
            await conn.rollback()
            raise


async def delete_note(
    pool: aiomysql.Pool, room_id: str, user_id: str, note_id: str
) -> bool:
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                removed = await cur.execute(
                    "delete from room_notes where id = %s and room_id = %s and user_id = %s",
                    (note_id, room_id, user_id),
                )
            await conn.commit()
            return bool(removed)
        except Exception:
            await conn.rollback()
            raise
