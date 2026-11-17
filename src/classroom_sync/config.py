"""Settings, read from the environment.

Ported from the Node server's `config.ts`. The comments there explain why each one
exists; they are kept because the reasons have not changed.
"""

import os

DEFAULT_HOST_PASSWORD = "change-me"


def _origins(raw: str) -> list[str]:
    """Split a comma-separated origin list.

    Comma-separated, because a deployment may be reachable by more than one name, and
    because testing a teacher and a student side by side needs two origins — browsers
    scope localStorage per origin, and the host token lives there.
    """
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


class Config:
    is_production: bool = os.environ.get("ENV") == "production"
    port: int = int(os.environ.get("PORT", "8000"))

    #: Tutor's own MySQL, in its own database beside the LMS and notes ones.
    #: Form: mysql://user:password@mysql:3306/classroom
    database_url: str | None = os.environ.get("DATABASE_URL")

    #: Shared secret that permits creating a room. Not a per-user credential.
    host_password: str = os.environ.get("HOST_PASSWORD", DEFAULT_HOST_PASSWORD)

    #: Origins allowed to call the API.
    web_origins: list[str] = _origins(
        os.environ.get("WEB_ORIGIN", "http://localhost:5173")
    )

    #: The OpenPecha library. Its host is never seen by a browser: the reader's
    #: library calls are proxied through this service, which is the only place the
    #: key exists. A key in a frontend bundle is a published key.
    library_base_url: str = os.environ.get("LIBRARY_BASE_URL", "")
    library_app_name: str = os.environ.get("LIBRARY_APP_NAME", "")
    library_api_key: str = os.environ.get("LIBRARY_API_KEY", "")
    #: How long a library response is kept and shared, in seconds. A class loads the same text,
    #: segments and alignments for every student at once; kept for a few minutes they cost the
    #: library one request instead of fifty. 0 turns it off.
    library_cache_seconds: float = float(os.environ.get("LIBRARY_CACHE_SECONDS", "600"))

    #: How the Academy login is checked. All three are public values: the public half of
    #: the platform's signing key, and the issuer and audience its tokens carry. Without
    #: them login is "not configured" and every route that needs a user answers 503.
    jwt_public_jwk_set: str = os.environ.get("JWT_PUBLIC_JWK_SET", "")
    jwt_issuer: str = os.environ.get("JWT_ISSUER", "")
    jwt_audience: str = os.environ.get("JWT_AUDIENCE", "")


config = Config()


def warn_on_insecure_defaults(log) -> None:
    if not (config.jwt_public_jwk_set and config.jwt_issuer and config.jwt_audience):
        log(
            "JWT_PUBLIC_JWK_SET, JWT_ISSUER or JWT_AUDIENCE is not set, so the Academy "
            "login cannot be checked. Routes that need a user will answer 503."
        )
    if not config.library_base_url:
        log(
            "LIBRARY_BASE_URL is not set. Registering a text needs the library, so "
            "creating a room will fail until it is."
        )
    if config.host_password == DEFAULT_HOST_PASSWORD:
        log(
            f'HOST_PASSWORD is still "{DEFAULT_HOST_PASSWORD}". Anyone who can reach '
            "this server can create rooms. It does not let them take over an existing "
            "one — that is the per-room host token — but set it before deploying."
        )
