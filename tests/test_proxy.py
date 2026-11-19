"""The library proxy's allowlist.

This is a security boundary, not a routing convenience. Our API key can write: the
library has 38 paths accepting POST, PATCH, PUT or DELETE, including
`DELETE /v2/texts/{id}` and `POST /v2/applications`. Whatever this proxy forwards, it
forwards *authenticated as us*.

So these tests are mostly about what must NOT get through.
"""

import pytest

from classroom_sync.proxy import ALLOWED_PATHS, is_allowed


@pytest.mark.parametrize(
    "path",
    [
        "/v2/texts",
        "/v2/texts/languages",
        "/v2/texts/zz2kQS2yo7ODxYpJRfyFg",
        "/v2/texts/zz2kQS2yo7ODxYpJRfyFg/editions",
        "/v2/editions/q9g7rNhrEi8jcJR1eKkiL",
        "/v2/editions/q9g7rNhrEi8jcJR1eKkiL/segmentation",
        "/v2/editions/q9g7rNhrEi8jcJR1eKkiL/segmentation/segments",
        "/v2/editions/q9g7rNhrEi8jcJR1eKkiL/content",
        "/v2/editions/a1/alignments",
        "/v2/editions/a1/alignments/b2",
        "/v2/segments/s5Xqqb8S2Y1EINzgnKa2P",
        "/v2/segments/s5Xqqb8S2Y1EINzgnKa2P/content",
    ],
)
def test_the_reader_can_read_what_it_needs(path):
    assert is_allowed(path)


@pytest.mark.parametrize(
    "path, why",
    [
        ("/v2/applications", "creating applications is not the reader's business"),
        ("/v2/categories", "not needed, and it accepts POST upstream"),
        ("/v2/texts/abc/tags/xyz", "tagging writes"),
        ("/v2/editions/abc/durchens", "scholarly annotations are out of scope"),
        ("/v2/editions/abc/yigchungs", "out of scope"),
        ("/v2/editions/abc/footnotes", "out of scope"),
        ("/v2/editions/abc/pagination", "out of scope"),
        ("/v2/recordings/abc/edition", "out of scope"),
        ("/v2/audios/abc", "out of scope"),
        ("/v2/editions/abc/recordings", "out of scope"),
    ],
)
def test_everything_else_is_refused(path, why):
    assert not is_allowed(path), why


@pytest.mark.parametrize(
    "path",
    [
        "/../v2/texts",
        "/v2/texts/../../admin",
        "/v2/texts/..%2F..%2Fadmin",
        "/v2/editions/a/../../../v2/applications",
        "//v2/texts",
        "/v2/texts/abc/",          # trailing slash is a different upstream path
        "/V2/texts",               # case matters upstream
        "",
    ],
)
def test_nothing_sneaks_past_the_patterns(path):
    assert not is_allowed(path)


def test_ids_cannot_contain_a_slash():
    """An id is matched as an opaque token. If it could contain `/`, a crafted id
    would reach a path the allowlist never approved."""
    assert not is_allowed("/v2/texts/abc/def")
    assert not is_allowed("/v2/editions/abc/def/content")


def test_every_pattern_is_anchored_at_both_ends():
    """An unanchored pattern would let `/v2/texts/x/delete-everything` match the
    `/v2/texts/{id}` entry. Cheap to assert, and the failure mode is severe."""
    for pattern in ALLOWED_PATHS:
        assert pattern.pattern.startswith("^")
        assert pattern.pattern.endswith("$")


def test_query_strings_are_not_part_of_the_path_match():
    """Params are forwarded separately; a `?` in the path means something is wrong."""
    assert not is_allowed("/v2/texts?title=x")


def test_content_encoding_is_passed_through():
    """Regression. The body is streamed with aiter_raw(), which does not decompress,
    and httpx asks for gzip on every request. Forwarding content-length while
    dropping content-encoding hands the browser compressed bytes labelled
    application/json, and every response fails to parse."""
    from classroom_sync.proxy import PASS_THROUGH_HEADERS

    assert "content-encoding" in PASS_THROUGH_HEADERS
    assert "content-length" in PASS_THROUGH_HEADERS, "both, or neither"
    assert "vary" in PASS_THROUGH_HEADERS, "a cache needs it alongside the encoding"


# -- who may use it -------------------------------------------------------------------


def test_the_proxy_and_the_text_search_need_a_login():
    from fastapi.testclient import TestClient

    from classroom_sync.app import api
    from classroom_sync.auth import Authenticator
    from tests.test_auth import AUDIENCE, ISSUER, jwk_set

    api.state.authenticator = Authenticator(jwk_set(), ISSUER, AUDIENCE)
    try:
        client = TestClient(api)
        assert client.get("/library/v2/texts").status_code == 401
        assert client.get("/api/texts?title=x").status_code == 401
    finally:
        api.state.authenticator = None


