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

import asyncio
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .auth import current_user
from .config import config
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


@dataclass(frozen=True)
class Cached:
    """A library answer, held whole so it can be given to many people."""

    status: int
    headers: dict[str, str]
    body: bytes
    expires: float


class ResponseCache:
    """A small in-memory cache of library answers, shared by every student.

    Two things make fifty students loading a text at the same moment cost the library one
    request: answers are **kept** for a few minutes, and a request that arrives while the same
    one is already on its way **waits for it** instead of sending another (otherwise fifty
    arrive before the first has finished, and nothing is cached yet).

    Only successful answers are kept, so a failure is retried by the next person. Bounded in
    size (one entry, and the total), oldest-used out first, so a few long texts cannot fill the
    container. Held in this process: the service runs one worker.
    """

    def __init__(
        self,
        ttl: float,
        max_entry_bytes: int = 16 * 1024 * 1024,
        max_total_bytes: int = 96 * 1024 * 1024,
        clock=time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_entry_bytes = max_entry_bytes
        self.max_total_bytes = max_total_bytes
        self._clock = clock
        self._entries: OrderedDict[tuple, Cached] = OrderedDict()
        self._total = 0
        self._inflight: dict[tuple, asyncio.Future] = {}

    def get(self, key: tuple) -> Cached | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires <= self._clock():
            self._drop(key)
            return None
        self._entries.move_to_end(key)
        return entry

    def put(self, key: tuple, status: int, headers: dict[str, str], body: bytes) -> None:
        if self.ttl <= 0 or status != 200 or len(body) > self.max_entry_bytes:
            return
        self._drop(key)
        self._entries[key] = Cached(status, headers, body, self._clock() + self.ttl)
        self._total += len(body)
        while self._total > self.max_total_bytes and self._entries:
            self._drop(next(iter(self._entries)))

    def _drop(self, key: tuple) -> None:
        entry = self._entries.pop(key, None)
        if entry is not None:
            self._total -= len(entry.body)

    def clear(self) -> None:
        self._entries.clear()
        self._total = 0

    async def fetch(self, key: tuple, load):
        """The cached answer for `key`, or the result of `load()`, which is run at most once at a
        time per key however many ask. `load` returns `(status, headers, body)`."""
        hit = self.get(key)
        if hit is not None:
            return hit.status, hit.headers, hit.body
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            result = await load()
            self.put(key, *result)
            future.set_result(result)
            return result
        except BaseException as error:
            future.set_exception(error)
            # Whoever was waiting gets the error; if nobody was, it must not be reported twice.
            future.exception()
            raise
        finally:
            self._inflight.pop(key, None)


#: Shared by every request. Sized and timed from the config; see `ResponseCache`.
cache = ResponseCache(ttl=config.library_cache_seconds)


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
    params = dict(request.query_params)

    async def load() -> tuple[int, dict[str, str], bytes]:
        try:
            upstream = await library.stream_get(upstream_path, params)
        except LibraryError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        try:
            # The whole answer, so it can be kept and given to everyone who asks. Bounded by what
            # the library serves (an edition's text is megabytes, not gigabytes), and by the cache's
            # own caps for what is kept.
            body = b"".join([chunk async for chunk in upstream.aiter_raw()])
        finally:
            await upstream.aclose()
        headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() in PASS_THROUGH_HEADERS
        }
        return upstream.status_code, headers, body

    key = (upstream_path, tuple(sorted(params.items())))
    status, headers, body = await cache.fetch(key, load)
    return Response(content=body, status_code=status, headers=headers)
