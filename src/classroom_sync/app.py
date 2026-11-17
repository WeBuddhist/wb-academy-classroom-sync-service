"""The service.

Track A: the HTTP routes are in, the socket events are next — see docs/BUILD-PLAN.md.

The Socket.IO server is mounted here even with no events attached yet, because it is
what this service exists for and the mounting is the part most easily got wrong.
`socketio.ASGIApp` wraps the FastAPI app rather than the other way round: Socket.IO
takes its own path (/socket.io/ by default) and hands everything else to FastAPI.
"""

import logging
from contextlib import asynccontextmanager

import socketio
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .auth import Authenticator, current_user, User
from .config import config, warn_on_insecure_defaults
from .db import Database
from .events import attach
from .library import LibraryClient
from .proxy import router as proxy_router
from .routes import router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("classroom_sync")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the pool and the library client once, not per request.

    Both are held on `app.state` so routes reach them through `request.app.state` —
    which is also what lets a test swap in a stub without touching module globals.
    """
    app.state.config = config
    app.state.database = Database(config.database_url or "")
    await app.state.database.connect()
    app.state.library = LibraryClient(
        config.library_base_url, config.library_app_name, config.library_api_key
    )
    # None when the login settings are missing; routes needing a user then answer 503.
    app.state.authenticator = (
        Authenticator(config.jwt_public_jwk_set, config.jwt_issuer, config.jwt_audience)
        if config.jwt_public_jwk_set and config.jwt_issuer and config.jwt_audience
        else None
    )
    try:
        yield
    finally:
        await app.state.library.aclose()
        await app.state.database.close()


api = FastAPI(title="Classroom sync service", lifespan=lifespan)

api.add_middleware(
    CORSMiddleware,
    allow_origins=config.web_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

api.include_router(router)
api.include_router(proxy_router)


@api.get("/api/me")
async def me(user: User = Depends(current_user)) -> dict[str, str]:
    """Who the server thinks you are. What the reader asks first, so a missing or expired
    login is found on the code screen rather than part-way into joining a class."""
    return {"id": user.id, "username": user.username, "name": user.name}


@api.get("/api/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


# Prefer a real WebSocket. Long-polling stays as a fallback for restrictive networks,
# but on it the teacher would see the highlight lag behind their own voice.
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=config.web_origins,
)

# The pool is opened in `lifespan`, after this module is imported, so the handlers
# reach it through a callable rather than capturing it now.
api.state.live = attach(
    sio,
    lambda: api.state.database.pool,
    lambda: api.state.authenticator,
    config.web_origins,
)

app = socketio.ASGIApp(sio, other_asgi_app=api)

warn_on_insecure_defaults(log.warning)
