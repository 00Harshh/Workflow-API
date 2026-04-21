import time
from threading import Lock


class RateLimiter:
    """
    Per-key token bucket limiter.
    Each key gets its own bucket with its own capacity (rate_limit_per_minute).
    """
    def __init__(self):
        self._buckets: dict[str, dict] = {}
        self._lock = Lock()

    def is_allowed(self, key_value: str, rate_limit_per_minute: int) -> bool:
        if rate_limit_per_minute <= 0:
            return True  # 0 = unlimited

        with self._lock:
            now = time.monotonic()
            refill_rate = rate_limit_per_minute / 60.0

            if key_value not in self._buckets:
                self._buckets[key_value] = {
                    "tokens": float(rate_limit_per_minute),
                    "last_refill": now,
                    "capacity": rate_limit_per_minute,
                }

            bucket = self._buckets[key_value]

            # Update capacity in case the key's rate limit was changed
            bucket["capacity"] = rate_limit_per_minute

            elapsed = now - bucket["last_refill"]
            bucket["tokens"] = min(
                float(rate_limit_per_minute),
                bucket["tokens"] + elapsed * refill_rate,
            )
            bucket["last_refill"] = now

            if bucket["tokens"] >= 1:
                bucket["tokens"] -= 1
                return True
            return False


# Singleton — shared across all requests
limiter = RateLimiter()
