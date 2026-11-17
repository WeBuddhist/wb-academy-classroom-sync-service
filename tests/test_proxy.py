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
