"""The Academy login check.

Tokens here are signed with a key generated for the test, so these exercise the real
verification — signature, issuer, audience, expiry, algorithm — without an LMS. A live
check against a token the real LMS minted is in `scripts/check_auth.py`.

Mostly about what must be **refused**: this is what stands between a stranger and a
class's controls.
"""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from classroom_sync.app import api
from classroom_sync.auth import (
    COOKIE_HEADER_PAYLOAD,
    COOKIE_SIGNATURE,
    AuthError,
    Authenticator,
    User,
)

ISSUER = "http://lms.test/oauth2"
AUDIENCE = "openedx"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk_set(key=KEY, kid="openedx") -> str:
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = kid
    return json.dumps({"keys": [jwk]})


def token(key=KEY, kid="openedx", alg="RS256", **overrides) -> str:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": int(time.time()) + 3600,
        "sub": "abc123",
        "preferred_username": "tenzin",
        "name": "Tenzin K",
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    headers = {"kid": kid} if kid else {}
    return jwt.encode(claims, key, algorithm=alg, headers=headers)


@pytest.fixture
def authenticator() -> Authenticator:
    return Authenticator(jwk_set(), ISSUER, AUDIENCE)


def test_a_good_token_names_the_user(authenticator):
    assert authenticator.verify(token()) == User(id="abc123", username="tenzin", name="Tenzin K")


def test_a_person_is_identified_by_the_stable_id_not_the_username(authenticator):
    one = authenticator.verify(token(preferred_username="old-name"))
    two = authenticator.verify(token(preferred_username="new-name"))
    assert one.id == two.id


def test_the_algorithm_open_edx_really_uses_is_accepted(authenticator):
    """Found by checking a token the real LMS issued: RS512, not RS256."""
    assert authenticator.verify(token(alg="RS512")).username == "tenzin"


def test_a_token_with_no_key_id_is_checked_against_the_platform_key(authenticator):
    """Open edX's login cookies carry no `kid`. Also checked against a real one."""
    assert authenticator.verify(token(kid=None, alg="RS512")).username == "tenzin"


def test_a_keyless_token_signed_by_a_stranger_is_still_refused(authenticator):
    with pytest.raises(AuthError):
        authenticator.verify(token(key=OTHER_KEY, kid=None, alg="RS512"))


@pytest.mark.parametrize(
    "why, bad",
    [
        ("signed with someone else's key", lambda: token(key=OTHER_KEY)),
        ("expired", lambda: token(exp=int(time.time()) - 10)),
        ("from another issuer", lambda: token(iss="http://evil.test/oauth2")),
        ("for another audience", lambda: token(aud="someone-else")),
        ("naming a key we do not hold", lambda: token(kid="not-ours")),
        ("with no subject", lambda: token(sub=None)),
        ("with no username", lambda: token(preferred_username=None)),
        ("with no expiry", lambda: token(exp=None)),
        ("not a token at all", lambda: "not.a.token"),
        ("empty", lambda: ""),
    ],
)
def test_a_bad_token_is_refused(authenticator, why, bad):
    with pytest.raises(AuthError):
        authenticator.verify(bad())


def test_a_token_cannot_choose_its_own_algorithm(authenticator):
    """The classic forgery: sign with HMAC, using the *public* key as the secret.

    Built by hand, because PyJWT itself refuses to produce it.
    """
    import base64
    import hashlib
    import hmac

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    public_key = jwk_set().encode()  # anything the attacker can know
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "openedx"}).encode())
    payload = b64(json.dumps({
        "iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 3600,
        "sub": "x", "preferred_username": "admin",
    }).encode())
    signature = b64(hmac.new(public_key, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    with pytest.raises(AuthError):
        authenticator.verify(f"{header}.{payload}.{signature}")


def test_an_unsigned_token_is_refused(authenticator):
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "exp": int(time.time()) + 3600,
         "sub": "x", "preferred_username": "admin"},
        None,
        algorithm="none",
        headers={"kid": "openedx"},
    )
    with pytest.raises(AuthError):
        authenticator.verify(unsigned)


@pytest.mark.parametrize("args", [("", ISSUER, AUDIENCE), (jwk_set(), "", AUDIENCE), (jwk_set(), ISSUER, "")])
def test_it_will_not_start_half_configured(args):
    with pytest.raises(ValueError):
        Authenticator(*args)


def test_a_key_set_with_no_keys_is_refused():
    with pytest.raises(ValueError):
        Authenticator(json.dumps({"keys": []}), ISSUER, AUDIENCE)


# -- the route and where the token is read from ------------------------------------

client = TestClient(api)


@pytest.fixture(autouse=True)
def configured():
    api.state.authenticator = Authenticator(jwk_set(), ISSUER, AUDIENCE)
    yield
    api.state.authenticator = None


def split(t: str) -> tuple[str, str]:
    header_payload, _, signature = t.rpartition(".")
    return header_payload, signature


def cookies(t: str) -> dict:
    header_payload, signature = split(t)
    return {COOKIE_HEADER_PAYLOAD: header_payload, COOKIE_SIGNATURE: signature}


def test_me_names_a_logged_in_browser():
    response = client.get("/api/me", cookies=cookies(token()), headers={"USE-JWT-COOKIE": "true"})
    assert response.status_code == 200
    assert response.json() == {"id": "abc123", "username": "tenzin", "name": "Tenzin K"}


def test_cookies_alone_are_not_a_credential():
    """A hostile page can make a browser attach cookies to a request; it cannot add this
    header without a CORS preflight, so requiring it keeps cookies from acting alone."""
    assert client.get("/api/me", cookies=cookies(token())).status_code == 401


def test_me_accepts_an_authorization_header():
    response = client.get("/api/me", headers={"Authorization": f"JWT {token()}"})
    assert response.status_code == 200
    assert client.get("/api/me", headers={"Authorization": f"Bearer {token()}"}).status_code == 200


def test_me_refuses_no_login():
    assert client.get("/api/me").status_code == 401


def test_me_refuses_a_half_present_cookie():
    header_payload, _ = split(token())
    response = client.get(
        "/api/me",
        cookies={COOKIE_HEADER_PAYLOAD: header_payload},
        headers={"USE-JWT-COOKIE": "true"},
    )
    assert response.status_code == 401


def test_me_refuses_a_forged_cookie():
    response = client.get(
        "/api/me", cookies=cookies(token(key=OTHER_KEY)), headers={"USE-JWT-COOKIE": "true"}
    )
    assert response.status_code == 401


def test_me_says_503_not_401_when_login_is_not_configured():
    """A deployment mistake is not the caller's to fix by logging in again."""
    api.state.authenticator = None
    response = client.get("/api/me", headers={"Authorization": f"JWT {token()}"})
    assert response.status_code == 503
