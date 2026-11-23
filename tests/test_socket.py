"""The live socket, with real clients against a real server (only the database is faked).

Every property here is about **who** may do **what**, because that is what the socket
changed: a connection is an account, entering a class needs membership, and moving it
needs to be its teacher. Most tests are about what must be refused.
"""

import asyncio
import dataclasses
import socket
import threading
import time

import pytest
import pytest_asyncio
import socketio
import uvicorn

from classroom_sync import events, routes
from classroom_sync.app import api, app
from classroom_sync.auth import (
    COOKIE_HEADER_PAYLOAD,
    COOKIE_SIGNATURE,
    Authenticator,
)
from classroom_sync.config import config
from classroom_sync.rooms import Member, Room
from tests.test_auth import AUDIENCE, ISSUER, jwk_set, token

TEACHER = {"sub": "teacher-1", "preferred_username": "dorje", "name": "Dorje"}
STUDENT = {"sub": "student-1", "preferred_username": "pema", "name": "Pema"}
STUDENT2 = {"sub": "student-2", "preferred_username": "tashi", "name": "Tashi"}
LATE = {"sub": "student-2", "preferred_username": "tsering", "name": ""}
OUTSIDER = {"sub": "other-9", "preferred_username": "stranger", "name": ""}

ORIGIN = "http://apps.local.openedx.io:2003"

SERVER: dict = {}


async def on_server(coroutine):
    """Run a coroutine on the server's own event loop and wait for it."""
    future = asyncio.run_coroutine_threadsafe(coroutine, SERVER["loop"])
    return await asyncio.wrap_future(future)


def a_room(**overrides) -> Room:
    values = dict(
        id="room-1", code="ABC234", segmentation_id="seg-1", edition_id="ed-1",
        title="A Text", current_segment_id=None, is_live=True,
        translation_edition_id=None, name="Tuesday study", host_user_id="teacher-1",
    )
    values.update(overrides)
    return Room(**values)


class World:
    """The fake database: one room, and who is in it."""

    def __init__(self) -> None:
        self.room = a_room()
        self.members = {"teacher-1", "student-1", "student-2"}
        #: Members who have left but kept their place in the class.
        self.left: set[str] = set()
        self.segments = {"s1", "s2", "s3"}


@pytest.fixture
def world(monkeypatch) -> World:
    w = World()

    async def get_room(pool, code):
        return w.room if code.upper() == w.room.code else None

    async def get_room_by_id(pool, room_id):
        return w.room if room_id == w.room.id else None

    async def get_member(pool, room_id, user_id):
        if user_id in w.members:
            return Member(
                user_id, user_id, "", "student", None, None,
                left_at="then" if user_id in w.left else None,
            )
        return None

    async def touch_member(pool, room_id, user_id):
        return None

    async def set_current_segment(pool, room, segment_id):
        if segment_id not in w.segments:
            return False
        w.room = dataclasses.replace(w.room, current_segment_id=segment_id)
        return True

    monkeypatch.setattr(events, "get_room", get_room)
    monkeypatch.setattr(events, "get_room_by_id", get_room_by_id)
    monkeypatch.setattr(events, "get_member", get_member)
    monkeypatch.setattr(events, "touch_member", touch_member)
    async def set_session_ended(pool, room_id, ended):
        w.room = dataclasses.replace(w.room, session_ended=ended)

    monkeypatch.setattr(events, "set_current_segment", set_current_segment)
    monkeypatch.setattr(events, "set_session_ended", set_session_ended)
    api.state.live.told_teacher_here.clear()
    return w


