"""The HTTP API.

What a client does outside the live socket: find a text, open a class on it, join one
with its code, list your classes, and (for a teacher) manage members and the code.

Field names are **camelCase on the wire**, matching the client. The temptation to write
`segment_id` is the single most likely way to break this service — see
docs/socket-contract.md.
"""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from .auth import User, current_user
from .library import ID_PATTERN, LibraryError
from .ratelimit import limit_code_attempts
from . import notes as notes_store
from .rooms import (
    RoomError,
    add_member,
    change_code,
    classes_for,
    close_room,
    create_room,
    get_member,
    get_room,
    is_host,
    leave_member,
    list_members,
    password_matches,
    remove_member,
    set_class_dates,
)

router = APIRouter(prefix="/api")


class TextResult(BaseModel):
    """A search hit, enough to choose between."""

    id: str
    title: str
    language: str


class CreateRoomRequest(BaseModel):
    hostPassword: str = Field(min_length=1)
    #: A library text id, from a search. The server resolves it to one edition and
    #: segmentation — never the client, or two students can land on different ids.
    textId: str = Field(pattern=rf"^{ID_PATTERN}$")
    #: What the class is called in a student's list. Defaults to the text's title.
    name: str | None = Field(default=None, max_length=255)
    #: The first and last day the class is held. Both optional.
    startsOn: date | None = None
    endsOn: date | None = None

    @model_validator(mode="after")
    def _dates_in_order(self):
        if self.startsOn and self.endsOn and self.endsOn < self.startsOn:
            raise ValueError("The class cannot end before it starts")
        return self


class CreateRoomResponse(BaseModel):
    code: str
    title: str
    editionId: str
    segmentationId: str
    segmentCount: int
    name: str
    #: The edition shown beneath the source, or null when the text has no aligned
    #: translation.
    translationEditionId: str | None = None


class RoomResponse(BaseModel):
    code: str
    editionId: str
    segmentationId: str
    title: str
    currentSegmentId: str | None
    isLive: bool
    translationEditionId: str | None = None
    name: str | None = None
    startsOn: str | None = None
    endsOn: str | None = None


@router.get("/texts", response_model=list[TextResult])
async def search_texts(
    request: Request,
    title: str = "",
    limit: int = 20,
    user: User = Depends(current_user),
):
    """Search the library. This is how a teacher picks a text.

    Live, against the library. There is no local catalogue to go stale, and nothing is
    stored until a room is actually created.
    """
    try:
        results = await request.app.state.library.search_texts(
            title, limit=min(limit, 100)
        )
    except LibraryError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return [
        TextResult(id=r.id, title=r.title, language=r.language) for r in results
    ]


@router.post("/rooms", response_model=CreateRoomResponse, status_code=201)
async def post_room(
    request: Request, body: CreateRoomRequest, user: User = Depends(current_user)
):
    """Open a room, registering its text if this is the first time anyone has.

    Needs a login: the account that creates a class is its teacher, and is remembered as
    its first member. The shared password is compared in constant time. It permits
    *creating* a room and nothing more.
    """
    app = request.app
    if not password_matches(body.hostPassword, app.state.config.host_password):
        raise HTTPException(status_code=401, detail="Wrong host password")

    try:
        context = await app.state.library.resolve(body.textId)
        stored = await app.state.database.get_text(context.segmentation_id)
        if stored is None or stored.segment_count == 0:
            segment_ids = await app.state.library.segment_ids(context.edition_id)
            if not segment_ids:
                raise HTTPException(
                    status_code=422,
                    detail="That text has no segments, so there is nothing to teach",
                )
            # By id. Searching by title with an id matches nothing and silently
            # stores the id as the title — and register_text never re-syncs, so that
            # name would stick to the text forever.
            text = await app.state.library.fetch_text(body.textId)
            title = text.title if text else body.textId
            language = text.language if text else None
            stored = await app.state.database.register_text(
                context, segment_ids, title=title, language=language
            )
    except LibraryError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    try:
        # Resolved once, here, and pinned to the room. A failure to find one is not a
        # failure to teach: the room simply shows the source alone.
        translation_edition_id = await app.state.library.resolve_translation(
            body.textId, context.edition_id
        )
    except LibraryError:
        translation_edition_id = None

    try:
        created = await create_room(
            app.state.database.pool,
            context.segmentation_id,
            translation_edition_id,
            host=user,
            name=body.name,
            starts_on=body.startsOn,
            ends_on=body.endsOn,
        )
    except RoomError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    return CreateRoomResponse(
        code=created.code,
        title=stored.title,
        editionId=context.edition_id,
        segmentationId=context.segmentation_id,
        segmentCount=stored.segment_count,
        name=(body.name or "").strip() or stored.title,
        translationEditionId=translation_edition_id,
    )


