"""
qd.providers.http — shared HTTP client for vendor adapters.

Retry with backoff, client-side rate limiting, and optional response recording.

The recorder is the interesting part. When enabled, every response is archived
with the wall-clock time it ARRIVED, not the time it describes. That arrival
time becomes `known_at` when the archive is replayed, which is what makes a
replay honest: it reproduces the information as it actually reached this
system, latency included, rather than as the vendor's timestamps claim it
existed. A backtest built from a clean historical download silently assumes zero
latency and perfect availability, and neither was ever true.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional
from urllib.parse import urlencode

try:
    import requests
except ImportError:                                  # pragma: no cover
    requests = None                                  # type: ignore

from qd.providers.base import ProviderError, RateLimited
from qd.types import utcnow

logger = logging.getLogger(__name__)


class RateLimiter:
    """Simple token bucket, thread-safe.

    Being rate-limited mid-session is not a clean failure — it arrives as
    partial data, which reads downstream as a genuinely quiet tape rather than
    as missing information.
    """

    def __init__(self, per_minute: int) -> None:
        self.interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next = now + self.interval


class Recorder:
    """Archives responses as JSONL for later replay."""

    def __init__(self, directory: str, enabled: bool = True) -> None:
        self.dir = directory
        self.enabled = enabled
        self._lock = threading.Lock()
        if enabled:
            os.makedirs(directory, exist_ok=True)

    def write(self, tag: str, url: str, payload: Any, received_at: datetime) -> None:
        if not self.enabled:
            return
        path = os.path.join(self.dir, f"{tag}-{received_at:%Y%m%d}.jsonl")
        record = {
            "received_at": received_at.isoformat(),
            "url": _redact(url),
            "payload": payload,
        }
        with self._lock:
            with open(path, "a") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def _redact(url: str) -> str:
    """Strip credentials from a URL before it reaches a log or an archive."""
    for key in ("apiKey", "apikey", "api_key", "token"):
        marker = f"{key}="
        idx = url.find(marker)
        while idx != -1:
            end = url.find("&", idx)
            end = len(url) if end == -1 else end
            url = url[: idx + len(marker)] + "***" + url[end:]
            idx = url.find(marker, idx + len(marker) + 3)
    return url


@dataclass
class HttpClient:
    base_url: str
    headers: Mapping[str, str]
    timeout: float = 10.0
    max_retries: int = 3
    rate_limiter: Optional[RateLimiter] = None
    recorder: Optional[Recorder] = None
    session: Any = None

    def __post_init__(self) -> None:
        if requests is None:
            raise ProviderError(
                "the `requests` package is required for live providers — "
                "`pip install requests` (the replay path needs no dependencies)"
            )
        if self.session is None:
            self.session = requests.Session()
            self.session.headers.update(dict(self.headers))

    def get(
        self, path: str, params: Optional[Mapping[str, Any]] = None, tag: str = "get"
    ) -> Any:
        return self._request("GET", path, params=params, tag=tag)

    def post(self, path: str, body: Mapping[str, Any], tag: str = "post") -> Any:
        return self._request("POST", path, json_body=body, tag=tag)

    def delete(self, path: str, tag: str = "delete") -> Any:
        return self._request("DELETE", path, tag=tag)

    def patch(self, path: str, body: Mapping[str, Any], tag: str = "patch") -> Any:
        return self._request("PATCH", path, json_body=body, tag=tag)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Mapping[str, Any]] = None,
        tag: str = "req",
    ) -> Any:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        last: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            if self.rate_limiter:
                self.rate_limiter.acquire()
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body, timeout=self.timeout
                )
                received = utcnow()

                if resp.status_code == 429:
                    # Honour Retry-After when given; the server knows better
                    # than an exponential guess.
                    delay = float(resp.headers.get("Retry-After", 2 ** attempt))
                    logger.warning("rate limited on %s, waiting %.1fs", _redact(url), delay)
                    time.sleep(delay)
                    last = RateLimited(f"429 from {_redact(url)}")
                    continue

                if resp.status_code >= 500:
                    last = ProviderError(f"{resp.status_code} from {_redact(url)}")
                    time.sleep(min(8.0, 2 ** attempt) + random.random() * 0.3)
                    continue

                if resp.status_code == 404:
                    return None

                if resp.status_code >= 400:
                    raise ProviderError(
                        f"{resp.status_code} from {_redact(url)}: {resp.text[:300]}"
                    )

                payload = resp.json() if resp.content else None
                if self.recorder:
                    self.recorder.write(tag, resp.url, payload, received)
                return payload

            except ProviderError:
                raise
            except Exception as exc:                 # network-level failure
                last = exc
                if attempt < self.max_retries:
                    time.sleep(min(8.0, 2 ** attempt) + random.random() * 0.3)

        raise ProviderError(f"{method} {_redact(url)} failed after "
                            f"{self.max_retries + 1} attempts: {last}")


__all__ = ["HttpClient", "RateLimiter", "Recorder"]