@pytest.fixture(scope="module")
def url():
    """The real ASGI app on a real port, in a thread. Lifespan is off: it would try to
    open a database that is deliberately not here."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    # Its own event loop, kept, so a test can run server-side code *on it*. Socket.IO is not
    # thread-safe: emitting from the test's loop would queue onto the wrong one and the
    # message would never arrive. In production the HTTP routes run on the server's loop.
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=lambda: loop.run_until_complete(server.serve()), daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    SERVER["loop"] = loop
    api.state.database = type("Db", (), {"pool": object()})()
    api.state.authenticator = Authenticator(jwk_set(), ISSUER, AUDIENCE)
    config.web_origins[:] = [ORIGIN]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


class Client:
    """A socket for one account, recording what it hears."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.sio = socketio.AsyncClient()
        self.heard: dict[str, list] = {}
        #: Why the server refused the connection, if it did. The client's own exception
        #: says only "failed to connect", so a test asserting the *reason* reads this.
        self.refusal: str | None = None
        self.sio.on("connect_error", self._refused)
        for name in (
            "teach:goto", "presence", "session:ended", "session:started", "auth:expired", "room:closed",
            "member:removed", "member:left",
        ):
            self.sio.on(name, self._remember(name))

    async def _refused(self, data=None):
        self.refusal = str(data.get("message") if isinstance(data, dict) else data)

    def _remember(self, name):
        async def handler(data=None):
            self.heard.setdefault(name, []).append(data)
        return handler

    async def connect(self, who=None, *, raw_token=None, headers=None, auth=True):
        kwargs = {"transports": ["websocket"], "wait_timeout": 5}
        if headers:
            kwargs["headers"] = headers
        if auth:
            kwargs["auth"] = {"token": raw_token or token(**who)}
        await self.sio.connect(self.url, **kwargs)
        return self

    async def emit(self, event, payload=None):
        return await self.sio.call(event, payload or {}, timeout=5)

    async def join(self, code="ABC234"):
        return await self.emit("room:join", {"code": code})

    async def heard_within(self, name, seconds=1.0, count=1):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if len(self.heard.get(name, [])) >= count:
                return True
            await asyncio.sleep(0.02)
        return False

    async def disconnect(self):
        if self.sio.connected:
            await self.sio.disconnect()


@pytest_asyncio.fixture
async def clients(url):
    made: list[Client] = []

    async def make(who=None, **kwargs) -> Client:
        client = Client(url)
        made.append(client)
        await client.connect(who, **kwargs)
        return client

    make.made = made
    yield make
    for client in made:
        await client.disconnect()


async def refusal_of(clients, who=None, **kwargs) -> str | None:
    """Try to connect and return the server's reason for refusing, failing if it did not.

    Asserting the *reason*, not just that connecting failed: a connection can fail for any
    number of unrelated reasons, and a test that passes on all of them proves nothing.
    (This file once passed every refusal test with the client library not even installed.)
    """
    with pytest.raises(socketio.exceptions.ConnectionError):
        await clients(who, **kwargs)
    return clients.made[-1].refusal


# -- getting in -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_connection_with_no_login_is_refused(clients, world):
    assert "Log in" in await refusal_of(clients, auth=False)


@pytest.mark.asyncio
async def test_a_forged_login_is_refused(clients, world):
    from tests.test_auth import OTHER_KEY

    assert "login has expired" in await refusal_of(clients, raw_token=token(key=OTHER_KEY, **TEACHER))


@pytest.mark.asyncio
async def test_a_browsers_cookies_are_accepted_from_our_own_pages(clients, world):
    header_payload, _, signature = token(**STUDENT).rpartition(".")
    client = await clients(
        auth=False,
        headers={
            "Cookie": f"{COOKIE_HEADER_PAYLOAD}={header_payload}; {COOKIE_SIGNATURE}={signature}",
            "Origin": ORIGIN,
        },
    )
    assert (await client.join())["ok"] is True


@pytest.mark.asyncio
async def test_a_browsers_cookies_are_refused_from_any_other_page(clients, world):
    """A page on another site can open a socket and the browser will attach our cookies.
    It cannot forge the Origin, so that is what tells them apart."""
    header_payload, _, signature = token(**STUDENT).rpartition(".")
    # Refused by Socket.IO's own origin check before our handler runs.
    await refusal_of(
        clients,
        auth=False,
        headers={
            "Cookie": f"{COOKIE_HEADER_PAYLOAD}={header_payload}; {COOKIE_SIGNATURE}={signature}",
            "Origin": "http://evil.example",
        },
    )


