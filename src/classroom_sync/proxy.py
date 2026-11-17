"""Forwards the reader's library calls, with the API key attached here.

The browser cannot hold the key: anything in a frontend bundle is published. So the
reader asks this service for `/library/v2/...` and this service asks the library,
adding `X-Application` and `X-API-Key` on the way through. The same arrangement
WeBuddhist's nginx uses, for the same reason.

## Why this is an allowlist and not a pass-through

Our key can write. The library has **38 paths accepting POST, PATCH or PUT or DELETE**,
among them `DELETE /v2/texts/{id}`, `PATCH /v2/editions/{id}/content` and
`POST /v2/applications`. A proxy that forwarded whatever it was given would hand every
one of those to anyone who could load the classroom page, authenticated with our key.

So: **GET only, and only the paths the reader actually reads.** Anything else is 403
here and never reaches the library. Adding a path is a deliberate act.
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from .auth import current_user
from .library import LibraryError

# Behind the Academy login. The key this forwards can read the whole library, so it is not for
# anyone who can merely reach the service: only an account that is logged in may use it (the
# reader sends its login with every call).
router = APIRouter(prefix="/library", dependencies=[Depends(current_user)])

#: Opaque library ids: nanoid-shaped, so no slashes and nothing path-traversing.
_ID = r"[A-Za-z0-9_-]{1,64}"

#: Exactly what the reader reads. Each entry exists because a specific part of
#: displaying a text needs it; none of them writes anything.
ALLOWED_PATHS = [
    re.compile(pattern)
    for pattern in (
        # Choosing a text
        rf"^/v2/texts$",
        rf"^/v2/texts/languages$",
        rf"^/v2/texts/{_ID}$",
        rf"^/v2/texts/{_ID}/editions$",
        # Resolving it to lines
        rf"^/v2/editions/{_ID}$",
        rf"^/v2/editions/{_ID}/segmentation$",
        rf"^/v2/editions/{_ID}/segmentation/segments$",
        rf"^/v2/editions/{_ID}/content$",
        rf"^/v2/editions/{_ID}/table-of-contents$",
        # Translations: which lines correspond, and what they say
        rf"^/v2/editions/{_ID}/alignments$",
        rf"^/v2/editions/{_ID}/alignments/{_ID}$",
        rf"^/v2/segments/{_ID}$",
        rf"^/v2/segments/{_ID}/content$",
    )
]

#: Response headers worth passing back. Everything else — the library's cookies, its
#: server banner, its CORS decisions — is the library's business, not the browser's.
#:
#: `content-encoding` is here because the body is streamed with `aiter_raw()`, which
#: does **not** decompress. httpx asks for gzip on every request, so forwarding the
#: length while dropping the encoding would hand the browser compressed bytes labelled
#: `application/json`. The library does not compress today; the day it does, or a CDN
#: that compresses is put in front of it, this is the difference between working and
#: every response failing to parse. `vary` comes along because a cache that sees one
#: without the other will serve the wrong body.
PASS_THROUGH_HEADERS = (
    "content-type",
    "content-length",
    "content-encoding",
    "vary",
    "etag",
    "last-modified",
)


def is_allowed(path: str) -> bool:
    return any(pattern.match(path) for pattern in ALLOWED_PATHS)


@router.get("/{path:path}")
async def proxy(request: Request, path: str) -> Response:
    """Forward one GET to the library.

    Only GET is routed here at all, so a POST to this prefix is a 405 from FastAPI
    before any of this runs.
    """
    upstream_path = "/" + path.lstrip("/")
    if not is_allowed(upstream_path):
        raise HTTPException(
            status_code=403,
            detail="That library path is not proxied. See proxy.ALLOWED_PATHS.",
        )

    library = request.app.state.library
    try:
        upstream = await library.stream_get(upstream_path, dict(request.query_params))
    except LibraryError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower() in PASS_THROUGH_HEADERS
    }

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    # Streamed rather than buffered: an edition's content is the whole text, and
    # holding a few of those in memory at once is how a small container dies.
    return StreamingResponse(
        body(), status_code=upstream.status_code, headers=headers
    )
