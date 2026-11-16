"""The service.

Track A, Step 1: it stands up and answers. Rooms, texts and the socket events come
next — see docs/BUILD-PLAN.md.

The Socket.IO server is mounted here from the start, even with no events attached,
because it is what this service exists for and the mounting is the part most easily
got wrong. `socketio.ASGIApp` wraps the FastAPI app rather than the other way round:
Socket.IO takes its own path (/socket.io/ by default) and hands everything else to
FastAPI.
"""

import logging

import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import config, warn_on_insecure_defaults

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("classroom_sync")

api = FastAPI(title="Classroom sync service")

api.add_middleware(
    CORSMiddleware,
    allow_origins=config.web_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@api.get("/api/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


# Prefer a real WebSocket. Long-polling stays as a fallback for restrictive networks,
# but on it the teacher would see the highlight lag behind their own voice.
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=config.web_origins,
)

app = socketio.ASGIApp(sio, other_asgi_app=api)

warn_on_insecure_defaults(log.warning)
