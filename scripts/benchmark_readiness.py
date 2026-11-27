"""Reproducibly measure the backend readiness endpoint without sending user data.

Run: python scripts/benchmark_readiness.py [--url URL] [--samples N]
"""

from __future__ import annotations

import argparse
import json
import math
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


def percentile(samples: list[float], value: float) -> float:
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("at least one sample is required")
    index = max(0, math.ceil(value * len(ordered)) - 1)
    return ordered[index]


def safe_endpoint(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def measure(url: str, samples: int, timeout: float) -> dict[str, object]:
    timings_ms = []
    statuses = []
    for _ in range(samples):
        started = time.perf_counter()
        try:
            request = Request(url, headers={"Accept": "application/json"})
            with urlopen(request, timeout=timeout) as response:
                response.read()
                statuses.append(response.status)
        except HTTPError as error:
            statuses.append(error.code)
        except URLError as error:
            raise RuntimeError(f"Readiness request failed: {error.reason}") from error
        timings_ms.append((time.perf_counter() - started) * 1000)
    return {
        "endpoint": safe_endpoint(url),
        "samples": samples,
        "statuses": statuses,
        "latency_ms": {
            "min": round(min(timings_ms), 1),
            "median": round(percentile(timings_ms, 0.5), 1),
            "p95": round(percentile(timings_ms, 0.95), 1),
            "max": round(max(timings_ms), 1),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:7000/api/ready")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error("--samples must be between 1 and 100")
    if not 0 < args.timeout <= 60:
        parser.error("--timeout must be greater than 0 and at most 60 seconds")
    try:
        result = measure(args.url, args.samples, args.timeout)
    except RuntimeError as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, indent=2))
    return 0 if all(200 <= status < 300 for status in result["statuses"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
