"""The HTTP API.

Four things a client does before any socket is open: find a text, open a room on it,
look a room up, and check a recovery code.

Field names are **camelCase on the wire**, matching the client. The temptation to write
`segment_id` is the single most likely way to break this service — see
docs/socket-contract.md.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .library import LibraryError
from .rooms import (
    HOST_OK,
    HOST_ROOM_ENDED,
    RoomError,
    create_room,
    get_room,
    password_matches,
    verify_host,
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
    textId: str = Field(min_length=1)


class CreateRoomResponse(BaseModel):
    code: str
    #: Shown once. Only its hash is stored, so this cannot be recovered from the
    #: database — it is the teacher's to keep.
    hostToken: str
    title: str
    editionId: str
    segmentationId: str
    segmentCount: int


class RoomResponse(BaseModel):
    code: str
    editionId: str
    segmentationId: str
    title: str
    currentSegmentId: str | None
    isLive: bool


class VerifyHostRequest(BaseModel):
    hostToken: str = Field(default="")


@router.get("/texts", response_model=list[TextResult])
async def search_texts(request: Request, title: str = "", limit: int = 20):
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
async def post_room(request: Request, body: CreateRoomRequest):
    """Open a room, registering its text if this is the first time anyone has.

    The shared password is compared in constant time. It permits *creating* a room and
    nothing more — driving one needs the per-room host token returned here.
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
        created = await create_room(app.state.database.pool, context.segmentation_id)
    except RoomError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    return CreateRoomResponse(
        code=created.code,
        hostToken=created.host_token,
        title=stored.title,
        editionId=context.edition_id,
        segmentationId=context.segmentation_id,
        segmentCount=stored.segment_count,
    )


@router.get("/rooms/{code}", response_model=RoomResponse)
async def get_room_route(request: Request, code: str):
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
    )


@router.post("/rooms/{code}/verify-host", status_code=200)
async def post_verify_host(request: Request, code: str, body: VerifyHostRequest):
    """Check a recovery code *before* the teacher acts on it.

    A teacher restoring a room on another device otherwise learns their code is wrong
    only when their first tap is refused — mid-class, too late to do anything.
    """
    outcome = await verify_host(
        request.app.state.database.pool, code, body.hostToken
    )
    if outcome == HOST_ROOM_ENDED:
        # 410 Gone, not 200. The code is right, but the class is over — telling a
        # returning teacher they are the host of a dead room sends them off to teach
        # into a room nobody can join.
        raise HTTPException(status_code=410, detail="That class has ended")
    if outcome != HOST_OK:
        raise HTTPException(status_code=401, detail="Wrong recovery code")
    return {"ok": True}
