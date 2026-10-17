"""Owner-approved guide endpoint with typed functional outcomes and independent throttling."""
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from fastapi import HTTPException, Request, Response

from .guide_contracts import GuideQuestion, GuideResponse
from .guide_service import GuideService
from .interface_copy import GUIDE_RATE_LIMIT_MESSAGE


class QuestionLimiter:
    """One-worker demo limiter; stores salted source hashes and times, never questions."""

    def __init__(self, limit: int, source_limit: int, clock=time.monotonic):
        self.limit, self.source_limit, self.clock = limit, source_limit, clock
        self.salt = secrets.token_bytes(32)
        self.sources: dict[str, deque[float]] = {}
        self.lock = threading.Lock()

    def accept(self, source: str) -> bool:
        key = hmac.new(self.salt, source.encode(), hashlib.sha256).hexdigest()
        now = self.clock()
        with self.lock:
            for stale_key in list(self.sources):
                timestamps = self.sources[stale_key]
                while timestamps and timestamps[0] <= now - 60:
                    timestamps.popleft()
                if not timestamps:
                    del self.sources[stale_key]
            if key not in self.sources and len(self.sources) >= self.source_limit:
                return False
            timestamps = self.sources.setdefault(key, deque())
            if len(timestamps) >= self.limit:
                return False
            timestamps.append(now)
            return True


def install_guide_routes(app, service: GuideService):
    limiter = QuestionLimiter(service.settings.questions_per_minute, service.settings.source_limit)

    app.state.guide_limiter = limiter

    @app.post('/api/v1/guide/questions', response_model=GuideResponse)
    def question(body: GuideQuestion, request: Request, response: Response):
        if not limiter.accept(request.client.host if request.client else 'unknown'):
            raise HTTPException(429, GUIDE_RATE_LIMIT_MESSAGE, headers={"Retry-After": "60"})
        response.headers['Cache-Control'] = 'private, no-store'
        return service.ask(body)
