"""The library client, against a stub.

These test the logic we must not get wrong — which edition is chosen, how a 404 is
read, how paging terminates — without needing the real library. A live check belongs
in `scripts/check_library.py`, not here: CI should not depend on an upstream service.
"""

import httpx
import pytest

from classroom_sync.library import LibraryClient, LibraryError


def client_with(handler) -> LibraryClient:
    """A LibraryClient whose transport is a function, not a network."""
    library = LibraryClient("http://library.test", "app", "key")
    library._client = httpx.AsyncClient(
        base_url="http://library.test",
        transport=httpx.MockTransport(handler),
        headers={"X-Application": "app", "X-API-Key": "key"},
    )
    return library


def json_route(routes: dict):
    """Serve a fixed map of path -> (status, body)."""

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = routes.get(request.url.path, (404, None))
        return httpx.Response(status, json=body)

    return handler


@pytest.mark.asyncio
async def test_resolve_pins_the_first_edition():
    """The first edition is chosen — the behaviour a room then freezes."""
    library = client_with(json_route({
        "/v2/texts/t1/editions": (200, [{"id": "ed-A"}, {"id": "ed-B"}]),
        "/v2/editions/ed-A/segmentation": (
            200, {"id": "sg-A", "edition_id": "ed-A", "text_id": "t1"},
        ),
    }))
    context = await library.resolve("t1")
    assert (context.edition_id, context.segmentation_id) == ("ed-A", "sg-A")
    assert context.text_id == "t1"


@pytest.mark.asyncio
async def test_a_404_on_editions_means_it_was_an_edition_id():
    """Not a text id -> read the argument as an edition id instead."""
    library = client_with(json_route({
        "/v2/editions/ed-X/segmentation": (
            200, {"id": "sg-X", "edition_id": "ed-X", "text_id": "t9"},
        ),
    }))
    context = await library.resolve("ed-X")
    assert context.segmentation_id == "sg-X"
    assert context.text_id == "t9"


@pytest.mark.asyncio
async def test_a_500_is_not_swallowed():
    """Only 404 means "not a text id". A real failure must surface, or an existing
    text gets reported as missing."""
    def handler(request):
        return httpx.Response(500, json={"detail": "upstream on fire"})

    with pytest.raises(LibraryError) as caught:
        await client_with(handler).resolve("t1")
    assert caught.value.status == 500


@pytest.mark.asyncio
async def test_segment_ids_page_until_exhausted():
    """Two pages, then stop. Order is preserved; ids only."""
    pages = [
        (200, {"items": [{"id": "s-1"}, {"id": "s-2"}], "has_more": True}),
        (200, {"items": [{"id": "s-3"}], "has_more": False}),
    ]
    calls = {"n": 0}

    def handler(request):
        status, body = pages[calls["n"]]
        calls["n"] += 1
        return httpx.Response(status, json=body)

    ids = await client_with(handler).segment_ids("ed-A")
    assert ids == ["s-1", "s-2", "s-3"]
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_an_empty_page_terminates_even_if_has_more_lies():
    """Guards the loop against a server that says has_more and sends nothing."""
    def handler(request):
        return httpx.Response(200, json={"items": [], "has_more": True})

    assert await client_with(handler).segment_ids("ed-A") == []


@pytest.mark.asyncio
async def test_search_prefers_the_tibetan_title():
    library = client_with(json_route({
        "/v2/texts": (200, {"items": [
            {"id": "t1", "title": {"en": "Way of the Bodhisattva", "bo": "སྤྱོད་འཇུག"},
             "language": "bo"},
        ]}),
    }))
    results = await library.search_texts("bodhi")
    assert results[0].title == "སྤྱོད་འཇུག"
    assert results[0].id == "t1"


@pytest.mark.asyncio
async def test_an_empty_title_is_omitted_not_sent_blank():
    """The live library answers `title=` with 422, so a blank search must omit it."""
    seen = {}

    def handler(request):
        seen["query"] = str(request.url.query, "utf-8")
        return httpx.Response(200, json={"items": []})

    await client_with(handler).search_texts("")
    assert "title" not in seen["query"]

    await client_with(handler).search_texts("bodhi")
    assert "title=bodhi" in seen["query"]


@pytest.mark.asyncio
async def test_a_text_is_fetched_by_id_not_searched_for_by_title():
    """Regression. `search_texts(text_id)` searches by *title* with an *id*, matches
    nothing, and leaves the caller on its fallback — which is how a room came to be
    titled `TxrvC8Fk4GaCAtnFqBehd`. Since registration never re-syncs, that name
    would have stuck to the text permanently."""
    requested = {}

    def handler(request):
        requested["path"] = request.url.path
        requested["query"] = str(request.url.query, "utf-8")
        return httpx.Response(200, json={
            "id": "t1", "title": {"bo": "སྤྱོད་འཇུག"}, "language": "bo",
        })

    text = await client_with(handler).fetch_text("t1")
    assert text is not None
    assert text.title == "སྤྱོད་འཇུག"
    assert requested["path"] == "/v2/texts/t1"
    assert "title=" not in requested["query"], "must not search by title"


@pytest.mark.asyncio
async def test_a_missing_text_is_none_not_an_error():
    def handler(request):
        return httpx.Response(404, json={"detail": "not found"})

    assert await client_with(handler).fetch_text("nope") is None


@pytest.mark.asyncio
async def test_a_server_error_fetching_a_text_still_raises():
    """Only 404 means "no such text". Swallowing a 5xx would store the id as a title
    during an outage, permanently."""
    def handler(request):
        return httpx.Response(503, json={"detail": "down"})

    with pytest.raises(LibraryError):
        await client_with(handler).fetch_text("t1")


