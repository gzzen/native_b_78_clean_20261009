# Path: common/integrity/relay_monitor.py
# Purpose: Pace model requests to the relay across all attempts of one controller and back off on refusals.
"""Process-wide relay monitor shared by every per-attempt gateway.

Admission control is an AIMD in-flight limit on upstream /responses requests.
A capacity refusal halves the limit and starts a jittered, exponentially
growing cooldown during which no new request is admitted. Consecutive
refusals open a circuit breaker; once its cooldown passes, a single real
request is admitted as a passive probe (no paid health checks), and the
breaker closes when the relay accepts it. Everything recorded is content-free.
"""
from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass, field

TOKEN = re.compile(r"[a-z0-9_.-]{1,64}")
SIGNALS = ("http_429", "http_502", "http_503", "http_504", "http_52x", "http_529", "connect_error",
           "sse_capacity", "sse_server_error", "sse_stream_error", "sse_early_eof", "stream_failure_event")


@dataclass
class Ticket:
    probe: bool = False
    forced: bool = False
    released: bool = False


@dataclass
class RelayStats:
    """Per-attempt, content-free counters written to <workspace>/relay_monitor.json."""
    codex_requests: int = 0
    upstream_tries: int = 0
    transparent_retries: int = 0
    forced_admissions: int = 0
    refusals_forwarded: int = 0
    completed_streams: int = 0
    mid_stream_drops: int = 0
    client_disconnects: int = 0
    admission_wait_seconds: float = 0.0
    first_byte_seconds: list = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    error_codes: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, name: str, amount: float = 1) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + amount)

    def signal(self, kind: str, code: object = None) -> None:
        with self.lock:
            self.signals[kind] = self.signals.get(kind, 0) + 1
            # Only short identifier-like codes; never messages or bodies.
            if isinstance(code, str) and TOKEN.fullmatch(code):
                self.error_codes[code] = self.error_codes.get(code, 0) + 1

    def first_byte(self, seconds: float) -> None:
        with self.lock:
            self.first_byte_seconds.append(round(seconds, 3))

    def to_json(self) -> dict:
        with self.lock:
            latency = self.first_byte_seconds
            return {"codex_requests": self.codex_requests, "upstream_tries": self.upstream_tries,
                    "transparent_retries": self.transparent_retries,
                    "forced_admissions": self.forced_admissions,
                    "refusals_forwarded": self.refusals_forwarded,
                    "completed_streams": self.completed_streams, "mid_stream_drops": self.mid_stream_drops,
                    "client_disconnects": self.client_disconnects,
                    "admission_wait_seconds": round(self.admission_wait_seconds, 3),
                    "first_byte_seconds": {"count": len(latency), "max": max(latency, default=None),
                                           "mean": round(sum(latency) / len(latency), 3) if latency else None},
                    "signals": dict(sorted(self.signals.items())),
                    "error_codes": dict(sorted(self.error_codes.items()))}


