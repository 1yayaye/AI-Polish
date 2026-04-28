"""Small in-process rate limiter for single-instance deployments."""

import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import HTTPException, status


class InMemoryRateLimiter:
    def __init__(self):
        self._attempts: Dict[str, Deque[float]] = defaultdict(deque)

    def check(self, key: str, *, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        cutoff = now - window_seconds
        attempts = self._attempts[key]
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if len(attempts) >= limit:
            return False
        attempts.append(now)
        return True

    def check_or_raise(self, key: str, *, limit: int, window_seconds: int) -> None:
        if not self.check(key, limit=limit, window_seconds=window_seconds):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many attempts. Please try again later.",
            )


rate_limiter = InMemoryRateLimiter()


def client_ip(request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for") if request else None
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    if request and request.client:
        return request.client.host
    return "unknown"
