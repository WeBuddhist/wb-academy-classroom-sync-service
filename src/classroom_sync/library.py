"""The OpenPecha library, as this service needs it.

Four calls, in one chain: find a text by title, resolve it to an edition and a
segmentation, then read that segmentation's segment ids.

**This service reads ids. It never reads content.** The reader fetches the words from
the library itself, in the browser. Nothing here returns a word of any text, and
nothing here should be extended to.

Mirrors the resolution logic of the WeBuddhist reader
(`src/services/library/textDetails.ts` at commit 80ed57cd), deliberately and closely,
because both sides must land on the same segmentation for the same text or a room's ids
mean nothing to the reader showing them.
"""

from dataclasses import dataclass

import httpx

#: The library pages its segment listing. 500 is what the reader uses.
SEGMENT_PAGE_SIZE = 500

#: Editions come in types; the reader asks for critical editions and so do we.
EDITION_TYPE = "critical"


class LibraryError(Exception):
    """A call to the library failed."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_not_found(self) -> bool:
        return self.status == 404


@dataclass(frozen=True)
class TextContext:
    """What a room needs to pin, resolved once.

    `segmentation_id` is what segment ids belong to; `edition_id` is what the reader can
    resolve and fetch from. Both travel in `RoomState` — see docs/socket-contract.md.
    """

    edition_id: str
    text_id: str
    segmentation_id: str


@dataclass(frozen=True)
class TextSummary:
    """A search result, named well enough to choose between."""

    id: str
    title: str
    language: str


class LibraryClient:
    """Talks to the library. One instance per process; close it on shutdown."""

    def __init__(
        self,
        base_url: str,
        app_name: str,
        api_key: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        if not base_url:
            raise ValueError("LIBRARY_BASE_URL is not set")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "accept": "application/json",
                # The two headers the library requires. They are attached here,
                # server-side, and never reach a browser — which is the reason the
                # reader's library calls are proxied through this service at all.
                "X-Application": app_name,
                "X-API-Key": api_key,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None):
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise LibraryError(
                f"Library returned {error.response.status_code} for {path}",
                error.response.status_code,
            ) from error
        except httpx.HTTPError as error:
            raise LibraryError(f"Library request failed for {path}") from error
        return response.json()

    async def stream_get(self, path: str, params: dict | None = None):
        """Start a GET and return the open response, for the proxy to stream.

        The caller owns the response and must close it. Unlike `_get` this does not
        raise on a 4xx or 5xx: the proxy passes the library's own status back to the
        browser rather than flattening everything to a 502, so a 404 for a missing
        text reads as a 404.
        """
        request = self._client.build_request("GET", path, params=params)
        try:
            return await self._client.send(request, stream=True)
        except httpx.HTTPError as error:
            raise LibraryError(f"Library request failed for {path}") from error

    # -- the four calls ----------------------------------------------------

    async def search_texts(
        self, title: str = "", *, limit: int = 20, offset: int = 0
    ) -> list[TextSummary]:
        """Find texts whose title matches. This is how a teacher picks one.

        An empty title lists everything. It must be *omitted*, not sent empty: the
        library answers `title=` with a 422, so a blank search box would error rather
        than list — checked against the live API, not assumed.
        """
        params: dict = {"limit": limit, "offset": offset}
        if title:
            params["title"] = title
        page = await self._get("/v2/texts", params)
        return [
            TextSummary(
                id=item["id"],
                title=_title_of(item),
                language=item.get("language", ""),
            )
            for item in page.get("items", [])
        ]

    async def fetch_text(self, text_id: str) -> TextSummary | None:
        """One text by id.

        Not `search_texts(text_id)`. That searches the library by *title* with an
        *id*, matches nothing, and leaves the caller quietly using a fallback — which
        is exactly how a room came to be titled `TxrvC8Fk4GaCAtnFqBehd`.
        """
        try:
            item = await self._get(f"/v2/texts/{text_id}")
        except LibraryError as error:
            if error.is_not_found:
                return None
            raise
        if not item:
            return None
        return TextSummary(
            id=item.get("id", text_id),
            title=_title_of(item),
            language=item.get("language", ""),
        )

    async def resolve(self, text_or_edition_id: str) -> TextContext:
        """Resolve a text id into the edition and segmentation its ids come from.

        Ask the text endpoint first: every caller here passes a text id, and for an
        edition id the library answers 200 with an empty list rather than failing,
        which is what tells us an edition id was passed instead.

        A 404 only means "not a text id", so it falls through to reading the argument
        as an edition id. Anything else — the network, a 5xx, a bad key — is a real
        error and must not be swallowed, or an existing text gets reported as missing.
        """
        try:
            editions = await self._get(
                f"/v2/texts/{text_or_edition_id}/editions",
                {"edition_type": EDITION_TYPE},
            )
        except LibraryError as error:
            if not error.is_not_found:
                raise
            editions = None

        # NOTE: the first edition. The library does not promise this ordering is
        # stable, which is exactly why a room stores what this resolved rather than
        # re-resolving per student. See docs/socket-contract.md.
        edition_id = (editions or [{}])[0].get("id") or text_or_edition_id

        segmentation = await self._get(f"/v2/editions/{edition_id}/segmentation")
        if not segmentation:
            raise LibraryError(
                f"No segmentation found for '{text_or_edition_id}'", 404
            )

        return TextContext(
            edition_id=segmentation.get("edition_id") or edition_id,
            text_id=segmentation.get("text_id") or text_or_edition_id,
            segmentation_id=segmentation["id"],
        )

    async def segment_ids(self, edition_id: str) -> list[str]:
        """Every segment id of an edition's segmentation, in reading order.

        Paged, because a long text runs to thousands of segments. Order is the order
        the library returns; the caller numbers them, and that numbering is what
        `segments.idx` stores.

        Returns ids only. The spans carry line offsets and the library has a separate
        content endpoint; neither is read here.
        """
        ids: list[str] = []
        offset = 0
        while True:
            page = await self._get(
                f"/v2/editions/{edition_id}/segmentation/segments",
                {"limit": SEGMENT_PAGE_SIZE, "offset": offset},
            )
            items = page.get("items") or []
            ids.extend(item["id"] for item in items)
            if not page.get("has_more") or not items:
                return ids
            offset += len(items)


def _title_of(item: dict) -> str:
    """Pick a display title out of the library's localized title object.

    Bo first — these are Tibetan texts and that is the name a teacher recognises —
    then English, then whatever is there. Display only; never parsed.
    """
    title = item.get("title")
    if isinstance(title, str):
        return title
    if isinstance(title, dict):
        for key in ("bo", "en"):
            if title.get(key):
                return title[key]
        for value in title.values():
            if isinstance(value, str) and value:
                return value
    return item.get("id", "")