@router.get("/rooms/{code}", response_model=RoomResponse)
async def get_room_route(
    request: Request, code: str, user: User = Depends(limit_code_attempts)
):
    room = await get_room(request.app.state.database.pool, code)
    if room is None:
        raise HTTPException(status_code=404, detail="No such room")
    return RoomResponse(
        code=room.code,
        editionId=room.edition_id,
        segmentationId=room.segmentation_id,
        title=room.title,
        currentSegmentId=room.current_segment_id,
        isLive=room.is_live,
        translationEditionId=room.translation_edition_id,
        name=room.name,
        startsOn=_iso(room.starts_on),
        endsOn=_iso(room.ends_on),
    )


# ---------------------------------------------------------------------------------------
# Classes and members. All of these need a login.
# ---------------------------------------------------------------------------------------


class ClassResponse(BaseModel):
    code: str
    name: str
    title: str
    role: str
    memberCount: int
    lastSeenAt: str | None
    startsOn: str | None = None
    endsOn: str | None = None
    #: The student left this class; it stays in their list and they can rejoin without the code.
    left: bool = False


class JoinResponse(RoomResponse):
    #: What this account is in this class: `teacher` for the creator, else `student`.
    role: str


class MemberResponse(BaseModel):
    id: str
    username: str
    name: str
    role: str
    joinedAt: str | None
    lastSeenAt: str | None


class ChangeCodeResponse(BaseModel):
    code: str


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


async def _room_or_404(request: Request, code: str):
    room = await get_room(request.app.state.database.pool, code)
    if room is None:
        raise HTTPException(status_code=404, detail="No such class")
    return room


async def _host_room(request: Request, code: str, user: User):
    """The room, if this account is its teacher.

    404 for a room that does not exist and 403 for one you do not teach. Telling them apart
    leaks only that a code exists, which `GET /api/rooms/{code}` already says.
    """
    room = await _room_or_404(request, code)
    if not is_host(room, user):
        raise HTTPException(status_code=403, detail="Only this class's teacher can do that")
    return room


async def _remove_and_kick(request: Request, room, user_id: str) -> bool:
    """Take a student out of a class and out of the live class, on every tab they have open.

    Without the kick a removal would only take effect the next time they join. Shared by a
    teacher removing someone and a student leaving, so the two cannot drift apart.
    """
    removed = await remove_member(request.app.state.database.pool, room.id, user_id)
    if removed:
        live = getattr(request.app.state, "live", None)
        if live is not None:
            await live.kick(room, user_id)
    return removed


@router.get("/me/classes", response_model=list[ClassResponse])
async def my_classes(request: Request, user: User = Depends(current_user)):
    """The classes this account is in, most recently visited first.

    What the code screen shows, so the code is entered once. Archived classes are left
    out.
    """
    classes = await classes_for(request.app.state.database.pool, user.id)
    return [
        ClassResponse(
            code=c.code, name=c.name, title=c.title, role=c.role,
            memberCount=c.member_count, lastSeenAt=_iso(c.last_seen_at),
            startsOn=_iso(c.starts_on), endsOn=_iso(c.ends_on), left=c.left,
        )
        for c in classes
    ]


@router.post("/rooms/{code}/join", response_model=JoinResponse)
async def join_room(
    request: Request, code: str, user: User = Depends(limit_code_attempts)
):
    """Become a member of a class. Safe to repeat.

    The one step that needs the code. After it the class is in the account's list and the
    code is not needed again, on any device.
    """
    room = await _room_or_404(request, code)
    if not room.is_live:
        raise HTTPException(status_code=410, detail="That class has been archived")
    role = await add_member(request.app.state.database.pool, room, user)
    return JoinResponse(
        code=room.code,
        editionId=room.edition_id,
        segmentationId=room.segmentation_id,
        title=room.title,
        currentSegmentId=room.current_segment_id,
        isLive=room.is_live,
        translationEditionId=room.translation_edition_id,
        name=room.name,
        startsOn=_iso(room.starts_on),
        endsOn=_iso(room.ends_on),
        role=role,
    )


@router.get("/rooms/{code}/members", response_model=list[MemberResponse])
async def get_members(request: Request, code: str, user: User = Depends(current_user)):
    """Who is in the class. The teacher's view."""
    room = await _host_room(request, code, user)
    members = await list_members(request.app.state.database.pool, room.id)
    return [
        MemberResponse(
            id=m.user_id, username=m.username, name=m.name, role=m.role,
            joinedAt=_iso(m.joined_at), lastSeenAt=_iso(m.last_seen_at),
        )
        for m in members
    ]


