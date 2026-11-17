"""Who may do what to a class.

The SQL is exercised against a real MySQL by `scripts/check_members.py`. These test the
part that is easy to get wrong and costly when wrong: the *permission* decisions in the
routes, with the database replaced by stand-ins. Mostly about what must be refused.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from classroom_sync import routes
from classroom_sync.app import api
from classroom_sync.auth import Authenticator
from classroom_sync.rooms import ClassSummary, Member, Room
from tests.test_auth import AUDIENCE, ISSUER, jwk_set, token

client = TestClient(api)

TEACHER = {"sub": "teacher-1", "preferred_username": "dorje", "name": "Dorje"}
STUDENT = {"sub": "student-1", "preferred_username": "pema", "name": "Pema"}
OUTSIDER = {"sub": "other-9", "preferred_username": "stranger", "name": ""}


def auth(who: dict) -> dict:
    return {"Authorization": "JWT " + token(**who)}


def a_room(**overrides) -> Room:
    values = dict(
        id="room-1", code="ABC234", segmentation_id="seg-1", edition_id="ed-1",
        title="A Text", current_segment_id=None, is_live=True,
        translation_edition_id=None, name="Tuesday study", host_user_id="teacher-1",
    )
    values.update(overrides)
    return Room(**values)


class FakeDb:
    pool = object()


@pytest.fixture(autouse=True)
def app(monkeypatch):
    api.state.authenticator = Authenticator(jwk_set(), ISSUER, AUDIENCE)
    api.state.database = FakeDb()
    api.state.calls = []

    class FakeLive:
        async def kick(self, room, user_id, event="member:removed"):
            api.state.calls.append(("kick", user_id, event))

        async def announce_closed(self, room):
            api.state.calls.append(("announce_closed", room.id))

    real_live = getattr(api.state, "live", None)
    api.state.live = FakeLive()

    async def fake_get_room(pool, code):
        return a_room() if code.upper() == "ABC234" else None

    monkeypatch.setattr(routes, "get_room", fake_get_room)

    async def record(name, result=None):
        api.state.calls.append(name)
        return result

    async def add_member(pool, room, user):
        await record(("add_member", user.id))
        return "teacher" if room.host_user_id == user.id else "student"

    async def list_members(pool, room_id):
        return [Member("teacher-1", "dorje", "Dorje", "teacher", datetime(2026, 1, 1), datetime(2026, 1, 2))]

    async def remove_member(pool, room_id, user_id):
        await record(("remove_member", user_id))
        return user_id == "student-1"

    async def leave_member(pool, room_id, user_id):
        await record(("leave_member", user_id))
        return user_id == "student-1"

    async def set_class_dates(pool, room_id, starts_on, ends_on):
        await record(("set_class_dates", starts_on, ends_on))

    async def change_code(pool, room):
        await record(("change_code", room.id))
        return "NEW234"

    async def close_room(pool, room):
        await record(("close_room", room.id))

    async def classes_for(pool, user_id):
        await record(("classes_for", user_id))
        return [ClassSummary("ABC234", "Tuesday study", "A Text", "student", 12, datetime(2026, 1, 2))]

    monkeypatch.setattr(routes, "add_member", add_member)
    monkeypatch.setattr(routes, "list_members", list_members)
    monkeypatch.setattr(routes, "remove_member", remove_member)
    monkeypatch.setattr(routes, "leave_member", leave_member)
    monkeypatch.setattr(routes, "set_class_dates", set_class_dates)
    monkeypatch.setattr(routes, "change_code", change_code)
    monkeypatch.setattr(routes, "close_room", close_room)
    monkeypatch.setattr(routes, "classes_for", classes_for)
    from classroom_sync.ratelimit import code_attempts

    code_attempts.reset()
    yield
    code_attempts.reset()
    # Put back what was there: `api` is shared, and the socket tests use the real one.
    api.state.live = real_live
    api.state.authenticator = None


def did(name: str) -> list:
    return [call for call in api.state.calls if call[0] == name]


# Every one of these needs a login.
NEEDS_LOGIN = [
    ("get", "/api/me/classes"),
    ("post", "/api/rooms/ABC234/join"),
    ("get", "/api/rooms/ABC234"),
    ("get", "/api/rooms/ABC234/members"),
    ("delete", "/api/rooms/ABC234/members/student-1"),
    ("post", "/api/rooms/ABC234/change-code"),
    ("post", "/api/rooms/ABC234/archive"),
    ("post", "/api/rooms/ABC234/leave"),
    ("put", "/api/rooms/ABC234/dates"),
]


@pytest.mark.parametrize("method, path", NEEDS_LOGIN)
def test_every_class_route_refuses_a_stranger(method, path):
    assert getattr(client, method)(path).status_code == 401
    assert api.state.calls == [], "nothing may reach the database without a login"


# Teacher-only.
TEACHER_ONLY = [
    ("get", "/api/rooms/ABC234/members"),
    ("delete", "/api/rooms/ABC234/members/student-1"),
    ("post", "/api/rooms/ABC234/change-code"),
    ("post", "/api/rooms/ABC234/archive"),
]


@pytest.mark.parametrize("method, path", TEACHER_ONLY)
@pytest.mark.parametrize("who", [STUDENT, OUTSIDER], ids=["a student", "an outsider"])
def test_only_the_teacher_can_manage_a_class(method, path, who):
    response = getattr(client, method)(path, headers=auth(who))
    assert response.status_code == 403
    assert not did("remove_member") and not did("change_code") and not did("close_room")


@pytest.mark.parametrize("method, path", TEACHER_ONLY)
def test_a_class_nobody_owns_cannot_be_managed_by_anyone(method, path, monkeypatch):
    """A room made before accounts has no owner. Guessing one would hand it to the wrong
    person, so it has none, and `None == someone` must never be true."""
    async def orphan(pool, code):
        return a_room(host_user_id=None)

    monkeypatch.setattr(routes, "get_room", orphan)
    assert getattr(client, method)(path, headers=auth(TEACHER)).status_code == 403


@pytest.mark.parametrize("method, path", TEACHER_ONLY)
def test_a_missing_class_is_a_404(method, path):
    path = path.replace("ABC234", "ZZZ999")
    assert getattr(client, method)(path, headers=auth(TEACHER)).status_code == 404


def test_anyone_logged_in_can_join_and_is_recorded():
    response = client.post("/api/rooms/abc234/join", headers=auth(STUDENT))
    assert response.status_code == 200
    assert response.json()["role"] == "student"
    assert response.json()["code"] == "ABC234"
    assert did("add_member") == [("add_member", "student-1")]


def test_the_creator_joins_as_the_teacher():
    assert client.post("/api/rooms/ABC234/join", headers=auth(TEACHER)).json()["role"] == "teacher"


def test_joining_an_unknown_code_is_a_404():
    assert client.post("/api/rooms/ZZZ999/join", headers=auth(STUDENT)).status_code == 404
    assert not did("add_member")


def test_nobody_can_join_an_archived_class(monkeypatch):
    async def archived(pool, code):
        return a_room(is_live=False)

    monkeypatch.setattr(routes, "get_room", archived)
    response = client.post("/api/rooms/ABC234/join", headers=auth(STUDENT))
    assert response.status_code == 410
    assert not did("add_member")


def test_the_teacher_sees_the_members():
    response = client.get("/api/rooms/ABC234/members", headers=auth(TEACHER))
    assert response.status_code == 200
    assert response.json()[0]["username"] == "dorje"
    assert response.json()[0]["role"] == "teacher"


def test_the_teacher_can_remove_a_student():
    response = client.delete("/api/rooms/ABC234/members/student-1", headers=auth(TEACHER))
    assert response.status_code == 204
    assert did("remove_member") == [("remove_member", "student-1")]
    assert did("kick") == [("kick", "student-1", "member:removed")], "a removed member must be dropped from the live class"


def test_the_teacher_cannot_remove_themselves():
    response = client.delete("/api/rooms/ABC234/members/teacher-1", headers=auth(TEACHER))
    assert response.status_code == 400
    assert not did("remove_member")


def test_removing_someone_who_is_not_a_member_is_a_404():
    assert client.delete("/api/rooms/ABC234/members/nobody", headers=auth(TEACHER)).status_code == 404


def test_a_student_can_leave_the_class():
    response = client.post("/api/rooms/ABC234/leave", headers=auth(STUDENT))
    assert response.status_code == 204
    # They step out, keeping their place: nothing is deleted, so no code is needed to return.
    assert did("leave_member") == [("leave_member", "student-1")]
    assert not did("remove_member")
    assert did("kick") == [("kick", "student-1", "member:left")], "leaving drops them from the live class, saying they left"


def test_the_teacher_cannot_leave_their_own_class():
    response = client.post("/api/rooms/ABC234/leave", headers=auth(TEACHER))
    assert response.status_code == 400
    assert not did("leave_member")


def test_leaving_a_class_you_are_not_in_is_a_404():
    assert client.post("/api/rooms/ABC234/leave", headers=auth(OUTSIDER)).status_code == 404


def test_leaving_an_unknown_class_is_a_404():
    assert client.post("/api/rooms/ZZZ999/leave", headers=auth(STUDENT)).status_code == 404


def test_the_teacher_can_change_the_code():
    response = client.post("/api/rooms/ABC234/change-code", headers=auth(TEACHER))
    assert response.status_code == 200
    assert response.json() == {"code": "NEW234"}


def test_an_archived_class_cannot_have_its_code_changed(monkeypatch):
    async def archived(pool, code):
        return a_room(is_live=False)

    monkeypatch.setattr(routes, "get_room", archived)
    assert client.post("/api/rooms/ABC234/change-code", headers=auth(TEACHER)).status_code == 410


def test_the_teacher_can_archive():
    assert client.post("/api/rooms/ABC234/archive", headers=auth(TEACHER)).status_code == 204
    assert did("close_room") == [("close_room", "room-1")]
    assert did("announce_closed") == [("announce_closed", "room-1")]


def test_my_classes_is_asked_for_the_logged_in_account_only():
    response = client.get("/api/me/classes", headers=auth(STUDENT))
    assert response.status_code == 200
    assert response.json()[0] == {
        "code": "ABC234", "name": "Tuesday study", "title": "A Text", "role": "student",
        "memberCount": 12, "lastSeenAt": "2026-01-02T00:00:00",
        "startsOn": None, "endsOn": None, "left": False,
    }
    # The account comes from the verified token, never from the request.
    assert did("classes_for") == [("classes_for", "student-1")]


def test_guessing_codes_too_fast_is_slowed_down():
    from classroom_sync.ratelimit import code_attempts

    code_attempts.reset()
    codes = [client.post(f"/api/rooms/ZZ{n:04d}/join", headers=auth(STUDENT)).status_code for n in range(31)]
    assert codes[:30] == [404] * 30
    assert codes[30] == 429
    # Per account: someone else is not held up by it.
    assert client.post("/api/rooms/ZZ9999/join", headers=auth(OUTSIDER)).status_code == 404
    code_attempts.reset()


def test_the_teacher_can_set_when_the_class_is_held():
    response = client.put(
        "/api/rooms/ABC234/dates", headers=auth(TEACHER),
        json={"startsOn": "2026-03-12", "endsOn": "2026-04-30"},
    )
    assert response.status_code == 204
    (call,) = did("set_class_dates")
    assert (str(call[1]), str(call[2])) == ("2026-03-12", "2026-04-30")


def test_either_date_can_be_set_alone_or_cleared():
    assert client.put("/api/rooms/ABC234/dates", headers=auth(TEACHER), json={"endsOn": "2026-04-30"}).status_code == 204
    assert client.put("/api/rooms/ABC234/dates", headers=auth(TEACHER), json={}).status_code == 204


def test_a_class_cannot_end_before_it_starts():
    response = client.put(
        "/api/rooms/ABC234/dates", headers=auth(TEACHER),
        json={"startsOn": "2026-04-30", "endsOn": "2026-03-12"},
    )
    assert response.status_code == 422
    assert not did("set_class_dates")


def test_a_student_cannot_set_the_dates():
    response = client.put("/api/rooms/ABC234/dates", headers=auth(STUDENT), json={"endsOn": "2026-04-30"})
    assert response.status_code == 403
    assert not did("set_class_dates")
