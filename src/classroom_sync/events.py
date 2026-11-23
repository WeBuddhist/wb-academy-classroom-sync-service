"""The live messages. This is what the service is for.

Who is connected, and what they may do, comes from the **Academy login**, never from
anything a client says about itself:

- A socket is accepted only with a valid login (see `connect`).
- It may enter a room only if its account is a **member** of that class. The code is
  needed once, over HTTP, to become a member; after that the account is the credential.
- It may move the room only if its account is the room's teacher (`rooms.is_host`).

The field names on the wire are **camelCase** — `segmentId`, `participantCount`,
`teacherHere`. Writing `segment_id` here is the single most likely way to break this
service, and it breaks it quietly: the server runs, the client runs, and thirty screens
do nothing. See docs/socket-contract.md.
"""

import logging
import time
from http.cookies import SimpleCookie

import socketio
from socketio.exceptions import ConnectionRefusedError

from .auth import (
    COOKIE_HEADER_PAYLOAD,
    COOKIE_SIGNATURE,
    AuthError,
    User,
    token_from_cookies,
)
from .ratelimit import TOO_MANY, code_attempts
from .rooms import (
    get_member,
    get_room,
    get_room_by_id,
    is_host,
    set_current_segment,
    set_session_ended,
    touch_member,
)

log = logging.getLogger("classroom_sync.events")


def socket_room(room_id: str) -> str:
    """The Socket.IO room for a class.

    Keyed by the room's **id**, not its code: the teacher can change a class's code while
    people are in it, and a connection keyed by the old code would be stranded.
    """
    return f"room:{room_id}"


def _user_room(user_id: str) -> str:
    """One room per account, so the server can reach all of a person's tabs at once."""
    return f"user:{user_id}"


def _room_state(
    room, user: User, count: int, teacher_here: bool, session_ended: bool = False,
) -> dict:
    """The authoritative snapshot. Sent on join, and overrides whatever the client had."""
    return {
        "code": room.code,
        "name": room.name or room.title,
        "editionId": room.edition_id,
        "segmentationId": room.segmentation_id,
        "translationEditionId": room.translation_edition_id,
        "title": room.title,
        "currentSegmentId": room.current_segment_id,
        "participantCount": count,
        "teacherHere": teacher_here,
        "sessionEnded": session_ended,
        "role": "teacher" if is_host(room, user) else "student",
    }


def _token_from_handshake(environ: dict, auth) -> tuple[str | None, bool]:
    """The login token a connection carries, and whether it came from cookies.

    - `auth.token`, for non-browser clients and tests.
    - Otherwise the two Open edX cookies, which a browser attaches to the handshake by
      itself. A script cannot read the signature half (it is HttpOnly), so this is the
      only way a browser's socket can present its login.
    """
    if isinstance(auth, dict) and auth.get("token"):
        return str(auth["token"]), False
    jar = SimpleCookie()
    try:
        jar.load(environ.get("HTTP_COOKIE", ""))
    except Exception:
        return None, True
    cookies = {name: morsel.value for name, morsel in jar.items()}
    return token_from_cookies(cookies), True


class Live:
    """What the HTTP routes can do to the live rooms: act on a connection they do not own.

    Returned by `attach`. Routes reach it through `app.state.live`.
    """

    def __init__(
        self, sio: socketio.AsyncServer, present, told_teacher_here: dict[str, bool]
    ) -> None:
        self._sio = sio
        self._present = present
        #: What students were last told about the teacher, by class. Public so a test can start clean.
        self.told_teacher_here = told_teacher_here

    async def kick(self, room, user_id: str, event: str = "member:removed") -> None:
        """Drop a member from the live class, on every tab they have open.

        `event` says why: `member:removed` when a teacher took them out, `member:left` when
        they stepped out themselves. The two read differently to the person, and a student
        who just pressed Leave must not be told a teacher removed them.
        """
        name = socket_room(room.id)
        try:
            sids = [sid for sid, _ in self._sio.manager.get_participants("/", _user_room(user_id))]
        except KeyError:
            sids = []
        for sid in sids:
            await self._sio.leave_room(sid, name)
            await self._sio.emit(event, {"code": room.code}, to=sid)
        await self._present(room)

    async def announce_closed(self, room) -> None:
        """Tell everyone in the class it has been archived."""
        self.told_teacher_here.pop(room.id, None)
        await self._sio.emit("room:closed", room=socket_room(room.id))