@pytest.mark.asyncio
async def test_a_browsers_cookies_are_refused_when_no_origin_is_sent(clients, world):
    """Our own second line of defence, for a request Socket.IO's check lets through: a
    browser always sends Origin on a socket, so cookies with none are not from a page."""
    header_payload, _, signature = token(**STUDENT).rpartition(".")
    reason = await refusal_of(
        clients,
        auth=False,
        headers={"Cookie": f"{COOKIE_HEADER_PAYLOAD}={header_payload}; {COOKIE_SIGNATURE}={signature}"},
    )
    assert "may not use" in reason


# -- entering a class -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_member_can_enter_and_is_told_their_role(clients, world):
    student = await clients(STUDENT)
    result = await student.join()
    assert result["ok"] is True
    assert result["state"]["role"] == "student"
    assert result["state"]["name"] == "Tuesday study"
    assert result["state"]["currentSegmentId"] is None


@pytest.mark.asyncio
async def test_the_creator_enters_as_the_teacher(clients, world):
    teacher = await clients(TEACHER)
    assert (await teacher.join())["state"]["role"] == "teacher"


@pytest.mark.asyncio
async def test_someone_who_is_not_a_member_cannot_enter_even_with_the_code(clients, world):
    outsider = await clients(OUTSIDER)
    result = await outsider.join()
    assert result["ok"] is False
    assert "Join it with its code first" in result["error"]


@pytest.mark.asyncio
async def test_guessing_codes_over_the_socket_is_throttled_like_joining(clients, world):
    """room:join answers differently for no class and not-a-member, so it is charged to the
    same budget as joining by code; past it, both answers are the same."""
    outsider = await clients(OUTSIDER)
    for _ in range(30):
        assert (await outsider.join("ZZZ999"))["ok"] is False
    unknown = await outsider.join("ZZZ999")
    exists = await outsider.join("ABC234")
    assert unknown == exists
    assert "Too many attempts" in unknown["error"]


@pytest.mark.asyncio
async def test_a_member_rejoining_is_never_charged(clients, world):
    student = await clients(STUDENT)
    for _ in range(40):
        assert (await student.join())["ok"] is True


@pytest.mark.asyncio
async def test_a_stranger_learns_nothing_about_an_archived_class(clients, world):
    world.room = a_room(is_live=False)
    result = await (await clients(OUTSIDER)).join()
    assert "archived" not in result["error"]


@pytest.mark.asyncio
async def test_an_unknown_code_is_refused(clients, world):
    assert (await (await clients(STUDENT)).join("ZZZ999"))["ok"] is False


@pytest.mark.asyncio
async def test_an_archived_class_cannot_be_entered(clients, world):
    world.room = a_room(is_live=False)
    assert (await (await clients(STUDENT)).join())["ok"] is False


# -- who can move the class -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_student_cannot_move_the_class(clients, world):
    student = await clients(STUDENT)
    await student.join()
    result = await student.emit("teach:goto", {"segmentId": "s2"})
    assert result["ok"] is False
    assert world.room.current_segment_id is None


@pytest.mark.asyncio
async def test_the_teacher_moves_the_class_and_everyone_hears(clients, world):
    teacher, student = await clients(TEACHER), await clients(STUDENT)
    await teacher.join()
    await student.join()

    assert (await teacher.emit("teach:goto", {"segmentId": "s2"}))["ok"] is True
    assert await student.heard_within("teach:goto")
    assert student.heard["teach:goto"][0] == {"segmentId": "s2"}


@pytest.mark.asyncio
async def test_an_unknown_segment_is_refused(clients, world):
    teacher = await clients(TEACHER)
    await teacher.join()
    assert (await teacher.emit("teach:goto", {"segmentId": "nope"}))["ok"] is False


