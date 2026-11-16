"""The live messages. This is what the service is for.

Five events, ported from the Node server's `socket.ts`. The field names on the wire are
**camelCase** — `segmentId`, `hostToken`, `displayName`, `participantCount`. Writing
`segment_id` here is the single most likely way to break this service, and it breaks it
quietly: the server runs, the client runs, and thirty screens do nothing.
See docs/socket-contract.md.
"""

import logging

import socketio

from .rooms import (
    close_room,
    get_room,
    set_current_segment,
    tokens_match,
)

log = logging.getLogger("classroom_sync.events")


def _room_state(room, participant_count: int) -> dict:
    """The authoritative snapshot. Sent on join, and overrides whatever the client had."""
    return {
        "code": room.code,
        "editionId": room.edition_id,
        "segmentationId": room.segmentation_id,
        "title": room.title,
        "currentSegmentId": room.current_segment_id,
        "participantCount": participant_count,
    }


def attach(sio: socketio.AsyncServer, get_pool) -> None:
    """Wire the handlers onto a server. `get_pool` is a callable so the pool can be
    opened at startup rather than at import."""

    def participants(code: str, *, excluding: str | None = None) -> int:
        """How many sockets are in this room.

        `excluding` is for disconnect, where the leaving socket may still be listed
        depending on when the handler runs relative to the manager's cleanup. Counting
        it would leave the remaining clients showing one person too many — which fails
        silently, since nothing errors when a count is wrong.
        """
        try:
            sids = sio.manager.get_participants("/", code)
        except KeyError:
            return 0
        return sum(1 for sid, _ in sids if sid != excluding)

    async def _presence(code: str, *, excluding: str | None = None) -> None:
        await sio.emit("presence", {"count": participants(code, excluding=excluding)},
                       room=code)

    @sio.event
    async def connect(sid, environ, auth=None):
        # Nothing to do until they join a room. Connecting is not joining.
        pass

    @sio.on("room:join")
    async def room_join(sid, payload):
        try:
            payload = payload or {}
            code = str(payload.get("code") or "")
            display_name = str(payload.get("displayName") or "")

            room = await get_room(get_pool(), code)
            if room is None:
                return {"ok": False, "error": "No room with that code."}
            if not room.is_live:
                return {"ok": False, "error": "That class has ended."}

            await sio.enter_room(sid, room.code)
            await sio.save_session(sid, {"code": room.code})

            count = participants(room.code)
            # Presence goes to everyone *after* the joiner's own answer is built, so
            # the two agree. The joiner is already in the room, so they are counted.
            await _presence(room.code)
            log.info(
                "%s joined %s (%d present)", display_name or "someone", room.code, count
            )
            return {"ok": True, "state": _room_state(room, count)}
        except Exception:
            log.exception("room:join failed")
            return {"ok": False, "error": "Could not join the room."}

    @sio.on("teach:goto")
    async def teach_goto(sid, payload):
        try:
            payload = payload or {}
            segment_id = str(payload.get("segmentId") or "")
            host_token = str(payload.get("hostToken") or "")

            session = await sio.get_session(sid)
            code = (session or {}).get("code")
            if not code:
                return {"ok": False, "error": "Join a room first."}

            room = await get_room(get_pool(), code)
            if room is None:
                return {"ok": False, "error": "No room with that code."}
            # Checked here as well as on join. Without it a teacher still connected
            # after room:close keeps driving an ended class: the broadcast goes out,
            # the position moves and the log grows, on a room that refuses newcomers
            # and reports itself as over.
            if not room.is_live:
                return {"ok": False, "error": "That class has ended."}

            # The authority check. A student can emit this event; it gets no further.
            # Constant-time, and against the stored hash — see rooms.tokens_match.
            if not host_token or not tokens_match(host_token, await _token_hash(code)):
                return {"ok": False, "error": "Not the host of this room."}

            moved = await set_current_segment(get_pool(), room, segment_id)
            if not moved:
                return {"ok": False, "error": "That segment is not part of this text."}

            # Everyone, the sender included. If the teacher's own view were updated
            # locally and the students' by broadcast, the two paths could silently
            # drift apart and the teacher would be the last to know.
            await sio.emit("teach:goto", {"segmentId": segment_id}, room=room.code)
            return {"ok": True}
        except Exception:
            log.exception("teach:goto failed")
            return {"ok": False, "error": "Could not move the room."}

    @sio.on("room:close")
    async def room_close(sid, payload):
        try:
            payload = payload or {}
            host_token = str(payload.get("hostToken") or "")

            session = await sio.get_session(sid)
            code = (session or {}).get("code")
            if not code:
                return {"ok": False, "error": "Join a room first."}

            room = await get_room(get_pool(), code)
            if room is None:
                return {"ok": False, "error": "No room with that code."}
            if not host_token or not tokens_match(host_token, await _token_hash(code)):
                return {"ok": False, "error": "Not the host of this room."}
            # Already over. Answer ok — ending an ended class is what the caller
            # wanted — but do not broadcast again: a client that treats room:closed
            # as a one-shot transition would handle it twice.
            if not room.is_live:
                return {"ok": True}

            await close_room(get_pool(), room)
            await sio.emit("room:closed", room=room.code)
            log.info("%s closed by the host", room.code)
            return {"ok": True}
        except Exception:
            log.exception("room:close failed")
            return {"ok": False, "error": "Could not end the class."}

    @sio.event
    async def disconnect(sid, reason=None):
        """Presence must drop when someone closes their browser.

        Easy to leave out, because nothing fails loudly when you do — the room just
        reports more people than are in it, forever.
        """
        try:
            session = await sio.get_session(sid)
            code = (session or {}).get("code")
            if code:
                await _presence(code, excluding=sid)
        except Exception:
            log.exception("disconnect handling failed")

    async def _token_hash(code: str) -> str:
        """The room's stored hash, for the constant-time comparison above."""
        pool = get_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(
                "select host_token_hash from rooms where code = %s", (code.upper(),)
            )
            row = await cur.fetchone()
        return row[0] if row else ""
