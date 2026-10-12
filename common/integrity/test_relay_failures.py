# Path: common/integrity/test_relay_failures.py
# Purpose: Verify relay failure classification on the event shapes seen in the bix-10 diagnostic.
from __future__ import annotations

import unittest

from common.integrity.codex_agent import normalize_event
from common.integrity.relay_failures import RELAY_CAPACITY, RELAY_STREAM_DROP, classify_relay_failure

CAPACITY = "Selected model is at capacity. Please try a different model."
RECONNECT = "Reconnecting... 1/2 (stream disconnected before completion: stream closed before response.completed)"


def records(*raws):
    return [{"elapsed_seconds": index, "event": normalize_event(raw)} for index, raw in enumerate(raws)]


def failed(message):
    return [{"type": "error", "message": message}, {"type": "turn.failed", "error": {"message": message}}]


START = [{"type": "thread.started", "thread_id": "t"}, {"type": "turn.started"}]


class RelayFailureTests(unittest.TestCase):
    def test_immediate_capacity_refusal(self):  # bix-10-q7
        self.assertEqual(classify_relay_failure(1, records(*START, *failed(CAPACITY))), RELAY_CAPACITY)

    def test_capacity_after_recovered_reconnect(self):  # bix-10-q4
        events = records(*START, {"type": "error", "message": RECONNECT},
                         {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
                         *failed(CAPACITY))
        self.assertEqual(classify_relay_failure(1, events), RELAY_CAPACITY)

    def test_capacity_signalled_by_exhausted_retries(self):
        for message in ("exceeded retry limit, last status: 429 Too Many Requests",
                        "unexpected status 503 Service Unavailable: busy", "Server is busy, try later"):
            with self.subTest(message=message):
                self.assertEqual(classify_relay_failure(1, records(*START, *failed(message))), RELAY_CAPACITY)

    def test_mid_stream_drop_after_reconnects(self):
        for message in ("stream disconnected before completion: error decoding response body",
                        "exceeded retry limit, last status: 524 <unknown status code>",
                        "unexpected status 502 Bad Gateway: attempt gateway: gateway_upstream_error"):
            with self.subTest(message=message):
                self.assertEqual(classify_relay_failure(1, records(*START, *failed(message))), RELAY_STREAM_DROP)

    def test_last_error_decides_when_codex_exits_without_turn_failed(self):
        self.assertEqual(classify_relay_failure(1, records(*START, {"type": "error", "message": CAPACITY})),
                         RELAY_CAPACITY)

    def test_model_side_failures_timeouts_and_completed_turns_are_not_relay_failures(self):
        recovered = records(*START, {"type": "error", "message": RECONNECT}, {"type": "turn.completed", "usage": {}})
        self.assertIsNone(classify_relay_failure(0, recovered))
        self.assertIsNone(classify_relay_failure(1, recovered))  # Exit after a completed turn.
        self.assertIsNone(classify_relay_failure(None, records(*START, *failed(CAPACITY))))  # Timeout.
        self.assertIsNone(classify_relay_failure(1, records(*START, *failed("context window exceeded"))))
        self.assertIsNone(classify_relay_failure(1, records(*START)))
        # A non-relay final failure is decisive even after an earlier recovered reconnect.
        self.assertIsNone(classify_relay_failure(1, records(*START, {"type": "error", "message": RECONNECT},
                                                            *failed("invalid_prompt"))))


if __name__ == "__main__":
    unittest.main()
