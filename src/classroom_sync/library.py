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

import asyncio
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
                # The key, attached here, server-side, and never reaching a browser —
                # which is the reason the reader's library calls are proxied through
                # this service at all.
                "X-API-Key": api_key,
                # The application name is sent only when one is set. Each library
                # deployment keeps its own list of applications: our name is registered
                # on the staging library but not on production, where sending it is
                # refused with 401 "API key not authorized for this application" while
                # the same key without the header is accepted. Checked against both.
                **({"X-Application": app_name} if app_name else {}),
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

    async def resolve_translation(
        self, text_id: str, source_edition_id: str
    ) -> str | None:
        """The edition of the text's first translation that lines up with the source.

        Returns None when there is nothing to show beneath the source, which is an
        ordinary outcome: many texts have no translation, and some translations are
        not aligned to this edition. A class then shows the source alone.

        "First" follows the order the library lists `translations` in, and the first
        edition of each, matching the rule used for the source. A translation with no
        alignment to this edition is skipped rather than chosen, since choosing it
        would put an empty column on every student's screen.

        Only alignment *existence* is checked here, in either direction (the reader
        accepts both). The pairs themselves are read by the browser, so this service
        still handles ids and nothing else.
        """
        text = await self._get(f"/v2/texts/{text_id}")
        translation_ids = (text or {}).get("translations") or []

        async def first_edition(translation_id: str) -> str | None:
            try:
                editions = await self._get(
                    f"/v2/texts/{translation_id}/editions",
                    {"edition_type": EDITION_TYPE},
                )
            except LibraryError as error:
                if error.is_not_found:
                    return None
                raise
            return editions[0]["id"] if editions else None

        # Every translation's edition, and then every alignment check, at once: these are
        # independent requests, and this runs inside the request that creates a class, which
        # was waiting on them one after another. The answer is still the first aligned one in
        # the library's own order, since the results keep that order.
        edition_ids = [
            edition_id
            for edition_id in await asyncio.gather(*(first_edition(t) for t in translation_ids))
            if edition_id
        ]
        # A check that fails counts as "not aligned" rather than sinking the rest: a later
        # translation's error must not undo an earlier one that did line up, which is what
        # checking them in order used to guarantee by never reaching the later one.
        aligned = await asyncio.gather(
            *(self._aligned(source_edition_id, edition_id) for edition_id in edition_ids),
            return_exceptions=True,
        )
        return next((e for e, ok in zip(edition_ids, aligned) if ok is True), None)

    async def _aligned(self, source_edition_id: str, other_edition_id: str) -> bool:
        """Whether any alignment pair exists between two editions, either way round."""

        # One direction after the other, and the second only if the first found nothing: the
        # library may answer an error for the direction it does not store, which must not
        # count against an alignment the first one already showed.
        for first, second in (
            (source_edition_id, other_edition_id),
            (other_edition_id, source_edition_id),
        ):
            page = await self._get(
                f"/v2/editions/{first}/alignments/{second}",
                {"limit": 1, "offset": 0},
            )
            if (page or {}).get("items"):
                return True
        return False

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
