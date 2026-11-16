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


config = Config()


def warn_on_insecure_defaults(log) -> None:
    if config.host_password == DEFAULT_HOST_PASSWORD:
        log(
            f'HOST_PASSWORD is still "{DEFAULT_HOST_PASSWORD}". Anyone who can reach '
            "this server can create a room and drive a class. Set it before deploying."
        )
