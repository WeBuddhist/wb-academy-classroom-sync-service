"""Rooms: creating them, finding them, and who is in them.

The teacher is the account that created the class. The teacher-only interface in the
client is a convenience; what actually stops a student driving a class is `is_host`,
checked here on the server against the verified login, never against anything the client
sends.
"""

import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import date

import aiomysql

from .auth import User

#: Read off a whiteboard from the back of a room, so I, L, O, 0 and 1 are excluded.
#: S and 5 are NOT — the original contract claimed they were, and a test caught it.
#: Left as it is: changing the alphabet mid-port would make a port bug and a design
#: change indistinguishable. Revisit if a real class finds it confusing.
ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ROOM_CODE_LENGTH = 6

#: Six characters from a 31-character alphabet collide in practice, so creation
#: retries rather than checking first — see `create_room`.
CODE_ATTEMPTS = 10

#: MySQL's duplicate-entry error. Catching the code, not the message, which is
#: localized and has changed between versions.
ER_DUP_ENTRY = 1062


class RoomError(Exception):
    """Something was wrong with the request, not with the server."""


@dataclass(frozen=True)
class CreatedRoom:
    """What creating a class returns: the code to write on the board."""

    code: str


@dataclass(frozen=True)
class Room:
    id: str
    code: str
    segmentation_id: str
    edition_id: str
    title: str
    current_segment_id: str | None
    is_live: bool
    #: The edition shown beneath the source, or None for a source-only room.
    translation_edition_id: str | None = None
    #: What the class is called in a student's list; None falls back to the text's title.
    name: str | None = None
    #: The creating account's id, i.e. the teacher. None for a room made before accounts
    #: existed: nobody owns those, so nobody can manage them.
    host_user_id: str | None = None
    #: The teacher ended today's session and has not come back (see `set_session_ended`).
    session_ended: bool = False
    #: The first and last day the class is held, when the teacher has said.
    starts_on: date | None = None
    ends_on: date | None = None


def new_room_code() -> str:
    """`secrets`, not `random`. A guessable code is a stranger in the class."""
    return "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))


def password_matches(presented: str, expected: str) -> bool:
    """Constant time, for the shared password that permits creating a room."""
    return hmac.compare_digest(presented.encode(), expected.encode())


def normalise_code(code: str) -> str:
    """A student typing `abc123` must find room `ABC123`."""
    return code.strip().upper()