@pytest.mark.asyncio
async def test_a_late_joiner_lands_on_the_current_line(clients, world):
    teacher = await clients(TEACHER)
    await teacher.join()
    await teacher.emit("teach:goto", {"segmentId": "s3"})
    late = await clients(LATE)
    assert (await late.join())["state"]["currentSegmentId"] == "s3"


@pytest.mark.asyncio
async def test_a_connection_that_has_not_joined_cannot_move_anything(clients, world):
    teacher = await clients(TEACHER)
    assert (await teacher.emit("teach:goto", {"segmentId": "s2"}))["ok"] is False


@pytest.mark.asyncio
async def test_nobody_can_move_an_archived_class(clients, world):
    teacher = await clients(TEACHER)
    await teacher.join()
    world.room = dataclasses.replace(world.room, is_live=False)
    assert (await teacher.emit("teach:goto", {"segmentId": "s2"}))["ok"] is False


# -- presence -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_presence_counts_people_not_tabs(clients, world):
    student_a, student_b = await clients(STUDENT), await clients(STUDENT)
    await student_a.join()
    result = await student_b.join()
    assert result["state"]["participantCount"] == 1


@pytest.mark.asyncio
async def test_the_class_knows_whether_the_teacher_is_here(clients, world):
    student = await clients(STUDENT)
    assert (await student.join())["state"]["teacherHere"] is False

    teacher = await clients(TEACHER)
    await teacher.join()
    assert await student.heard_within("presence", count=2)
    # A student is told only whether the teacher is here, never a head-count.
    assert student.heard["presence"][-1] == {"teacherHere": True}

    await teacher.disconnect()
    assert await student.heard_within("presence", count=3)
    assert student.heard["presence"][-1] == {"teacherHere": False}


@pytest.mark.asyncio
async def test_the_teacher_is_told_the_count_and_students_are_not_told_about_each_other(clients, world):
    teacher, early = await clients(TEACHER), await clients(STUDENT)
    await teacher.join()
    await early.join()
    assert await teacher.heard_within("presence", count=2)
    assert teacher.heard["presence"][-1] == {"count": 2, "teacherHere": True}
    heard_before = len(early.heard.get("presence", []))

    # More students arrive: the teacher's number goes up; the student hears nothing.
    for who in (STUDENT2, {"sub": "student-3", "preferred_username": "x", "name": "X"}):
        world.members.add(who["sub"])
        await (await clients(who)).join()
    assert await teacher.heard_within("presence", count=4)
    assert teacher.heard["presence"][-1] == {"count": 4, "teacherHere": True}
    await asyncio.sleep(0.2)
    assert len(early.heard.get("presence", [])) == heard_before


# -- ending a session -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_teacher_can_end_the_session(clients, world):
    student = await clients(STUDENT)
    await student.join()
    assert (await student.emit("session:end"))["ok"] is False


@pytest.mark.asyncio
async def test_ending_the_session_tells_everyone_and_keeps_the_class(clients, world):
    teacher, student = await clients(TEACHER), await clients(STUDENT)
    await teacher.join()
    await student.join()
    assert (await teacher.emit("session:end"))["ok"] is True
    assert await student.heard_within("session:ended")
    # The class itself is untouched: still live, nobody removed.
    assert world.room.is_live and "student-1" in world.members


@pytest.mark.asyncio
async def test_a_connection_is_dropped_when_its_login_runs_out(clients, world):
    import time as _time

    short = token(**STUDENT, exp=int(_time.time()) + 2)
    student = await clients(raw_token=short)
    assert (await student.join())["ok"] is True
    # Told, so the page can refresh its login and reconnect, and then dropped.
    assert await student.heard_within("auth:expired", seconds=5)
    for _ in range(50):
        if not student.sio.connected:
            break
        await asyncio.sleep(0.1)
    assert not student.sio.connected


