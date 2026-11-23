"""A student's private notes: who may touch them, and what may be stored.

The SQL is exercised live (see the end-to-end check in docs/BUILD-PLAN.md). These test the
decisions that are easy to get wrong and costly when wrong, with the database replaced by
stand-ins: only students, only members, only your own, and nothing malformed stored.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from classroom_sync import notes as notes_store
from classroom_sync import routes
from classroom_sync.app import api
from classroom_sync.auth import Authenticator
from classroom_sync.rooms import Member, Room
from tests.test_auth import AUDIENCE, ISSUER, jwk_set, token

client = TestClient(api)

TEACHER = {"sub": "teacher-1", "preferred_username": "dorje", "name": "Dorje"}
STUDENT = {"sub": "student-1", "preferred_username": "pema", "name": "Pema"}
OTHER_STUDENT = {"sub": "student-2", "preferred_username": "tsering", "name": ""}
OUTSIDER = {"sub": "other-9", "preferred_username": "stranger", "name": ""}

NOW = datetime(2026, 1, 2, 3, 4, 5)


def auth(who: dict) -> dict:
    return {"Authorization": "JWT " + token(**who)}


def a_room(**overrides) -> Room:
    values = dict(
        id="room-1", code="ABC234", segmentation_id="seg-1", edition_id="ed-1",
        title="A Text", current_segment_id=None, is_live=True,
        translation_edition_id=None, name=None, host_user_id="teacher-1",
    )
    values.update(overrides)
    return Room(**values)


def a_note(**overrides) -> notes_store.Note:
    values = dict(
        id="note-1", segment_id="seg-a", layer="source", start=3, end=9, quote="words",
        prefix="", suffix="", color="yellow", body=None, created_at=NOW, updated_at=NOW,
    )
    values.update(overrides)
    return notes_store.Note(**values)


GOOD = {
    "segmentId": "seg-a", "layer": "source", "startOffset": 3, "endOffset": 8,
    "quote": "words", "prefix": "a ", "suffix": " b", "color": "yellow", "body": "my thought",
}


class FakeDb:
    pool = object()

    async def has_segment(self, segmentation_id, segment_id):
        return segment_id in {"seg-a", "seg-b"}


class World:
    def __init__(self) -> None:
        self.room = a_room()
        self.roles = {"teacher-1": "teacher", "student-1": "student", "student-2": "student"}
        self.stored: list[dict] = []
        self.count = 0
        self.left: set[str] = set()
        self.calls: list[tuple] = []


@pytest.fixture
def world(monkeypatch) -> World:
    w = World()
    api.state.authenticator = Authenticator(jwk_set(), ISSUER, AUDIENCE)
    api.state.database = FakeDb()

    async def get_room(pool, code):
        return w.room if code.upper() == "ABC234" else None

    async def get_member(pool, room_id, user_id):
        role = w.roles.get(user_id)
        if role is None:
            return None
        return Member(
            user_id, user_id, "", role, None, None,
            left_at="then" if user_id in w.left else None,
        )

    async def list_notes(pool, room_id, user_id):
        w.calls.append(("list", user_id))
        return [a_note()]

    async def create_note(pool, room_id, user_id, **fields):
        if w.count >= notes_store.MAX_NOTES_PER_STUDENT:
            return None
        w.calls.append(("create", user_id, fields))
        return a_note(**{k: v for k, v in fields.items() if k in a_note().__dict__})

    async def update_note(pool, room_id, user_id, note_id, **fields):
        w.calls.append(("update", user_id, note_id, fields))
        return a_note(id=note_id) if note_id == "note-1" else None

    async def delete_note(pool, room_id, user_id, note_id):
        w.calls.append(("delete", user_id, note_id))
        return note_id == "note-1"

    monkeypatch.setattr(routes, "get_room", get_room)
    monkeypatch.setattr(routes, "get_member", get_member)
    monkeypatch.setattr(notes_store, "list_notes", list_notes)
    monkeypatch.setattr(notes_store, "create_note", create_note)
    monkeypatch.setattr(notes_store, "update_note", update_note)
    monkeypatch.setattr(notes_store, "delete_note", delete_note)
    yield w
    api.state.authenticator = None


URL = "/api/rooms/ABC234/notes"
EVERY_ROUTE = [
    ("get", URL, None),
    ("post", URL, GOOD),
    ("patch", f"{URL}/note-1", {"color": "green"}),
    ("delete", f"{URL}/note-1", None),
]


def send(method, path, who=None, body=None):
    return getattr(client, method)(path, headers=auth(who) if who else {}, **({"json": body} if body is not None else {}))


# -- who may touch notes ---------------------------------------------------------------


@pytest.mark.parametrize("method, path, body", EVERY_ROUTE)
def test_every_notes_route_needs_a_login(world, method, path, body):
    assert send(method, path, None, body).status_code == 401
    assert world.calls == []


@pytest.mark.parametrize("method, path, body", EVERY_ROUTE)
def test_a_teacher_cannot_keep_notes_for_now(world, method, path, body):
    """One check, `NOTES_ROLE`, so allowing teachers later is one line."""
    response = send(method, path, TEACHER, body)
    assert response.status_code == 403
    assert "students" in response.json()["detail"]
    assert world.calls == []


@pytest.mark.parametrize("method, path, body", EVERY_ROUTE)
def test_someone_not_in_the_class_cannot_reach_its_notes(world, method, path, body):
    response = send(method, path, OUTSIDER, body)
    assert response.status_code == 403
    assert world.calls == []


@pytest.mark.parametrize("method, path, body", EVERY_ROUTE)
def test_a_missing_class_is_a_404(world, method, path, body):
    assert send(method, path.replace("ABC234", "ZZZ999"), STUDENT, body).status_code == 404


# -- my notes ----------------------------------------------------------------------------


def test_a_student_gets_their_own_notes(world):
    response = send("get", URL, STUDENT)
    assert response.status_code == 200
    assert response.json()[0]["quote"] == "words"
    assert world.calls == [("list", "student-1")]


def test_the_owner_is_the_verified_login_never_anything_the_request_says(world):
    """Even a request that *claims* another account's id is stored as the caller's."""
    body = {**GOOD, "userId": "student-2", "user_id": "student-2"}
    send("post", URL, STUDENT, body)
    (call,) = [c for c in world.calls if c[0] == "create"]
    assert call[1] == "student-1"


def test_another_students_note_id_reaches_nothing(world):
    """Updates and deletes are scoped to the caller, so a guessed id is simply not found."""
    assert send("patch", f"{URL}/someone-elses", STUDENT, {"color": "pink"}).status_code == 404
    assert send("delete", f"{URL}/someone-elses", STUDENT).status_code == 404
    assert all(call[1] == "student-1" for call in world.calls)


def test_a_student_can_make_change_and_remove_a_note(world):
    assert send("post", URL, STUDENT, GOOD).status_code == 201
    assert send("patch", f"{URL}/note-1", STUDENT, {"color": "blue"}).status_code == 200
    assert send("delete", f"{URL}/note-1", STUDENT).status_code == 204


def test_blank_words_become_a_plain_highlight(world):
    body = {**GOOD, "body": "   "}
    send("post", URL, STUDENT, body)
    (call,) = [c for c in world.calls if c[0] == "create"]
    assert call[2]["body"] is None


def test_an_edit_can_clear_the_words_but_leaving_the_field_out_changes_nothing(world):
    send("patch", f"{URL}/note-1", STUDENT, {"body": None})
    send("patch", f"{URL}/note-1", STUDENT, {"color": "green"})
    clearing, untouched = [c[3] for c in world.calls if c[0] == "update"]
    assert clearing["set_body"] is True and clearing["body"] is None
    assert untouched["set_body"] is False


# -- what may be stored --------------------------------------------------------------------


@pytest.mark.parametrize(
    "why, change",
    [
        ("an unknown colour", {"color": "chartreuse"}),
        ("an unknown layer", {"layer": "footnote"}),
        ("nothing selected", {"quote": ""}),
        ("a backwards range", {"startOffset": 9, "endOffset": 3}),
        ("an empty range", {"startOffset": 5, "endOffset": 5}),
        ("a negative start", {"startOffset": -1}),
        ("an absurd offset", {"endOffset": 10**9}),
        ("an over-long quote", {"quote": "x" * 2001}),
        ("over-long context", {"prefix": "x" * 201}),
        ("an over-long note", {"body": "x" * 4001}),
        ("no line", {"segmentId": ""}),
    ],
)
def test_malformed_notes_are_refused_and_nothing_is_stored(world, why, change):
    response = send("post", URL, STUDENT, {**GOOD, **change})
    assert response.status_code == 422, why
    assert not [c for c in world.calls if c[0] == "create"]


def test_a_note_on_a_line_that_is_not_in_this_text_is_refused(world):
    response = send("post", URL, STUDENT, {**GOOD, "segmentId": "not-in-this-text"})
    assert response.status_code == 422
    assert not [c for c in world.calls if c[0] == "create"]


def test_there_is_a_limit_on_how_many_a_student_can_keep(world):
    world.count = notes_store.MAX_NOTES_PER_STUDENT
    assert send("post", URL, STUDENT, GOOD).status_code == 422
    assert not [c for c in world.calls if c[0] == "create"]


def test_offsets_that_do_not_match_the_quote_are_refused(world):
    # "words" is five characters, but this range is six.
    response = send("post", URL, STUDENT, {**GOOD, "startOffset": 3, "endOffset": 9})
    assert response.status_code == 422
    assert not [c for c in world.calls if c[0] == "create"]


def test_offsets_count_code_points_not_bytes(world):
    # Three Tibetan letters, each several bytes in UTF-8 but one code point.
    response = send(
        "post", URL, STUDENT, {**GOOD, "quote": "\u0f40\u0f41\u0f42", "startOffset": 0, "endOffset": 3},
    )
    assert response.status_code == 201


def test_an_edit_cannot_move_a_note_to_other_words(world):
    """Where a note is anchored never changes: different words are a different note."""
    send("patch", f"{URL}/note-1", STUDENT, {"color": "pink", "startOffset": 0, "quote": "other"})
    (call,) = [c[3] for c in world.calls if c[0] == "update"]
    assert set(call) == {"color", "set_body", "body"}


# -- archived classes -----------------------------------------------------------------------


def test_an_archived_class_can_still_be_read_but_not_written(world):
    world.room = a_room(is_live=False)
    assert send("get", URL, STUDENT).status_code == 200
    assert send("post", URL, STUDENT, GOOD).status_code == 410
    assert send("patch", f"{URL}/note-1", STUDENT, {"color": "pink"}).status_code == 410
    assert send("delete", f"{URL}/note-1", STUDENT).status_code == 410


def test_a_removed_members_notes_are_kept_but_unreachable(world):
    """Kept in the database, and shown again if they are let back in."""
    world.roles.pop("student-1")
    assert send("get", URL, STUDENT).status_code == 403
    world.roles["student-1"] = "student"
    assert send("get", URL, STUDENT).status_code == 200


def test_the_database_has_no_link_that_would_delete_notes_with_a_member():
    import pathlib

    sql = (pathlib.Path(__file__).resolve().parents[1] / "migrations" / "006_room_notes.sql").read_text()
    assert "room_members" not in sql.split("create table")[1], "notes must not be tied to membership rows"
    assert "foreign key (room_id) references rooms" in sql


def test_a_student_who_left_can_still_read_their_notes_but_not_add_to_them(world):
    world.left.add("student-1")
    assert send("get", URL, STUDENT).status_code == 200
    response = send("post", URL, STUDENT, GOOD)
    assert response.status_code == 403
    assert "rejoin" in response.json()["detail"].lower()
    assert not [c for c in world.calls if c[0] == "create"]


# -- the notes routes take a code too: see ratelimit.charge_code_miss ----------------------


def test_notes_refusals_that_reveal_a_code_are_throttled_either_way(world):
    for _ in range(30):
        assert send("get", "/api/rooms/ZZZ999/notes", OUTSIDER).status_code == 404
    assert send("get", "/api/rooms/ZZZ999/notes", OUTSIDER).status_code == 429
    # A class that exists, which the account is not in, now answers the same way.
    assert send("get", URL, OUTSIDER).status_code == 429


def test_reading_your_own_notes_is_never_charged(world):
    for _ in range(100):
        assert send("get", URL, STUDENT).status_code == 200
