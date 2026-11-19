"""A small limit on how fast one account may try class codes.

A code is the one thing standing between a stranger and a class (joining needs only the code
and an Academy login), so guessing them has to be slow as well as unlikely. The code space is
large, but nothing used to notice someone trying thousands a minute.

Per account, in this process: the service runs one worker, like its sockets, so there is no
shared store to keep. A restart forgets the counts, which only gives a guesser a fresh minute.
"""

import time
from collections import deque

from fastapi import Depends, HTTPException

from .auth import User, current_user


class RateLimiter:
    """At most `limit` hits per key in any `window` seconds."""

    def __init__(self, limit: int, window: float) -> None:
        self._limit = limit
        self._window = window
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> bool:
        """Count one attempt. False when the key is over its limit (and it is not counted)."""
        now = time.monotonic()
        recent = self._hits.setdefault(key, deque())
        while recent and now - recent[0] > self._window:
            recent.popleft()
        if len(recent) >= self._limit:
            return False
        recent.append(now)
        if len(self._hits) > 10_000:
            self._sweep(now)
        return True

    def reset(self) -> None:
        self._hits.clear()

    def _sweep(self, now: float) -> None:
        """Forget keys with nothing recent, so a long-running process does not keep every
        account it has ever seen."""
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > self._window]:
            del self._hits[key]


#: Looking up and joining classes by code: a person typing a code makes a handful of attempts
#: a minute at most.
code_attempts = RateLimiter(limit=30, window=60)


async def limit_code_attempts(user: User = Depends(current_user)) -> User:
    """FastAPI dependency: the logged-in user, unless they are guessing codes too fast."""
    if not code_attempts.hit(user.id):
        raise HTTPException(
            status_code=429,
            detail="Too many attempts. Wait a minute and try again.",
            headers={"Retry-After": "60"},
        )
    return user