# -- the cache --------------------------------------------------------------------------

import asyncio  # noqa: E402

import httpx  # noqa: E402

from classroom_sync import proxy  # noqa: E402
from classroom_sync.app import api  # noqa: E402
from classroom_sync.auth import Authenticator  # noqa: E402
from classroom_sync.library import LibraryError  # noqa: E402
from tests.test_auth import AUDIENCE, ISSUER, jwk_set, token  # noqa: E402


class FakeUpstream:
    def __init__(self, body: bytes, status: int = 200):
        self.status_code = status
        self.headers = {"content-type": "application/json", "set-cookie": "no"}
        self._body = body

    async def aiter_raw(self):
        yield self._body[:3]
        yield self._body[3:]

    async def aclose(self):
        pass


class FakeLibrary:
    def __init__(self, body=b'{"ok": true}', status=200, delay=0.0):
        self.calls: list[tuple] = []
        self.body, self.status, self.delay, self.fail = body, status, delay, False

    async def stream_get(self, path, params=None):
        self.calls.append((path, tuple(sorted((params or {}).items()))))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise LibraryError("down")
        return FakeUpstream(self.body, self.status)


@pytest.fixture
def library():
    proxy.cache.clear()
    proxy.cache.ttl = 600
    fake = FakeLibrary()
    real = getattr(api.state, "library", None)
    api.state.library = fake
    api.state.authenticator = Authenticator(jwk_set(), ISSUER, AUDIENCE)
    yield fake
    api.state.library = real
    api.state.authenticator = None
    proxy.cache.clear()


def get(path):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api), base_url="http://t",
            headers={"authorization": "JWT " + token()},
        ) as client:
            return await client.get(path)
    return run


@pytest.mark.asyncio
async def test_the_second_request_is_served_from_memory(library):
    first = await get("/library/v2/editions/e1/content")()
    second = await get("/library/v2/editions/e1/content")()
    assert first.status_code == second.status_code == 200
    assert first.content == second.content == b'{"ok": true}'
    assert len(library.calls) == 1


@pytest.mark.asyncio
async def test_fifty_students_at_once_cost_the_library_one_request(library):
    library.delay = 0.1
    responses = await asyncio.gather(*[get("/library/v2/editions/e1/content")() for _ in range(50)])
    assert all(r.status_code == 200 and r.content == b'{"ok": true}' for r in responses)
    assert len(library.calls) == 1


@pytest.mark.asyncio
async def test_a_different_path_or_query_is_a_different_answer(library):
    await get("/library/v2/editions/e1/content")()
    await get("/library/v2/editions/e2/content")()
    await get("/library/v2/editions/e1/segmentation/segments?limit=500&offset=0")()
    await get("/library/v2/editions/e1/segmentation/segments?limit=500&offset=500")()
    assert len(library.calls) == 4


@pytest.mark.asyncio
async def test_a_failure_is_passed_on_to_everyone_waiting_and_not_remembered(library):
    library.fail = True
    library.delay = 0.05
    responses = await asyncio.gather(*[get("/library/v2/texts")() for _ in range(5)])
    assert [r.status_code for r in responses] == [502] * 5
    library.fail = False
    assert (await get("/library/v2/texts")()).status_code == 200


@pytest.mark.asyncio
async def test_the_librarys_own_errors_are_not_remembered(library):
    library.status = 404
    assert (await get("/library/v2/texts/zz")()).status_code == 404
    library.status = 200
    assert (await get("/library/v2/texts/zz")()).status_code == 200
    assert len(library.calls) == 2


@pytest.mark.asyncio
async def test_only_the_headers_worth_passing_on_are_passed(library):
    response = await get("/library/v2/texts")()
    assert response.headers["content-type"] == "application/json"
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_a_cache_of_zero_seconds_keeps_nothing(library):
    proxy.cache.ttl = 0
    await get("/library/v2/texts")()
    await get("/library/v2/texts")()
    assert len(library.calls) == 2


def test_an_answer_is_forgotten_after_its_time():
    now = [0.0]
    cache = proxy.ResponseCache(ttl=10, clock=lambda: now[0])
    cache.put(("k",), 200, {}, b"abc")
    assert cache.get(("k",)).body == b"abc"
    now[0] = 11
    assert cache.get(("k",)) is None


def test_the_cache_stays_within_its_size():
    cache = proxy.ResponseCache(ttl=10, max_entry_bytes=5, max_total_bytes=8)
    cache.put(("big",), 200, {}, b"123456")      # over the per-entry limit: not kept
    assert cache.get(("big",)) is None
    cache.put(("a",), 200, {}, b"1234")
    cache.put(("b",), 200, {}, b"5678")
    cache.put(("c",), 200, {}, b"9012")          # over the total: the oldest goes
    assert cache.get(("a",)) is None
    assert cache.get(("b",)) is not None and cache.get(("c",)) is not None