async def create_room(
    pool: aiomysql.Pool,
    segmentation_id: str,
    translation_edition_id: str | None = None,
    *,
    host: User | None = None,
    name: str | None = None,
    starts_on: date | None = None,
    ends_on: date | None = None,
) -> CreatedRoom:
    """Open a room on an already-registered text.

    Does **not** look for a free code first. Checking then inserting is a race, and at
    six characters collisions are real rather than theoretical. Insert, catch the
    unique-key violation, try again.
    """
    for _ in range(CODE_ATTEMPTS):
        code = new_room_code()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    room_id = str(uuid.uuid4())
                    await cur.execute(
                        """
                        insert into rooms
                               (id, code, segmentation_id, name, host_user_id,
                                starts_on, ends_on)
                        values (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            room_id, code, segmentation_id,
                            (name or "").strip() or None,
                            host.id if host else None,
                            starts_on, ends_on,
                        ),
                    )
                    if host:
                        # The creator is the class's first member, in the same
                        # transaction: a class never exists without its teacher in it.
                        await cur.execute(
                            """
                            insert into room_members
                                   (room_id, user_id, username, name, role)
                            values (%s, %s, %s, %s, 'teacher')
                            """,
                            (room_id, host.id, host.username, host.name),
                        )
                    if translation_edition_id:
                        # Same transaction: a room never exists half-configured.
                        await cur.execute(
                            """
                            insert into room_translations (room_id, edition_id)
                            values (%s, %s)
                            """,
                            (room_id, translation_edition_id),
                        )
                await conn.commit()
            except aiomysql.IntegrityError as error:
                await conn.rollback()
                if error.args and error.args[0] == ER_DUP_ENTRY:
                    continue  # that code was taken; try another
                raise
            else:
                return CreatedRoom(code=code)

    raise RoomError("Could not allocate a free room code")


_ROOM_SELECT = """
    select r.id, r.code, r.segmentation_id, s.edition_id, s.title,
           r.current_segment_id, r.is_live, t.edition_id,
           r.name, r.host_user_id, r.session_ended, r.starts_on, r.ends_on
      from rooms r
      join segmentations s on s.id = r.segmentation_id
      left join room_translations t on t.room_id = r.id
"""


async def get_room(pool: aiomysql.Pool, code: str) -> Room | None:
    """Find a room by the code on the whiteboard, matched case-insensitively."""
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(_ROOM_SELECT + " where r.code = %s", (normalise_code(code),))
        row = await cur.fetchone()
    return None if row is None else Room(*row)


async def get_room_by_id(pool: aiomysql.Pool, room_id: str) -> Room | None:
    """Find a room by its id. What a live connection holds, since a room's code can be
    changed under it but its id never is."""
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(_ROOM_SELECT + " where r.id = %s", (room_id,))
        row = await cur.fetchone()
    return None if row is None else Room(*row)


async def set_current_segment(
    pool: aiomysql.Pool, room: Room, segment_id: str
) -> bool:
    """Move the room to a line, and record that it happened.

    Returns False if the segment is not part of this room's text. **Checked here, not
    left to the foreign key**: a bad id would otherwise surface as a database error in
    the middle of a class, where it reads as the server breaking rather than as a stale
    client sending nonsense.

    The update and the log row are one transaction. The log is what a session replay
    will be built from, and a history with gaps where the writes half-failed is worse
    than no history.
    """
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "select 1 from segments where segmentation_id = %s and id = %s",
                    (room.segmentation_id, segment_id),
                )
                if await cur.fetchone() is None:
                    await conn.rollback()
                    return False

                await cur.execute(
                    "update rooms set current_segment_id = %s where id = %s",
                    (segment_id, room.id),
                )
                await cur.execute(
                    "insert into room_log (room_id, segment_id) values (%s, %s)",
                    (room.id, segment_id),
                )
            await conn.commit()
            return True
        except Exception:
            await conn.rollback()
            raise


async def set_class_dates(
    pool: aiomysql.Pool, room_id: str, starts_on: date | None, ends_on: date | None
) -> None:
    """Set (or, with None, clear) the days the class is held."""
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update rooms set starts_on = %s, ends_on = %s where id = %s",
                    (starts_on, ends_on, room_id),
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def set_session_ended(pool: aiomysql.Pool, room_id: str, ended: bool) -> None:
    """Record that today's session has ended (or, with False, that the teacher is back).

    Stored, not remembered in a process: a student who joins after it ended must be told,
    whichever worker they reach and however long ago the service last restarted.
    """
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update rooms set session_ended = %s where id = %s", (ended, room_id)
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def close_room(pool: aiomysql.Pool, room: Room) -> None:
    """End the class.

    Must actually set `is_live = false`. A room that still works weeks later is one a
    stranger can wander into, with the teacher none the wiser. The client listening for
    `room:closed` while the server never sent it was a real bug; so was the room
    staying open after it.
    """
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    update rooms
                       set is_live = false, closed_at = current_timestamp
                     where id = %s
                    """,
                    (room.id,),
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


# ---------------------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------------------

ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"


@dataclass(frozen=True)
class Member:
    user_id: str
    username: str
    name: str
    role: str
    joined_at: object
    last_seen_at: object
    #: Set when the student left the class; they can rejoin without the code. None while in it.
    left_at: object = None


@dataclass(frozen=True)
class ClassSummary:
    """One entry in a person's list of classes."""

    code: str
    #: The class's name, falling back to the text's title.
    name: str
    title: str
    role: str
    member_count: int
    last_seen_at: object
    #: The days the class is held, when set.
    starts_on: date | None = None
    ends_on: date | None = None
    #: The student left this class but it stays in their list, to rejoin.
    left: bool = False


def is_host(room: Room, user: User) -> bool:
    """Whether this account teaches this room. The only teacher test for the REST side.

    A room with no recorded owner (made before accounts) has no teacher at all, so this is
    False for everyone: `None == user.id` is never true, but it is spelt out rather than
    left to that.
    """
    return room.host_user_id is not None and room.host_user_id == user.id


async def add_member(pool: aiomysql.Pool, room: Room, user: User) -> str:
    """Record that this account is in this class, and return their role in it.

    Idempotent: joining again only refreshes the name shown and when they were last seen.
    A role is never changed here; the creator stays the teacher however many times they
    join, and a student cannot become one by joining.
    """
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    insert into room_members (room_id, user_id, username, name, role)
                    values (%s, %s, %s, %s, %s) as new
                    on duplicate key update
                      username = new.username,
                      name = new.name,
                      left_at = null,
                      last_seen_at = current_timestamp
                    """,
                    (
                        room.id, user.id, user.username, user.name,
                        ROLE_TEACHER if is_host(room, user) else ROLE_STUDENT,
                    ),
                )
                await cur.execute(
                    "select role from room_members where room_id = %s and user_id = %s",
                    (room.id, user.id),
                )
                (role,) = await cur.fetchone()
            await conn.commit()
            return role
        except Exception:
            await conn.rollback()
            raise


async def touch_member(pool: aiomysql.Pool, room_id: str, user_id: str) -> None:
    """Note that a member was just here. Updates an existing member only: it must never
    bring back someone the teacher removed, which an upsert would."""
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update room_members set last_seen_at = current_timestamp "
                    "where room_id = %s and user_id = %s",
                    (room_id, user_id),
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def get_member(pool: aiomysql.Pool, room_id: str, user_id: str) -> Member | None:
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select user_id, username, name, role, joined_at, last_seen_at, left_at
              from room_members where room_id = %s and user_id = %s
            """,
            (room_id, user_id),
        )
        row = await cur.fetchone()
    return None if row is None else Member(*row)


