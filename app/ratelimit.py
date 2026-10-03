import time
from collections import defaultdict, deque


class RateLimiter:
    """Einfaches Sliding-Window-Limit im Arbeitsspeicher.

    Genügt für den Betrieb mit einem uvicorn-Worker. Bei mehreren Workern
    müsste der Zähler in Redis/DB liegen."""

    def __init__(self, window_seconds: int = 3600):
        self.window = window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


limiter = RateLimiter()
