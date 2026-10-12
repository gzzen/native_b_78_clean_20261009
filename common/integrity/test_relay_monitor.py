# Path: common/integrity/test_relay_monitor.py
# Purpose: Verify relay admission control, backoff, circuit breaking and content-free stats.
from __future__ import annotations

import threading
import time
import unittest

from common.integrity.relay_monitor import RelayMonitor, RelayStats


def far() -> float:
    return time.monotonic() + 5


class RelayMonitorTests(unittest.TestCase):
    def test_backoff_doubles_with_bounded_jitter_and_honours_retry_after(self):
        for rng, factor in ((lambda: 0.0, 0.75), (lambda: 1.0, 1.25)):
            monitor = RelayMonitor(4, base_backoff=1, max_backoff=8, breaker_threshold=99, rng=rng)
            delays = []
            for _ in range(5):
                now = time.monotonic()
                delays.append(monitor.refused(monitor.admit(far())) - now)
                monitor.cooldown_until = 0
            for observed, expected in zip(delays, (1, 2, 4, 8, 8)):
                self.assertAlmostEqual(observed, expected * factor, delta=0.05)
        monitor = RelayMonitor(1, base_backoff=1, max_backoff=8, rng=lambda: 0.5)
        now = time.monotonic()
        self.assertAlmostEqual(monitor.refused(monitor.admit(far()), retry_after=5) - now, 5, delta=0.05)
        monitor.cooldown_until = 0
        now = time.monotonic()
        self.assertAlmostEqual(monitor.refused(monitor.admit(far()), retry_after=600) - now, 8, delta=0.05)

    def test_limit_halves_on_refusal_and_recovers_one_by_one(self):
        monitor = RelayMonitor(4, base_backoff=0.001, max_backoff=0.001, breaker_threshold=99, recovery_seconds=0)
        monitor.refused(monitor.admit(far()))
        self.assertEqual(monitor.snapshot()["limit"], 2)
        monitor.refused(monitor.admit(far()))
        monitor.refused(monitor.admit(far()))
        self.assertEqual(monitor.snapshot()["limit"], 1)
        time.sleep(0.01)
        for expected in (2, 3, 4, 4):
            ticket = monitor.admit(far())
            monitor.committed(ticket)
            monitor.finished(ticket, "completed")
            self.assertEqual(monitor.snapshot()["limit"], expected)
        ticket = monitor.admit(far())
        monitor.finished(ticket, "drop")
        self.assertEqual(monitor.snapshot()["limit"], 2)

    def test_admission_waits_for_a_free_slot(self):
        monitor = RelayMonitor(1)
        first = monitor.admit(far())
        admitted = threading.Event()
        thread = threading.Thread(target=lambda: (monitor.admit(far()), admitted.set()))
        thread.start()
        self.assertFalse(admitted.wait(0.1))
        monitor.finished(first, "completed")
        self.assertTrue(admitted.wait(1))
        thread.join()

    def test_admission_past_deadline_is_forced_not_refused(self):
        monitor = RelayMonitor(1)
        monitor.admit(far())
        started = time.monotonic()
        ticket = monitor.admit(time.monotonic() + 0.05)
        self.assertTrue(ticket.forced)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(monitor.inflight, 2)

    def test_cooldown_blocks_admission_and_wait_ready(self):
        monitor = RelayMonitor(2, base_backoff=0.2, max_backoff=0.2, jitter=0)
        monitor.refused(monitor.admit(far()))
        started = time.monotonic()
        monitor.finished(monitor.admit(far()), "completed")
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        monitor.refused(monitor.admit(far()))
        calls = []
        waited = monitor.wait_ready(5, on_wait=calls.append, interval=0.05)
        self.assertGreaterEqual(waited, 0.15)
        self.assertTrue(calls)
        self.assertLess(monitor.wait_ready(5), 0.01)
        monitor.refused(monitor.admit(far()))
        self.assertLess(monitor.wait_ready(0.05), 0.1)  # max_wait bounds an outage wait.

    def test_breaker_opens_then_admits_one_probe_and_closes_on_acceptance(self):
        monitor = RelayMonitor(4, base_backoff=0.02, max_backoff=0.02, breaker_threshold=3, jitter=0)
        for _ in range(3):
            monitor.refused(monitor.admit(far()))
        self.assertEqual(monitor.state, "open")
        self.assertEqual(monitor.totals["breaker_opened"], 1)
        probe = monitor.admit(far())
        self.assertTrue(probe.probe)
        self.assertEqual(monitor.state, "half_open")
        blocked = monitor.admit(time.monotonic() + 0.05)
        self.assertTrue(blocked.forced)  # A second request waits for the probe.
        monitor.finished(blocked, "aborted")
        monitor.committed(probe)
        self.assertEqual(monitor.state, "closed")
        monitor.finished(probe, "completed")
        ticket = monitor.admit(far())
        self.assertFalse(ticket.probe or ticket.forced)

    def test_refused_probe_reopens_with_longer_backoff(self):
        monitor = RelayMonitor(4, base_backoff=0.02, max_backoff=1, breaker_threshold=1, jitter=0)
        monitor.refused(monitor.admit(far()))
        probe = monitor.admit(far())
        now = time.monotonic()
        until = monitor.refused(probe)
        self.assertEqual(monitor.state, "open")
        self.assertAlmostEqual(until - now, 0.04, delta=0.01)
        self.assertFalse(monitor.probe_inflight)

    def test_release_is_idempotent(self):
        monitor = RelayMonitor(2)
        ticket = monitor.admit(far())
        monitor.refused(ticket)
        monitor.finished(ticket, "aborted")
        self.assertEqual(monitor.inflight, 0)

    def test_stats_are_content_free_and_absorbed_into_totals(self):
        stats = RelayStats()
        stats.add("codex_requests")
        stats.add("upstream_tries", 2)
        stats.signal("http_429", "rate_limit_exceeded")
        stats.signal("sse_capacity", "Server busy: secret text")
        stats.first_byte(1.5)
        payload = stats.to_json()
        self.assertEqual(payload["error_codes"], {"rate_limit_exceeded": 1})
        self.assertEqual(payload["first_byte_seconds"], {"count": 1, "max": 1.5, "mean": 1.5})
        monitor = RelayMonitor(1)
        monitor.absorb(stats)
        monitor.absorb(stats)
        totals = monitor.snapshot()["totals"]
        self.assertEqual((totals["attempts"], totals["upstream_tries"]), (2, 4))
        self.assertEqual(totals["signals"], {"http_429": 2, "sse_capacity": 2})


if __name__ == "__main__":
    unittest.main()
