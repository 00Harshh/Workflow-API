import os
import time
from collections import OrderedDict
from threading import Lock


class RateLimiter:
    """
    Per-key in-memory token bucket limiter.
    Used as the default and as the Redis fallback.

    Fix #5: bounded to MAX_BUCKETS entries using LRU eviction.
    An attacker flooding fake keys can no longer grow this dict unboundedly.
    """

    MAX_BUCKETS = 50_000  # ~12 MB RAM maximum

    def __init__(self):
        self._buckets: OrderedDict[str, dict] = OrderedDict()
        self._lock = Lock()

    def is_allowed(self, key_value: str, rate_limit_per_minute: int) -> bool:
        if rate_limit_per_minute <= 0:
            return True  # 0 = unlimited

        with self._lock:
            now = time.monotonic()
            refill_rate = rate_limit_per_minute / 60.0

            if key_value not in self._buckets:
                # Evict oldest 10% when at capacity (LRU eviction)
                if len(self._buckets) >= self.MAX_BUCKETS:
                    evict_count = self.MAX_BUCKETS // 10
                    for _ in range(evict_count):
                        self._buckets.popitem(last=False)

                self._buckets[key_value] = {
                    "tokens": float(rate_limit_per_minute),
                    "last_refill": now,
                    "capacity": rate_limit_per_minute,
                }

            bucket = self._buckets[key_value]
            # Move to end (most-recently-used)
            self._buckets.move_to_end(key_value)

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


class RedisRateLimiter:
    """
    Per-key sliding-window rate limiter backed by Redis.
    Accurate across multiple uvicorn worker processes.

    Algorithm: sorted-set sliding window.
      - ZREMRANGEBYSCORE  — drop timestamps older than 60 s
      - ZADD              — record this request's timestamp
      - ZCARD             — count requests in the window
      - EXPIRE            — auto-clean the key
    All four commands run in a single pipeline (one round-trip).

    On any Redis error, falls back to the in-memory limiter for that call.
    """

    def __init__(self, client):
        self._client = client
        self._fallback = RateLimiter()

    def is_allowed(self, key_value: str, rate_limit_per_minute: int) -> bool:
        if rate_limit_per_minute <= 0:
            return True
        try:
            return self._redis_check(key_value, rate_limit_per_minute)
        except Exception:
            return self._fallback.is_allowed(key_value, rate_limit_per_minute)

    def _redis_check(self, key_value: str, rate_limit_per_minute: int) -> bool:
        now = time.time()
        window_start = now - 60.0
        rkey = f"workflow-api:rl:{key_value}"

        with self._client.pipeline() as pipe:
            pipe.zremrangebyscore(rkey, 0, window_start)
            # Use a unique member per request to avoid collisions at the same timestamp
            pipe.zadd(rkey, {f"{now:.6f}": now})
            pipe.zcard(rkey)
            pipe.expire(rkey, 70)  # slightly longer than the 60-s window
            results = pipe.execute()

        count = results[2]
        return count <= rate_limit_per_minute


def _build_limiter() -> RateLimiter | RedisRateLimiter:
    """
    Try to connect to Redis. If reachable, return a RedisRateLimiter.
    Otherwise, fall back to the in-memory RateLimiter and print a notice.
    Reads REDIS_URL env var (default: redis://localhost:6379).
    """
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        import redis as redis_lib  # optional dependency

        client = redis_lib.Redis.from_url(redis_url, socket_connect_timeout=1)
        client.ping()
        print(f"✅ Rate limiter: Redis connected ({redis_url})")
        return RedisRateLimiter(client)
    except ImportError:
        print("⚠️  redis package not installed — using in-memory rate limiter")
    except Exception as exc:
        print(f"⚠️  Redis unavailable ({exc}) — using in-memory rate limiter")

    return RateLimiter()


# Singleton — shared across all requests in this worker process
limiter = _build_limiter()