def attach(sio: socketio.AsyncServer, get_pool, get_authenticator, web_origins) -> Live:
    """Wire the handlers onto a server.

    `get_pool` and `get_authenticator` are callables so each can be set up at startup
    rather than at import.
    """
    #: sid -> the account on that connection. Held in this process, which is fine
    #: because the service runs one worker: Socket.IO's own state is in-process too.
    users: dict[str, User] = {}
    async def resume(room) -> None:
        """The teacher is back: the session is on again, for whoever is still in the class.

        Only ever a deliberate act by the teacher (opening the class afresh). Not a reconnect
        of the connection that ended it: that one is taken out of the class when it ends, so
        a network blip cannot start the session again.
        """
        if room.session_ended:
            await set_session_ended(get_pool(), room.id, False)
            await sio.emit("session:started", room=socket_room(room.id))

    async def expire_at_login_end(sid: str, user: User) -> None:
        """Drop a connection when the login it was made with runs out.

        The login is checked once, at connect. Without this a person who logs out (or whose
        token simply expires, after about an hour) would stay connected, and a teacher would
        keep driving the class. The client is told, refreshes its login, and reconnects.
        """
        if user.expires_at is None:
            return
        await sio.sleep(max(0.0, user.expires_at - time.time()))
        if users.get(sid) is user:
            await sio.emit("auth:expired", to=sid)
            await sio.disconnect(sid)

    def present_users(room_id: str, *, excluding: str | None = None) -> set[str]:
        """The distinct accounts in this room.

        Accounts, not sockets: one person with two tabs is one person. `excluding` is for
        disconnect, where the leaving socket may still be listed depending on when the
        handler runs relative to the manager's cleanup; counting it would leave the others
        showing one person too many, and nothing errors when a count is wrong.
        """
        try:
            members = sio.manager.get_participants("/", socket_room(room_id))
        except KeyError:
            return set()
        return {users[sid].id for sid, _ in members if sid != excluding and sid in users}

    #: Whether the teacher was here the last time students were told, by class. Students are told
    #: only when it changes.
    told_teacher_here: dict[str, bool] = {}

    async def broadcast_presence(room, *, excluding: str | None = None) -> None:
        """Say who is here, to those who need to know.

        The **teacher** is told the head-count and whether they themselves are counted, on every
        change: it is the number on their screen. **Students** are not shown a count, so they are
        told one thing only, whether the teacher is here, and only when that changes. Sending
        every arrival to everyone is students-squared messages (a hundred joining at the start is
        ten thousand), none of which any student could see.
        """
        here = present_users(room.id, excluding=excluding)
        teacher_here = room.host_user_id in here
        try:
            members = sio.manager.get_participants("/", socket_room(room.id))
        except KeyError:
            members = []
        teacher_sids = [
            sid for sid, _ in members
            if sid != excluding and sid in users and users[sid].id == room.host_user_id
        ]
        for sid in teacher_sids:
            await sio.emit("presence", {"count": len(here), "teacherHere": teacher_here}, to=sid)
        if told_teacher_here.get(room.id) != teacher_here:
            told_teacher_here[room.id] = teacher_here
            await sio.emit(
                "presence",
                {"teacherHere": teacher_here},
                room=socket_room(room.id),
                skip_sid=teacher_sids or None,
            )

    @sio.event
    async def connect(sid, environ, auth=None):
        """Accept a connection only if it carries a valid Academy login."""
        authenticator = get_authenticator()
        if authenticator is None:
            raise ConnectionRefusedError("Login is not configured on this server.")

        token, from_cookies = _token_from_handshake(environ, auth)
        if from_cookies and token:
            # A browser sends cookies on any request to this site, including a socket a
            # hostile page opens. Browsers always send `Origin` on a socket, and a page
            # cannot forge it, so cookies are honoured only from our own pages. (A token
            # passed in `auth` is a deliberate act and needs no such check.)
            origin = environ.get("HTTP_ORIGIN", "")
            if origin not in web_origins:
                raise ConnectionRefusedError("This page may not use the classroom.")
        if not token:
            raise ConnectionRefusedError("Log in to join a class.")
        try:
            user = authenticator.verify(token)
        except AuthError:
            raise ConnectionRefusedError("Your login has expired. Please log in again.")

        users[sid] = user
        await sio.enter_room(sid, _user_room(user.id))
        sio.start_background_task(expire_at_login_end, sid, user)

    @sio.on("room:join")
    async def room_join(sid, payload):
        try:
            user = users.get(sid)
            if user is None:
                return {"ok": False, "error": "Log in to join a class."}
            payload = payload or {}
            code = str(payload.get("code") or "")

            # A refusal that says anything about a code is charged to the same budget as
            # joining by code, so this cannot be used to find live codes for free. See
            # `ratelimit.charge_code_miss`.
            room = await get_room(get_pool(), code)
            if room is None:
                if not code_attempts.hit(user.id):
                    return {"ok": False, "error": TOO_MANY}
                return {"ok": False, "error": "No class with that code."}

            # The account is the credential. Knowing the code is not enough to be *in* a
            # class, only to become a member of it, which is done over HTTP. Membership is
            # checked before "archived" so a stranger learns nothing about a class's state.
            member = await get_member(get_pool(), room.id, user.id)
            if member is None:
                if not code_attempts.hit(user.id):
                    return {"ok": False, "error": TOO_MANY}
                return {"ok": False, "error": "You are not in this class. Join it with its code first."}
            if not room.is_live:
                return {"ok": False, "error": "That class has been archived."}
            if member.left_at is not None:
                return {"ok": False, "error": "You left this class. Rejoin it from your list first."}

            # One class at a time on a connection.
            session = await sio.get_session(sid)
            previous = (session or {}).get("room_id")
            if previous and previous != room.id:
                await sio.leave_room(sid, socket_room(previous))

            await sio.enter_room(sid, socket_room(room.id))
            await sio.save_session(sid, {"room_id": room.id})
            await touch_member(get_pool(), room.id, user.id)

            # The teacher coming back to a class whose session ended starts it again.
            session_ended = room.session_ended
            if is_host(room, user) and session_ended:
                await resume(room)
                session_ended = False

            here = present_users(room.id)
            teacher_here = room.host_user_id in here
            # Presence goes to everyone *after* the joiner's own answer is built, so the
            # two agree. The joiner is already in the room, so they are counted.
            await broadcast_presence(room)
            log.info("%s joined %s (%d present)", user.username, room.code, len(here))
            return {
                "ok": True,
                "state": _room_state(
                    room, user, len(here), teacher_here, session_ended,
                ),
            }
        except Exception:
            log.exception("room:join failed")
            return {"ok": False, "error": "Could not join the class."}

    async def _teacher_room(sid):
        """The room this connection is in, if its account is the teacher; else the reason
        it may not act, as `(None, error)`."""
        user = users.get(sid)
        if user is None:
            return None, "Log in to join a class."
        session = await sio.get_session(sid)
        room_id = (session or {}).get("room_id")
        if not room_id:
            return None, "Join a class first."
        room = await get_room_by_id(get_pool(), room_id)
        if room is None:
            return None, "No such class."
        # Checked here as well as on join. Without it a teacher still connected after the
        # class is archived keeps driving it: the broadcast goes out, the position moves
        # and the log grows, on a room that refuses newcomers.
        if not room.is_live:
            return None, "That class has been archived."
        # The authority check. A student can emit these events; they get no further.
        if not is_host(room, user):
            return None, "Only this class's teacher can do that."
        return room, None

    @sio.on("teach:goto")
    async def teach_goto(sid, payload):
        try:
            segment_id = str((payload or {}).get("segmentId") or "")
            room, error = await _teacher_room(sid)
            if room is None:
                return {"ok": False, "error": error}

            moved = await set_current_segment(get_pool(), room, segment_id)
            if not moved:
                return {"ok": False, "error": "That segment is not part of this text."}

            # Everyone, the sender included. If the teacher's own view were updated
            # locally and the students' by broadcast, the two paths could silently drift
            # apart and the teacher would be the last to know.
            await sio.emit("teach:goto", {"segmentId": segment_id}, room=socket_room(room.id))
            return {"ok": True}
        except Exception:
            log.exception("teach:goto failed")
            return {"ok": False, "error": "Could not move the class."}

    @sio.on("session:end")
    async def session_end(sid, payload=None):
        """End *today's* session. The class stays: everyone remains a member, and the
        position is kept for next time. Archiving the whole class is a separate act, over
        HTTP."""
        try:
            room, error = await _teacher_room(sid)
            if room is None:
                return {"ok": False, "error": error}
            await set_session_ended(get_pool(), room.id, True)
            await sio.emit("session:ended", room=socket_room(room.id))
            # The teacher's own connection leaves the class with it. Their page shows the
            # "session ended" notice and stays mounted, so without this a reconnect would
            # re-join and the join would start the session again. Starting it is a deliberate
            # act: opening the class afresh.
            await sio.leave_room(sid, socket_room(room.id))
            await sio.save_session(sid, {})
            await broadcast_presence(room, excluding=sid)
            log.info("session in %s ended by the teacher", room.code)
            return {"ok": True}
        except Exception:
            log.exception("session:end failed")
            return {"ok": False, "error": "Could not end the session."}

    @sio.event
    async def disconnect(sid, reason=None):
        """Presence must drop when someone closes their browser.

        Easy to leave out, because nothing fails loudly when you do — the room just
        reports more people than are in it, forever.
        """
        try:
            session = await sio.get_session(sid)
            room_id = (session or {}).get("room_id")
            if room_id:
                room = await get_room_by_id(get_pool(), room_id)
                if room is not None:
                    await broadcast_presence(room, excluding=sid)
        except Exception:
            log.exception("disconnect handling failed")
        finally:
            users.pop(sid, None)

    return Live(sio, broadcast_presence, told_teacher_here)