@router.delete("/rooms/{code}/members/{user_id}", status_code=204)
async def delete_member(
    request: Request, code: str, user_id: str, user: User = Depends(current_user)
):
    """Take someone out of the class. They can rejoin with the code, so a teacher who
    wants them gone for good also changes the code."""
    room = await _host_room(request, code, user)
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="The teacher cannot be removed")
    if not await _remove_and_kick(request, room, user_id):
        raise HTTPException(status_code=404, detail="No such member")


@router.post("/rooms/{code}/leave", status_code=204)
async def post_leave(request: Request, code: str, user: User = Depends(current_user)):
    """A student steps out of the class. It stays in their list, and they can rejoin it
    without the code; their notes are kept. The teacher cannot leave: ending the session or
    archiving the class is how they finish. (A teacher *removing* someone is different: that
    deletes the membership, and the code is needed again.)"""
    room = await _room_or_404(request, code)
    if is_host(room, user):
        raise HTTPException(status_code=400, detail="The teacher cannot leave their own class")
    pool = request.app.state.database.pool
    if not await leave_member(pool, room.id, user.id):
        raise HTTPException(status_code=404, detail="You are not in this class")
    live = getattr(request.app.state, "live", None)
    if live is not None:
        await live.kick(room, user.id, "member:left")


@router.post("/rooms/{code}/change-code", response_model=ChangeCodeResponse)
async def post_change_code(
    request: Request, code: str, user: User = Depends(current_user)
):
    """Give the class a new code. The old one stops working immediately."""
    room = await _host_room(request, code, user)
    if not room.is_live:
        raise HTTPException(status_code=410, detail="That class has been archived")
    try:
        new_code = await change_code(request.app.state.database.pool, room)
    except RoomError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    return ChangeCodeResponse(code=new_code)


class DatesIn(BaseModel):
    """The days the class is held. `null` clears one (or both)."""

    startsOn: date | None = None
    endsOn: date | None = None

    @model_validator(mode="after")
    def _in_order(self):
        if self.startsOn and self.endsOn and self.endsOn < self.startsOn:
            raise ValueError("The class cannot end before it starts")
        return self


@router.put("/rooms/{code}/dates", status_code=204)
async def put_dates(
    request: Request, code: str, body: DatesIn, user: User = Depends(current_user)
):
    """Say when the class is held (the teacher's). Shown to everyone in it; it never closes
    the class by itself."""
    room = await _host_room(request, code, user)
    if not room.is_live:
        raise HTTPException(status_code=410, detail="That class has been archived")
    await set_class_dates(request.app.state.database.pool, room.id, body.startsOn, body.endsOn)


@router.post("/rooms/{code}/archive", status_code=204)
async def post_archive(request: Request, code: str, user: User = Depends(current_user)):
    """Archive the class: no one can join it any more and it leaves everyone's list.

    For when the course is over. Ending *today's session* is different and leaves the room
    as it is.
    """
    room = await _host_room(request, code, user)
    await close_room(request.app.state.database.pool, room)
    live = getattr(request.app.state, "live", None)
    if live is not None:
        await live.announce_closed(room)


# ---------------------------------------------------------------------------------------
# Notes: a student's private highlights and notes. Students only, for now.
# ---------------------------------------------------------------------------------------

Color = Literal["yellow", "green", "blue", "pink"]
Layer = Literal["source", "translation"]


class NoteIn(BaseModel):
    """A new note: where it is, what it covers, and how it looks."""

    segmentId: str = Field(min_length=1, max_length=128)
    layer: Layer
    startOffset: int = Field(ge=0, le=notes_store.MAX_OFFSET)
    endOffset: int = Field(gt=0, le=notes_store.MAX_OFFSET)
    quote: str = Field(min_length=1, max_length=notes_store.MAX_QUOTE)
    prefix: str = Field(default="", max_length=notes_store.MAX_CONTEXT)
    suffix: str = Field(default="", max_length=notes_store.MAX_CONTEXT)
    color: Color
    body: str | None = Field(default=None, max_length=notes_store.MAX_BODY)

    @model_validator(mode="after")
    def _range_makes_sense(self):
        if self.endOffset <= self.startOffset:
            raise ValueError("A note must cover at least one character")
        # Offsets count code points, as Python's `len` does, and the quote is what lies between
        # them. A request where the two disagree would anchor the note to the wrong words.
        if self.endOffset - self.startOffset != len(self.quote):
            raise ValueError("The offsets do not match the length of the quote")
        return self


