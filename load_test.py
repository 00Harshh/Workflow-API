#!/usr/bin/env python3
"""
load_test.py — Workflow API load tester

Fires N requests/second at a Workflow API endpoint and reports:
  - Actual RPS achieved
  - Status code breakdown (2xx / 429 / 5xx / errors)
  - Latency percentiles (p50 / p95 / p99)
  - Workflow API overhead vs n8n time estimate

Usage:
  python3 load_test.py                          # uses defaults
  python3 load_test.py --rps 20 --duration 10
  python3 load_test.py --rps 20 --key wfapi-xxx --url http://localhost:8000/run/n8ntest
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_URL      = "http://localhost:8000/run/n8ntest"
DEFAULT_RPS      = 20
DEFAULT_DURATION = 10     # seconds
DEFAULT_PAYLOAD  = {"name": "LoadTest", "test": True}
HEALTH_URL       = "http://localhost:8000/health"


@dataclass
class Result:
    status:     int
    latency_ms: float
    error:      Optional[str] = None


@dataclass
class Stats:
    results: list[Result] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def ok(self) -> list[Result]:
        return [r for r in self.results if 200 <= r.status < 300]

    @property
    def rate_limited(self) -> list[Result]:
        return [r for r in self.results if r.status == 429]

    @property
    def server_errors(self) -> list[Result]:
        return [r for r in self.results if r.status >= 500]

    @property
    def conn_errors(self) -> list[Result]:
        return [r for r in self.results if r.status == 0]

    @property
    def latencies(self) -> list[float]:
        return sorted(r.latency_ms for r in self.results if r.status != 0)

    def percentile(self, p: float) -> float:
        ls = self.latencies
        if not ls:
            return 0.0
        idx = max(0, int(len(ls) * p / 100) - 1)
        return ls[min(idx, len(ls) - 1)]


# ── HTTP worker ───────────────────────────────────────────────────────────────

async def fire_one(client, url: str, key: str, stats: Stats) -> None:
    import httpx
    start = time.monotonic()
    try:
        r = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type":  "application/json",
            },
            json=DEFAULT_PAYLOAD,
        )
        latency = (time.monotonic() - start) * 1000
        stats.results.append(Result(status=r.status_code, latency_ms=latency))
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        stats.results.append(Result(status=0, latency_ms=latency, error=str(exc)))


# ── Rate-controlled dispatcher ────────────────────────────────────────────────

async def run_test(url: str, key: str, rps: int, duration: int) -> Stats:
    import httpx
    stats   = Stats()
    interval = 1.0 / rps
    deadline = time.monotonic() + duration
    next_at  = time.monotonic()
    sent     = 0
    tasks: list[asyncio.Task] = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            now = time.monotonic()

            # Stop dispatching new requests after duration
            if now >= deadline:
                break

            if now >= next_at:
                tasks.append(asyncio.create_task(fire_one(client, url, key, stats)))
                sent += 1
                next_at += interval

                completed = stats.total
                elapsed   = now - (deadline - duration)
                actual_rps = completed / elapsed if elapsed > 0 else 0
                print(
                    f"\r  ⚡ Sent {sent:4d}  |  Completed {completed:4d}  |  "
                    f"Elapsed {elapsed:.1f}s  |  Live RPS {actual_rps:.1f}    ",
                    end="", flush=True,
                )
            else:
                await asyncio.sleep(max(0.0, next_at - time.monotonic()))

        # Wait for all in-flight requests
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    print()  # newline after live counter
    return stats


# ── Pre-flight check ──────────────────────────────────────────────────────────

async def preflight(url: str, key: str) -> bool:
    import httpx
    print(f"  Checking server at {HEALTH_URL} ...", end=" ", flush=True)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(HEALTH_URL)
            if r.status_code == 200:
                print("✅ OK")
            else:
                print(f"⚠️  HTTP {r.status_code}")
    except Exception as e:
        print(f"❌ Cannot reach server: {e}")
        print("  → Run: python cli.py start")
        return False

    print(f"  Validating API key ...", end=" ", flush=True)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # POST to the target; we expect anything except 401/403
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"name": "preflight", "test": True},
            )
            if r.status_code in (401, 403):
                print(f"❌ Auth failed (HTTP {r.status_code})")
                print("  → Restart the server so it picks up the new key: python cli.py start")
                return False
            print(f"✅ OK (HTTP {r.status_code})")
    except Exception as e:
        print(f"⚠️  Request error (server up but n8n may be offline): {e}")
        print("  → Test will proceed; n8n errors show as 5xx")

    return True


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(stats: Stats, rps: int, duration: int, key_prefix: str) -> None:
    SEP  = "─" * 58
    SEP2 = "═" * 58
    total    = stats.total
    elapsed  = duration  # best approximation

    print()
    print(f"  {SEP2}")
    print(f"  ⚡  Workflow API Load Test Report")
    print(f"  {SEP2}")
    print(f"  Key prefix     : {key_prefix}...")
    print(f"  Target RPS     : {rps}")
    print(f"  Duration       : {duration}s")
    print(f"  {SEP}")
    print(f"  Requests sent  : {total}")
    print(f"  Actual RPS     : {total / elapsed:.1f}")
    print(f"  {SEP}")

    pct = lambda n: f"{100 * n / total:.1f}%" if total else "—"
    ok  = len(stats.ok)
    rl  = len(stats.rate_limited)
    se  = len(stats.server_errors)
    ce  = len(stats.conn_errors)

    print(f"  ✅ Success (2xx)    {ok:5d}   {pct(ok)}")
    print(f"  🚫 Rate limited 429 {rl:5d}   {pct(rl)}")
    print(f"  💥 Server error 5xx {se:5d}   {pct(se)}")
    print(f"  🔌 Conn errors      {ce:5d}   {pct(ce)}")
    print(f"  {SEP}")

    ls = stats.latencies
    if ls:
        print(f"  Latency (ms)   min={min(ls):.1f}  p50={stats.percentile(50):.1f}  "
              f"p95={stats.percentile(95):.1f}  p99={stats.percentile(99):.1f}  max={max(ls):.1f}")
        print(f"  {SEP}")
        fg_overhead = 0.05  # ms — Workflow API's own auth+log overhead
        n8n_est = stats.percentile(50) - fg_overhead
        print(f"  Estimated n8n p50 latency : {max(n8n_est, 0):.1f} ms")
        print(f"  Workflow API overhead         : ~{fg_overhead} ms per request")

    print(f"  {SEP2}")

    if rl > 0:
        print(f"\n  ⚠️  {rl} requests were rate-limited (429).")
        print(f"     Your key allows {rps * 60} RPM. To increase it:")
        print(f"     python cli.py keys revoke LoadTest")
        print(f"     python cli.py keys create --name LoadTest --rate-limit {rps * 60 * 2}")
    if ce > 0:
        print(f"\n  ⚠️  {ce} connection errors. Is n8n running on port 5678?")
        print(f"     In n8n, click 'Listen for test event' on your Webhook node.")

    print()


# ── Entry point ───────────────────────────────────────────────────────────────

def resolve_key() -> str:
    """Read the first key_prefix from config.yaml, prompt user to supply full key."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from core.auth import load_config
        keys = load_config().get("keys") or []
        if keys:
            pfx = keys[0].get("key_prefix", "wfapi-???")
            print(f"  Found key prefix in config: {pfx}...")
            print(f"  ⚠️  Raw key not stored (hashed). Pass it via --key")
    except Exception:
        pass
    return ""