def routes_with_alignment(pairs_forward: bool, pairs_reverse: bool = False):
    """Text t1 (edition ed-S) with one translation text t2 (edition ed-T)."""
    page = {"items": [{"source_segment": {"id": "a"}, "target_segment": {"id": "b"}}]}
    empty = {"items": [], "has_more": False}
    return {
        "/v2/texts/t1": (200, {"id": "t1", "translations": ["t2"]}),
        "/v2/texts/t2/editions": (200, [{"id": "ed-T"}, {"id": "ed-T2"}]),
        "/v2/editions/ed-S/alignments/ed-T": (200, page if pairs_forward else empty),
        "/v2/editions/ed-T/alignments/ed-S": (200, page if pairs_reverse else empty),
    }


@pytest.mark.asyncio
async def test_translation_is_the_first_translations_first_edition():
    library = client_with(json_route(routes_with_alignment(pairs_forward=True)))
    assert await library.resolve_translation("t1", "ed-S") == "ed-T"


@pytest.mark.asyncio
async def test_translation_aligned_only_the_other_way_round_still_counts():
    library = client_with(
        json_route(routes_with_alignment(pairs_forward=False, pairs_reverse=True))
    )
    assert await library.resolve_translation("t1", "ed-S") == "ed-T"


@pytest.mark.asyncio
async def test_translation_with_no_alignment_is_not_chosen():
    """An empty English column on every student's screen is worse than none."""
    library = client_with(json_route(routes_with_alignment(pairs_forward=False)))
    assert await library.resolve_translation("t1", "ed-S") is None


@pytest.mark.asyncio
async def test_text_with_no_translations_gives_none():
    library = client_with(json_route({"/v2/texts/t1": (200, {"id": "t1"})}))
    assert await library.resolve_translation("t1", "ed-S") is None


@pytest.mark.asyncio
async def test_unaligned_translation_is_skipped_for_the_next_one():
    page = {"items": [{"source_segment": {"id": "a"}, "target_segment": {"id": "b"}}]}
    library = client_with(json_route({
        "/v2/texts/t1": (200, {"id": "t1", "translations": ["t2", "t3"]}),
        "/v2/texts/t2/editions": (200, [{"id": "ed-2"}]),
        "/v2/texts/t3/editions": (200, [{"id": "ed-3"}]),
        "/v2/editions/ed-S/alignments/ed-2": (200, {"items": []}),
        "/v2/editions/ed-2/alignments/ed-S": (200, {"items": []}),
        "/v2/editions/ed-S/alignments/ed-3": (200, page),
    }))
    assert await library.resolve_translation("t1", "ed-S") == "ed-3"


@pytest.mark.asyncio
async def test_translation_with_no_editions_is_skipped_not_fatal():
    library = client_with(json_route({
        "/v2/texts/t1": (200, {"id": "t1", "translations": ["t2"]}),
        "/v2/texts/t2/editions": (404, None),
    }))
    assert await library.resolve_translation("t1", "ed-S") is None


def sent_headers(app_name: str) -> dict:
    """The headers a client built with this application name puts on a request."""
    return dict(LibraryClient("http://library.test", app_name, "key")._client.headers)


def test_the_application_header_is_sent_when_a_name_is_set():
    headers = sent_headers("my-app")
    assert headers["x-application"] == "my-app"
    assert headers["x-api-key"] == "key"


def test_the_application_header_is_left_out_when_no_name_is_set():
    """The production library refuses a name it has not registered, but accepts the key."""
    headers = sent_headers("")
    assert "x-application" not in headers
    assert headers["x-api-key"] == "key"


@pytest.mark.asyncio
async def test_a_later_translations_error_does_not_undo_an_earlier_one_that_lines_up():
    page = {"items": [{"source_segment": {"id": "a"}, "target_segment": {"id": "b"}}]}
    library = client_with(json_route({
        "/v2/texts/t1": (200, {"id": "t1", "translations": ["t2", "t3"]}),
        "/v2/texts/t2/editions": (200, [{"id": "ed-2"}]),
        "/v2/texts/t3/editions": (200, [{"id": "ed-3"}]),
        "/v2/editions/ed-S/alignments/ed-2": (200, page),
        # Nothing at all is routed for ed-3: asking about it fails.
    }))
    assert await library.resolve_translation("t1", "ed-S") == "ed-2"


# -- ids go into URL paths, so they must not be able to leave their segment -------------

TRAVERSING_IDS = ["../categories?", "..", "a/b", "a?x=1", "a#b", "%2e%2e", "", "x" * 65, "a b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", TRAVERSING_IDS)
async def test_a_traversing_id_never_reaches_the_library(bad):
    """The service's API key must only be used on the paths the proxy allowlist permits. A
    hostile id is refused before any request is made."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=[])

    library = client_with(handler)
    for call in (
        library.resolve(bad),
        library.resolve_translation(bad, "ed-A"),
        library.segment_ids(bad),
    ):
        with pytest.raises(LibraryError) as caught:
            await call
        assert caught.value.is_not_found
    assert await library.fetch_text(bad) is None
    assert seen == []


@pytest.mark.asyncio
async def test_an_id_the_library_returns_is_checked_too():
    """An edition id read out of a response goes into the next path, so it is held to the
    same rule as one from a client."""
    library = client_with(json_route({
        "/v2/texts/t1/editions": (200, [{"id": "../categories?"}]),
    }))
    with pytest.raises(LibraryError):
        await library.resolve("t1")
