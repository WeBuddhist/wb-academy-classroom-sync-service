"""Rooms: creating them, finding them, and proving you are the teacher.

The security boundary of this whole service lives here. The teacher-only interface in
the client is a convenience; what actually stops a student driving a class is the token
comparison below.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

import aiomysql

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
    """The teacher's two secrets. `host_token` is shown once and never stored."""

    code: str
    host_token: str


@dataclass(frozen=True)
class Room:
    id: str
    code: str
    segmentation_id: str
    edition_id: str
    title: str
    current_segment_id: str | None
    is_live: bool


def new_room_code() -> str:
    """`secrets`, not `random`. A guessable code is a stranger in the class."""
    return "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))


def new_host_token() -> str:
    """The teacher's recovery code. Also what authorises every move they make."""
    return secrets.token_urlsafe(24)


def hash_token(token: str) -> str:
    """sha256, hex. Only the hash is stored, so a database leak cannot drive a class."""
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_match(presented: str, stored_hash: str) -> bool:
    """Constant time. `==` on a secret leaks its prefix through timing."""
    return hmac.compare_digest(hash_token(presented), stored_hash)


def password_matches(presented: str, expected: str) -> bool:
    """Constant time, for the shared password that permits creating a room."""
    return hmac.compare_digest(presented.encode(), expected.encode())


def normalise_code(code: str) -> str:
    """A student typing `abc123` must find room `ABC123`."""
    return code.strip().upper()


async def create_room(pool: aiomysql.Pool, segmentation_id: str) -> CreatedRoom:
    """Open a room on an already-registered text.

    Does **not** look for a free code first. Checking then inserting is a race, and at
    six characters collisions are real rather than theoretical. Insert, catch the
    unique-key violation, try again.
    """
    host_token = new_host_token()
    token_hash = hash_token(host_token)

    for _ in range(CODE_ATTEMPTS):
        code = new_room_code()
        async with pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        insert into rooms
                               (id, code, segmentation_id, host_token_hash)
                        values (%s, %s, %s, %s)
                        """,
                        (str(uuid.uuid4()), code, segmentation_id, token_hash),
                    )
                await conn.commit()
            except aiomysql.IntegrityError as error:
                await conn.rollback()
                if error.args and error.args[0] == ER_DUP_ENTRY:
                    continue  # that code was taken; try another
                raise
            else:
                return CreatedRoom(code=code, host_token=host_token)

    raise RoomError("Could not allocate a free room code")


async def get_room(pool: aiomysql.Pool, code: str) -> Room | None:
    """Find a room by the code on the whiteboard, matched case-insensitively."""
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            select r.id, r.code, r.segmentation_id, s.edition_id, s.title,
                   r.current_segment_id, r.is_live
              from rooms r
              join segmentations s on s.id = r.segmentation_id
             where r.code = %s
            """,
            (normalise_code(code),),
        )
        row = await cur.fetchone()
    return None if row is None else Room(*row)


#: What `verify_host` found. Three outcomes, not two: a teacher coming back to a class
#: that has ended needs to hear *that*, not "wrong code" and not "you are the host".
HOST_OK = "ok"
HOST_REJECTED = "rejected"
HOST_ROOM_ENDED = "ended"


async def verify_host(pool: aiomysql.Pool, code: str, host_token: str) -> str:
    """Is this the teacher's token for this room, and is the room still live?

    Exposed as its own route so a teacher restoring a room on a second device learns
    the code is wrong **before** they start teaching. Without it they are told they are
    teaching and then find every tap refused, mid-class, with nothing they can do.
    A real bug, found and fixed; do not reintroduce it.

    `is_live` is checked **after** the token, deliberately. Checking first would let
    anyone probe which room codes exist and which classes have ended, without holding
    any credential.
    """
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "select host_token_hash, is_live from rooms where code = %s",
            (normalise_code(code),),
        )
        row = await cur.fetchone()
    if row is None:
        # Compare anyway, against a hash of nothing, so a room that does not exist and
        # a wrong token take the same time to answer. The result is discarded.
        tokens_match(host_token, hash_token(""))
        return HOST_REJECTED
    stored_hash, is_live = row
    if not tokens_match(host_token, stored_hash):
        return HOST_REJECTED
    return HOST_OK if is_live else HOST_ROOM_ENDED


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