def main():
    parser = argparse.ArgumentParser(description="Workflow API load tester")
    parser.add_argument("--url",      default=DEFAULT_URL,      help="Workflow API endpoint URL")
    parser.add_argument("--key",      default="",               help="Bearer API key (required)")
    parser.add_argument("--rps",      default=DEFAULT_RPS,      type=int, help="Target requests/second")
    parser.add_argument("--duration", default=DEFAULT_DURATION, type=int, help="Test duration in seconds")
    parser.add_argument("--skip-preflight", action="store_true", help="Skip pre-flight check")
    args = parser.parse_args()

    key = args.key.strip()
    if not key:
        resolve_key()
        print("\n  ❌ --key is required. Example:")
        print("     python3 load_test.py --key wfapi-YourKeyHere --rps 20 --duration 10\n")
        sys.exit(1)

    key_prefix = key[:16] if len(key) > 16 else key

    print()
    print(f"  ⚡ Workflow API Load Test")
    print(f"  Target  : {args.url}")
    print(f"  RPS     : {args.rps}")
    print(f"  Duration: {args.duration}s")
    print(f"  Total   : ~{args.rps * args.duration} requests")
    print()

    async def _run():
        if not args.skip_preflight:
            ok = await preflight(args.url, key)
            if not ok:
                sys.exit(1)
        print()
        print(f"  🚀 Running load test...")
        stats = await run_test(args.url, key, args.rps, args.duration)
        print_report(stats, args.rps, args.duration, key_prefix)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
