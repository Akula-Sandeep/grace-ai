"""Circuit breakers for the external services a chat turn depends on.

When TMDB is unreachable (a network path or VPN problem) every request used to
wait out its own connect timeouts and retries, turn after turn. A breaker
remembers that the service is failing: after a few consecutive network-level
failures it opens and calls fail at once with a clear reason, then after a
short rest it lets calls through again (half-open): a success closes it, a failure reopens it.

Only failures that say something about the service count (network errors,
timeouts, server errors, rate limits, rejected keys). A "not found" answer is
a successful call.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# Categories as produced by app.services.upstream_errors.classify_upstream_exception
_NETWORK = {"timeout", "connect_error", "network_error"}


@dataclass
class _Policy:
    threshold: int = 2  # consecutive network-level failures before opening
    network_rest: float = 20.0
    server_rest: float = 10.0
    rate_limit_rest: float = 30.0
    auth_rest: float = 600.0


@dataclass
class CircuitBreaker:
    name: str
    policy: _Policy = field(default_factory=_Policy)
    failures: int = 0
    open_until: float = 0.0
    reason: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def retry_after(self) -> float:
        return max(0.0, self.open_until - time.monotonic())

    def allow(self) -> bool:
        """Whether a call may go out now. After the rest, calls go out again (half-open):
        one success closes the breaker, a failure opens it again straight away."""
        with self._lock:
            return time.monotonic() >= self.open_until

    def success(self) -> None:
        with self._lock:
            self.failures = 0
            self.open_until = 0.0
            self.reason = None

    def failure(self, category: str, *, retry_after: float | None = None) -> None:
        with self._lock:
            now = time.monotonic()
            if category == "not_found" or category == "client_error" or category == "parse_error":
                return
            if category == "auth_error":
                self._open(now, self.policy.auth_rest, category)
                return
            if category == "rate_limited":
                self._open(now, retry_after or self.policy.rate_limit_rest, category)
                return
            self.failures += 1
            if category in _NETWORK and self.failures >= self.policy.threshold:
                self._open(now, self.policy.network_rest, category)
            elif category == "server_error" and self.failures >= self.policy.threshold + 1:
                self._open(now, self.policy.server_rest, category)
            elif self.open_until > 0:
                # A failed half-open probe: rest again.
                self._open(now, self.policy.network_rest, category)

    def _open(self, now: float, seconds: float, reason: str) -> None:
        self.open_until = now + max(0.1, seconds)
        self.reason = reason

    def snapshot(self) -> dict:
        rest = self.retry_after()
        return {
            "name": self.name,
            "state": "open" if rest > 0 else ("half_open" if self.open_until > 0 else "closed"),
            "reason": self.reason,
            "retry_after_seconds": round(rest, 1),
            "consecutive_failures": self.failures,
        }


_BREAKERS: dict[str, CircuitBreaker] = {}


def breaker(name: str, **policy: float) -> CircuitBreaker:
    if name not in _BREAKERS:
        _BREAKERS[name] = CircuitBreaker(name, _Policy(**policy) if policy else _Policy())
    return _BREAKERS[name]


def snapshots() -> list[dict]:
    return [b.snapshot() for b in _BREAKERS.values()]


def reset() -> None:
    _BREAKERS.clear()