class NotePatch(BaseModel):
    """What may change about a note: its colour and its words. Not where it is."""

    color: Color | None = None
    #: `null` clears the note's words, leaving a plain highlight. Leaving the field out
    #: changes nothing.
    body: str | None = Field(default=None, max_length=notes_store.MAX_BODY)


class NoteResponse(BaseModel):
    id: str
    segmentId: str
    layer: str
    startOffset: int
    endOffset: int
    quote: str
    prefix: str
    suffix: str
    color: str
    body: str | None
    createdAt: str | None
    updatedAt: str | None


def _note_out(note: notes_store.Note) -> NoteResponse:
    return NoteResponse(
        id=note.id, segmentId=note.segment_id, layer=note.layer,
        startOffset=note.start, endOffset=note.end, quote=note.quote,
        prefix=note.prefix, suffix=note.suffix, color=note.color, body=note.body,
        createdAt=_iso(note.created_at), updatedAt=_iso(note.updated_at),
    )


async def _notes_room(request: Request, code: str, user: User, *, write: bool):
    """The room, if this account may keep notes in it.

    A member of the class, and a student in it. Everything else is refused, in words that
    say what to do. Writing to an archived class is refused too (410), while reading
    stays allowed: it is the student's own record.
    """
    pool = request.app.state.database.pool
    room = await _room_or_404(request, code)
    member = await get_member(pool, room.id, user.id)
    if member is None:
        raise HTTPException(status_code=403, detail="Join the class first")
    if member.role != notes_store.NOTES_ROLE:
        raise HTTPException(status_code=403, detail="Notes are for students")
    if write and not room.is_live:
        raise HTTPException(status_code=410, detail="That class has been archived")
    if write and member.left_at is not None:
        raise HTTPException(status_code=403, detail="Rejoin the class first")
    return room


@router.get("/rooms/{code}/notes", response_model=list[NoteResponse])
async def get_notes(request: Request, code: str, user: User = Depends(current_user)):
    """My notes in this class. Only ever the caller's own."""
    room = await _notes_room(request, code, user, write=False)
    notes = await notes_store.list_notes(request.app.state.database.pool, room.id, user.id)
    return [_note_out(n) for n in notes]


@router.post("/rooms/{code}/notes", response_model=NoteResponse, status_code=201)
async def post_note(
    request: Request, code: str, body: NoteIn, user: User = Depends(current_user)
):
    room = await _notes_room(request, code, user, write=True)
    database = request.app.state.database

    # A note on a line that is not in this class's text is nonsense, and would be kept
    # forever. Checked, as the socket checks a move.
    if not await database.has_segment(room.segmentation_id, body.segmentId):
        raise HTTPException(status_code=422, detail="That line is not part of this text")
    note = await notes_store.create_note(
        database.pool, room.id, user.id,
        segment_id=body.segmentId, layer=body.layer, start=body.startOffset,
        end=body.endOffset, quote=body.quote, prefix=body.prefix, suffix=body.suffix,
        color=body.color, body=(body.body or "").strip() or None,
    )
    if note is None:
        # The limit is part of the insert itself, so two requests at once cannot both pass.
        raise HTTPException(status_code=422, detail="You have reached the limit of notes for this class")
    return _note_out(note)


@router.patch("/rooms/{code}/notes/{note_id}", response_model=NoteResponse)
async def patch_note(
    request: Request, code: str, note_id: str, patch: NotePatch,
    user: User = Depends(current_user),
):
    room = await _notes_room(request, code, user, write=True)
    # Absent: no change. Present but null or blank: clear the words, leaving a highlight.
    set_body = "body" in patch.model_fields_set
    note = await notes_store.update_note(
        request.app.state.database.pool, room.id, user.id, note_id,
        color=patch.color, set_body=set_body, body=(patch.body or "").strip() or None,
    )
    # "Not found" covers "not yours" too, so an id says nothing about anyone else's notes.
    if note is None:
        raise HTTPException(status_code=404, detail="No such note")
    return _note_out(note)


@router.delete("/rooms/{code}/notes/{note_id}", status_code=204)
async def remove_note(
    request: Request, code: str, note_id: str, user: User = Depends(current_user)
):
    room = await _notes_room(request, code, user, write=True)
    removed = await notes_store.delete_note(
        request.app.state.database.pool, room.id, user.id, note_id
    )
    if not removed:
        raise HTTPException(status_code=404, detail="No such note")
