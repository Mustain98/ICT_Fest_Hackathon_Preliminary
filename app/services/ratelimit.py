"""Per-user rolling-window rate limiting for booking creation."""
import threading
import time

from ..errors import AppError

_WINDOW_SECONDS = 60
_MAX_REQUESTS = 20

_lock = threading.Lock()
_buckets: dict[int, list[float]] = {}


def record_and_check(user_id: int) -> None:
    now = time.time()
    cutoff = now - _WINDOW_SECONDS
    with _lock:
        bucket = [t for t in _buckets.get(user_id, []) if t > cutoff]
        bucket.append(now)
        _buckets[user_id] = bucket
        over_limit = len(bucket) > _MAX_REQUESTS

    # Raised outside the lock so an exception cannot leave it held.
    if over_limit:
        raise AppError(429, "RATE_LIMITED", "Too many booking requests")