async def list_members(pool: aiomysql.Pool, room_id: str) -> list[Member]:
    """Everyone in a class (not those who left), the teacher first, then in the order they joined."""
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select user_id, username, name, role, joined_at, last_seen_at, left_at
              from room_members
             where room_id = %s and left_at is null
             order by (role = 'teacher') desc, joined_at, username
            """,
            (room_id,),
        )
        return [Member(*row) for row in await cur.fetchall()]


async def remove_member(pool: aiomysql.Pool, room_id: str, user_id: str) -> bool:
    """Take a student out of a class. The teacher cannot be removed: the class would have
    nobody able to run it. Returns whether anyone was removed."""
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                removed = await cur.execute(
                    "delete from room_members where room_id = %s and user_id = %s "
                    "and role <> 'teacher'",
                    (room_id, user_id),
                )
            await conn.commit()
            return bool(removed)
        except Exception:
            await conn.rollback()
            raise


async def leave_member(pool: aiomysql.Pool, room_id: str, user_id: str) -> bool:
    """A student steps out of a class without losing their place in it.

    Stamps `left_at` instead of deleting the row, so the class stays in their list and
    rejoining needs no code. The teacher cannot leave. Returns whether anything changed: False
    for someone not in the class, or who had already left.
    """
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                changed = await cur.execute(
                    "update room_members set left_at = current_timestamp "
                    "where room_id = %s and user_id = %s and role <> 'teacher' "
                    "and left_at is null",
                    (room_id, user_id),
                )
            await conn.commit()
            return bool(changed)
        except Exception:
            await conn.rollback()
            raise


async def classes_for(pool: aiomysql.Pool, user_id: str) -> list[ClassSummary]:
    """The classes an account is in that have not been archived, most recent first.

    What the code screen shows, so "enter the code" happens once.
    """
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select r.code,
                   coalesce(r.name, s.title),
                   s.title,
                   m.role,
                   (select count(*) from room_members c
                     where c.room_id = r.id and c.left_at is null),
                   m.last_seen_at,
                   r.starts_on,
                   r.ends_on,
                   m.left_at is not null
              from room_members m
              join rooms r on r.id = m.room_id
              join segmentations s on s.id = r.segmentation_id
             where m.user_id = %s and r.is_live
             order by m.last_seen_at desc
            """,
            (user_id,),
        )
        return [ClassSummary(*row[:-1], left=bool(row[-1])) for row in await cur.fetchall()]


async def change_code(pool: aiomysql.Pool, room: Room) -> str:
    """Give the class a new code. The old one stops working at once.

    Members are recorded against the room, not the code, so nobody already in is affected.
    It is what a teacher does when a code has travelled further than it should: anyone who
    does not hold the new one cannot join. It does **not** remove anyone already in; that
    is `remove_member`.

    Retries on a duplicate, for the same reason `create_room` does.
    """
    for _ in range(CODE_ATTEMPTS):
        code = new_room_code()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "update rooms set code = %s where id = %s", (code, room.id)
                    )
                await conn.commit()
            except aiomysql.IntegrityError as error:
                await conn.rollback()
                if error.args and error.args[0] == ER_DUP_ENTRY:
                    continue
                raise
            else:
                return code
    raise RoomError("Could not allocate a free room code")
