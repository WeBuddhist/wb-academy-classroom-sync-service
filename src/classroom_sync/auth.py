"""Who is making a request: the Academy login, checked here without calling Open edX.

Open edX signs a short-lived JWT for every logged-in browser and keeps it in two cookies
(`edx-jwt-cookie-header-payload` and `edx-jwt-cookie-signature`, the second HttpOnly so
a script can never read it). Joined with a dot they are the token. This module joins
them, checks the signature against the platform's **public** key, and reads who it is.

Nothing here talks to the LMS, and nothing secret is held: the public key is the half
anyone may have. A forged or expired token is simply refused.

What identifies someone is the token's `sub`, an opaque id that never changes, not their
username: a person is the same person if a display name is edited. The username and name
travel along only to be shown in a member list.
"""

import json
from dataclasses import dataclass, field

import jwt
from fastapi import HTTPException, Request
from jwt.algorithms import RSAAlgorithm

#: Open edX's own names for the two halves of the cookie, and the header its frontend
#: library adds when it wants the cookie to count as a credential.
COOKIE_HEADER_PAYLOAD = "edx-jwt-cookie-header-payload"
COOKIE_SIGNATURE = "edx-jwt-cookie-signature"
COOKIE_OPT_IN_HEADER = "use-jwt-cookie"


#: Open edX's default is RS512. HMAC and `none` are deliberately absent.
ALLOWED_ALGORITHMS = ["RS512", "RS256"]


class AuthError(Exception):
    """The request carried no valid login."""


@dataclass(frozen=True)
class User:
    #: The token's `sub`. Stable and opaque: what memberships are keyed by.
    id: str
    username: str
    #: Display name, possibly empty.
    name: str
    #: When the login this was read from stops being valid (the token's `exp`, seconds since
    #: the epoch). A long-lived connection checks it, since it is verified only once. None for
    #: a user built by hand, as tests do, meaning no expiry.
    expires_at: float | None = field(default=None, compare=False)


class Authenticator:
    """Verifies Open edX JWTs against a public key set."""

    def __init__(self, jwk_set: str, issuer: str, audience: str) -> None:
        if not jwk_set or not issuer or not audience:
            raise ValueError("JWT_PUBLIC_JWK_SET, JWT_ISSUER and JWT_AUDIENCE are all needed")
        self._issuer = issuer
        self._audience = audience
        self._keys = {}
        for jwk in json.loads(jwk_set).get("keys", []):
            self._keys[jwk.get("kid")] = RSAAlgorithm.from_jwk(json.dumps(jwk))
        if not self._keys:
            raise ValueError("JWT_PUBLIC_JWK_SET holds no keys")

    def verify(self, token: str) -> User:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            if kid is not None:
                # A token that names a key must name one we hold. It is refused, never
                # tried against some other key: a token naming a key we do not have has
                # not been signed by us.
                key = self._keys.get(kid)
                if key is None:
                    raise AuthError("Unknown signing key")
                candidates = [key]
            else:
                # Open edX's own login cookies carry no `kid` at all, found by checking
                # a token the real LMS issued. The platform has one signing key, so it
                # is the only one a keyless token can have been signed with.
                candidates = list(self._keys.values())
            claims = self._decode(token, candidates)
        except AuthError:
            raise
        except jwt.PyJWTError as error:
            raise AuthError(str(error)) from error
        return User(
            id=str(claims["sub"]),
            username=str(claims["preferred_username"]),
            name=str(claims.get("name") or ""),
            expires_at=float(claims["exp"]),
        )

    def _decode(self, token: str, candidates: list) -> dict:
        last: Exception | None = None
        for key in candidates:
            try:
                return jwt.decode(
                    token,
                    key,
                    # Pinned to asymmetric RSA, and never taken from the token. Open edX
                    # signs with RS512 (found by checking a real login cookie, not the
                    # docs); RS256 is allowed beside it as the other common choice.
                    # Letting a token pick its own algorithm, or accepting HMAC with a
                    # public key as the secret, is the classic way to get a forgery in.
                    algorithms=ALLOWED_ALGORITHMS,
                    audience=self._audience,
                    issuer=self._issuer,
                    options={"require": ["exp", "iss", "aud", "sub", "preferred_username"]},
                )
            except jwt.InvalidSignatureError as error:
                last = error  # perhaps another key signed it; try the next
        raise last or AuthError("No key could verify this token")


def token_from_request(request: Request) -> str | None:
    """The token a request carries, from whichever place it was put.

    - An `Authorization: JWT …` (or `Bearer …`) header, for non-browser callers.
    - The two Open edX cookies, **only when the request also opts in** with the
      `USE-JWT-COOKIE` header. A browser sends cookies on any request to this site,
      including ones a hostile page makes; that header cannot be set cross-site without a
      CORS preflight we refuse, so requiring it keeps a cookie from acting as a
      credential for a request the user did not mean to make.
    """
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() in ("jwt", "bearer") and value:
        return value.strip()
    if request.headers.get(COOKIE_OPT_IN_HEADER, "").lower() == "true":
        return token_from_cookies(request.cookies)
    return None


def token_from_cookies(cookies) -> str | None:
    """Join the two cookie halves, or None if either is missing."""
    header_payload = cookies.get(COOKIE_HEADER_PAYLOAD)
    signature = cookies.get(COOKIE_SIGNATURE)
    if not header_payload or not signature:
        return None
    return f"{header_payload}.{signature}"


async def current_user(request: Request) -> User:
    """FastAPI dependency: the logged-in user, or a 401."""
    authenticator = getattr(request.app.state, "authenticator", None)
    if authenticator is None:
        # A deployment mistake, not the caller's: say so, rather than a 401 that sends a
        # logged-in teacher round in circles trying to log in again.
        raise HTTPException(503, "Login is not configured on this server")
    token = token_from_request(request)
    if not token:
        raise HTTPException(401, "Log in to continue")
    try:
        return authenticator.verify(token)
    except AuthError as error:
        raise HTTPException(401, "Your login has expired. Please log in again.") from error