class RelayMonitor:
    def __init__(self, max_inflight: int = 2, *, base_backoff: float = 5.0, max_backoff: float = 120.0,
                 breaker_threshold: int = 3, recovery_seconds: float = 60.0, jitter: float = 0.25,
                 rng=random.random):
        if max_inflight < 1:
            raise ValueError("max_inflight must be at least 1")
        self.max_inflight = max_inflight
        self.base_backoff, self.max_backoff = base_backoff, max_backoff
        self.breaker_threshold, self.recovery_seconds, self.jitter = breaker_threshold, recovery_seconds, jitter
        self.rng = rng
        self.condition = threading.Condition()
        self.limit = float(max_inflight)
        self.inflight = 0
        self.consecutive = 0
        self.cooldown_until = 0.0
        self.last_signal = float("-inf")
        self.state = "closed"  # closed | open | half_open
        self.probe_inflight = False
        self.totals: dict = {"attempts": 0, "refusals": 0, "breaker_opened": 0, "mid_stream_drops": 0}

    def config(self) -> dict:
        return {"max_inflight": self.max_inflight, "base_backoff_seconds": self.base_backoff,
                "max_backoff_seconds": self.max_backoff, "breaker_threshold": self.breaker_threshold,
                "recovery_seconds": self.recovery_seconds, "jitter": self.jitter}

    def _backoff(self, retry_after: float | None) -> float:
        delay = min(self.max_backoff, self.base_backoff * 2 ** max(0, self.consecutive - 1))
        delay *= 1 + self.jitter * (2 * self.rng() - 1)
        if retry_after is not None and retry_after > 0:
            delay = max(delay, min(float(retry_after), self.max_backoff))
        return delay

    def admit(self, deadline: float) -> Ticket:
        """Wait for a request slot; past the deadline, admit anyway rather than fail the request."""
        with self.condition:
            while True:
                now = time.monotonic()
                if now >= deadline:
                    self.inflight += 1
                    return Ticket(forced=True)
                if now < self.cooldown_until:
                    self.condition.wait(min(self.cooldown_until, deadline) - now)
                    continue
                if self.state == "open":
                    self.state = "half_open"
                if self.state == "half_open":
                    if self.probe_inflight:
                        self.condition.wait(deadline - now)
                        continue
                    self.probe_inflight = True
                    self.inflight += 1
                    return Ticket(probe=True)
                if self.inflight >= max(1, int(self.limit)):
                    self.condition.wait(deadline - now)
                    continue
                self.inflight += 1
                return Ticket()

    def _release(self, ticket: Ticket) -> None:
        if not ticket.released:
            ticket.released = True
            self.inflight -= 1
            if ticket.probe:
                self.probe_inflight = False
        self.condition.notify_all()

    def committed(self, ticket: Ticket) -> None:
        """The relay accepted the request (content started); the slot stays held."""
        with self.condition:
            self.consecutive = 0
            if ticket.probe or self.state != "closed":
                self.state = "closed"
                self.probe_inflight = False
                ticket.probe = False
            self.condition.notify_all()

    def refused(self, ticket: Ticket, retry_after: float | None = None) -> float:
        """Record a refusal, release the slot and return the monotonic cooldown end."""
        with self.condition:
            now = time.monotonic()
            self.consecutive += 1
            self.totals["refusals"] += 1
            self.limit = max(1.0, self.limit / 2)
            self.cooldown_until = max(self.cooldown_until, now + self._backoff(retry_after))
            self.last_signal = now
            if ticket.probe or (self.state == "closed" and self.consecutive >= self.breaker_threshold):
                if self.state != "open":
                    self.totals["breaker_opened"] += 1
                self.state = "open"
            self._release(ticket)
            return self.cooldown_until

    def finished(self, ticket: Ticket, outcome: str) -> None:
        """Release a slot: outcome is completed, drop (mid-stream failure) or aborted."""
        with self.condition:
            now = time.monotonic()
            if outcome == "completed" and now - self.last_signal >= self.recovery_seconds:
                self.limit = min(float(self.max_inflight), self.limit + 1)
            elif outcome == "drop":
                self.totals["mid_stream_drops"] += 1
                self.limit = max(1.0, self.limit / 2)
                self.last_signal = now
            self._release(ticket)

    def wait_ready(self, max_wait: float, on_wait=None, interval: float = 30.0) -> float:
        """Block a new attempt while the relay is cooling down; return seconds waited."""
        started = time.monotonic()
        with self.condition:
            while True:
                now = time.monotonic()
                remaining = self.cooldown_until - now
                if remaining <= 0 or now - started >= max_wait:
                    return now - started
                if on_wait is not None:
                    on_wait(remaining)
                self.condition.wait(min(remaining, interval, max_wait - (now - started)))

    def absorb(self, stats: RelayStats) -> None:
        with self.condition:
            self.totals["attempts"] += 1
            for key, value in stats.to_json().items():
                if isinstance(value, int) and not isinstance(value, bool):
                    self.totals[key] = self.totals.get(key, 0) + value
                elif key == "signals":
                    signals = self.totals.setdefault("signals", {})
                    for name, count in value.items():
                        signals[name] = signals.get(name, 0) + count

    def snapshot(self) -> dict:
        with self.condition:
            return {"config": self.config(), "state": self.state, "limit": int(max(1, self.limit)),
                    "totals": {key: dict(value) if isinstance(value, dict) else value
                               for key, value in self.totals.items()}}


_shared = RelayMonitor()
_shared_lock = threading.Lock()


def configure(max_inflight: int, **options) -> RelayMonitor:
    """Install the monitor every gateway in this controller process shares."""
    global _shared
    with _shared_lock:
        _shared = RelayMonitor(max_inflight, **options)
        return _shared


def shared() -> RelayMonitor:
    with _shared_lock:
        return _shared


__all__ = ["RelayMonitor", "RelayStats", "SIGNALS", "Ticket", "configure", "shared"]