@pytest.mark.asyncio
async def test_a_student_who_joins_after_the_session_ended_is_told(clients, world):
    teacher, early = await clients(TEACHER), await clients(STUDENT)
    await teacher.join()
    await early.join()
    await teacher.emit("session:end")
    await teacher.disconnect()
    assert await early.heard_within("session:ended")

    late = await clients(STUDENT2)
    assert (await late.join())["state"]["sessionEnded"] is True


@pytest.mark.asyncio
async def test_the_teachers_reconnect_does_not_start_an_ended_session_again(clients, world):
    teacher, student = await clients(TEACHER), await clients(STUDENT)
    await teacher.join()
    await student.join()
    await teacher.emit("session:end")
    assert await student.heard_within("session:ended")

    # The ending connection has left the class, so it can neither drive it nor, by re-joining
    # on a reconnect, start the session again.
    assert (await teacher.emit("teach:goto", {"segmentId": "s2"}))["ok"] is False
    assert world.room.session_ended is True
    assert "session:started" not in student.heard


@pytest.mark.asyncio
async def test_a_student_who_has_left_cannot_enter_the_class_until_they_rejoin(clients, world):
    world.left.add("student-1")
    student = await clients(STUDENT)
    result = await student.join()
    assert result["ok"] is False
    assert "rejoin" in result["error"].lower()

    world.left.discard("student-1")
    assert (await student.join())["ok"] is True


@pytest.mark.asyncio
async def test_a_student_is_not_told_the_session_ended_when_it_has_not(clients, world):
    student = await clients(STUDENT)
    assert (await student.join())["state"]["sessionEnded"] is False


@pytest.mark.asyncio
async def test_the_teacher_coming_back_starts_the_session_again(clients, world):
    teacher, student = await clients(TEACHER), await clients(STUDENT)
    await teacher.join()
    await student.join()
    await teacher.emit("session:end")
    await teacher.disconnect()
    assert await student.heard_within("session:ended")

    again = await clients(TEACHER)
    assert (await again.join())["state"]["sessionEnded"] is False
    assert await student.heard_within("session:started")

    # And a student who arrives now is not told it is over.
    newcomer = await clients(STUDENT2)
    assert (await newcomer.join())["state"]["sessionEnded"] is False


# -- what the HTTP side does to a live class ------------------------------------------


@pytest.mark.asyncio
async def test_a_removed_member_is_dropped_from_the_live_class(clients, world):
    teacher, student = await clients(TEACHER), await clients(STUDENT)
    await teacher.join()
    await student.join()

    world.members.discard("student-1")
    await on_server(api.state.live.kick(world.room, "student-1"))

    assert await student.heard_within("member:removed")
    await teacher.emit("teach:goto", {"segmentId": "s2"})
    await asyncio.sleep(0.2)
    assert "teach:goto" not in student.heard, "a removed member must stop hearing the class"


@pytest.mark.asyncio
async def test_a_removed_member_cannot_come_back_in_by_reconnecting(clients, world):
    student = await clients(STUDENT)
    await student.join()
    world.members.discard("student-1")
    await on_server(api.state.live.kick(world.room, "student-1"))
    assert (await student.join())["ok"] is False


@pytest.mark.asyncio
async def test_archiving_tells_everyone_in_the_class(clients, world):
    student = await clients(STUDENT)
    await student.join()
    await on_server(api.state.live.announce_closed(world.room))
    assert await student.heard_within("room:closed")


@pytest.mark.asyncio
async def test_changing_the_code_does_not_disturb_people_already_in(clients, world):
    """Rooms are keyed by id, so a new code strands nobody."""
    teacher, student = await clients(TEACHER), await clients(STUDENT)
    await teacher.join()
    await student.join()
    world.room = dataclasses.replace(world.room, code="NEW234")
    assert (await teacher.emit("teach:goto", {"segmentId": "s2"}))["ok"] is True
    assert await student.heard_within("teach:goto")
