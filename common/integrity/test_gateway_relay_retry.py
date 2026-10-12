# Path: common/integrity/test_gateway_relay_retry.py
# Purpose: Verify the gateway retries relay refusals only before Codex sees a byte, without paid calls.
from __future__ import annotations

import io
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from common.integrity.attempt_gateway import AttemptGateway, GatewayConfig, refusal_signal, sse_events
from common.integrity.relay_monitor import RelayMonitor, RelayStats


def sse(*events: dict) -> bytes:
    return b"".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode() for event in events)


CREATED = {"type": "response.created", "response": {"id": "resp"}}
OUTPUT = {"type": "response.output_item.added", "output_index": 0, "item": {"type": "reasoning"}}
COMPLETED = {"type": "response.completed", "response": {"id": "resp"}}
BUSY = {"type": "error", "code": "server_is_overloaded", "message": "Server busy, host-only-key detail"}
FAILED = {"type": "response.failed", "response": {"id": "resp", "error": {"code": "server_error",
                                                                           "message": "upstream died"}}}
SUCCESS = sse(CREATED, OUTPUT, COMPLETED)


def upstream(status: int = 200, chunks=(), body: bytes = b"", headers: dict | None = None) -> MagicMock:
    connection = MagicMock()
    response = connection.getresponse.return_value
    response.status = status
    response.getheader.side_effect = lambda key: (headers or {}).get(
        key, "text/event-stream" if key == "Content-Type" and status == 200 else None)
    response.read1.side_effect = [*chunks, b""]
    response.read.return_value = body
    return connection


class GatewayRelayRetryTests(unittest.TestCase):
    def setUp(self):
        self.gateway = object.__new__(AttemptGateway)
        self.gateway.config = GatewayConfig("temporary-token", "gpt-5.6-sol", "https://xfastapi.ai", "host-only-key")
        self.gateway.model_calls = 0
        self.gateway.lock = threading.Lock()
        self.gateway.monitor = RelayMonitor(2, base_backoff=0.01, max_backoff=0.02)
        self.gateway.stats = RelayStats()

    def forward(self, connections, stream=True):
        handler = object.__new__(self.gateway.handler())
        handler.command, handler.path = "POST", "/model/responses"
        body = json.dumps({"model": "gpt-5.6-sol", "input": [], "stream": stream}).encode()
        handler.headers = {"Content-Length": str(len(body)), "Authorization": "Bearer temporary-token"}
        handler.rfile, handler.wfile = io.BytesIO(body), MagicMock()
        handler.send_response, handler.send_header, handler.end_headers = MagicMock(), MagicMock(), MagicMock()
        handler.close_connection = False
        with patch("common.integrity.attempt_gateway.dial") as dial, \
                patch("common.integrity.attempt_gateway.ssl.create_default_context"), \
                patch("common.integrity.attempt_gateway.http.client.HTTPConnection", side_effect=connections):
            handler.forward()
        sent = b"".join(call.args[0] for call in handler.wfile.write.call_args_list)
        self.assertNotIn(b"host-only-key", sent.replace(b"host-only-key detail", b""))
        return handler, dial, sent

    def test_http_429_then_success_reaches_codex_as_one_clean_stream(self):
        refused = upstream(429, body=b'{"error": {"code": "rate_limit_exceeded", "message": "busy"}}',
                           headers={"Retry-After": "0"})
        handler, dial, sent = self.forward([refused, upstream(chunks=[SUCCESS])])
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(sent, SUCCESS)
        self.assertEqual(dial.call_count, 2)
        refused.close.assert_called_once()
        stats = self.gateway.stats.to_json()
        self.assertEqual((stats["codex_requests"], stats["upstream_tries"], stats["transparent_retries"]), (1, 2, 1))
        self.assertEqual(stats["signals"], {"http_429": 1})
        self.assertEqual(stats["error_codes"], {"rate_limit_exceeded": 1})
        self.assertEqual(stats["completed_streams"], 1)
        self.assertEqual(self.gateway.monitor.inflight, 0)

    def test_in_stream_capacity_error_before_output_is_retried(self):
        for first in (sse(BUSY), sse(CREATED, BUSY), sse(CREATED, FAILED)):
            with self.subTest(first=first[:40]):
                self.setUp()
                handler, dial, sent = self.forward([upstream(chunks=[first]), upstream(chunks=[SUCCESS])])
                handler.send_response.assert_called_once_with(200)
                self.assertEqual(sent, SUCCESS)
                self.assertEqual(dial.call_count, 2)
                self.assertEqual(self.gateway.stats.transparent_retries, 1)

    def test_split_chunks_are_held_until_the_first_output_event(self):
        chunks = [SUCCESS[:7], SUCCESS[7:60], SUCCESS[60:]]
        handler, dial, sent = self.forward([upstream(chunks=chunks)])
        self.assertEqual(sent, SUCCESS)
        dial.assert_called_once()
        self.assertEqual(self.gateway.stats.completed_streams, 1)

    def test_failure_after_output_passes_through_without_retry(self):
        stream = sse(CREATED, OUTPUT, BUSY)
        handler, dial, sent = self.forward([upstream(chunks=[stream[:150], stream[150:]])])
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(sent, stream)
        dial.assert_called_once()
        stats = self.gateway.stats.to_json()
        self.assertEqual((stats["transparent_retries"], stats["mid_stream_drops"]), (0, 1))
        self.assertEqual(self.gateway.monitor.totals["mid_stream_drops"], 1)

    def test_eof_without_completion_counts_as_drop(self):
        stream = sse(CREATED, OUTPUT)
        _, _, sent = self.forward([upstream(chunks=[stream])])
        self.assertEqual(sent, stream)
        self.assertEqual(self.gateway.stats.mid_stream_drops, 1)

    def test_exhausted_budget_forwards_the_original_refusal(self):
        body = b'{"error": {"code": "server_is_overloaded"}}'
        with patch("common.integrity.attempt_gateway.PRE_FIRST_BYTE_SECONDS", 0):
            handler, dial, sent = self.forward([upstream(429, body=body)])
        handler.send_response.assert_called_once_with(429)
        handler.send_header.assert_any_call("Content-Length", str(len(body)))
        self.assertEqual(sent, body)
        dial.assert_called_once()
        self.assertEqual(self.gateway.stats.refusals_forwarded, 1)

    def test_exhausted_budget_forwards_in_stream_refusal_verbatim(self):
        with patch("common.integrity.attempt_gateway.PRE_FIRST_BYTE_SECONDS", 0):
            handler, _, sent = self.forward([upstream(chunks=[sse(BUSY)])])
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(sent, sse(BUSY))

    def test_non_retryable_http_error_is_forwarded_once(self):
        body = b'{"error": {"code": "invalid_request_error"}}'
        handler, dial, sent = self.forward([upstream(400, body=body)])
        handler.send_response.assert_called_once_with(400)
        self.assertEqual(sent, body)
        dial.assert_called_once()

    def test_non_streaming_model_request_never_reaches_upstream(self):
        handler, dial, sent = self.forward([], stream=False)
        handler.send_response.assert_called_once_with(400)
        self.assertEqual(json.loads(sent)["error"]["type"], "gateway_stream_required")
        dial.assert_not_called()

    def test_connect_error_is_retried_without_leaking_details(self):
        broken = MagicMock()
        broken.getresponse.side_effect = OSError("host-only-key")
        handler, dial, sent = self.forward([broken, upstream(chunks=[SUCCESS])])
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(sent, SUCCESS)
        self.assertEqual(self.gateway.stats.signals, {"connect_error": 1})


class SseParsingTests(unittest.TestCase):
    def test_events_parse_across_crlf_and_partial_buffers(self):
        events, rest = sse_events(b"event: x\r\ndata: {\"type\": \"response.created\"}\r\n\r\ndata: {\"ty")
        self.assertEqual([name for name, _ in events], ["response.created"])
        self.assertEqual(rest, b"data: {\"ty")
        events, rest = sse_events(b": keepalive\n\nevent: ping\n\n")
        self.assertEqual([name for name, _ in events], ["ping"])

    def test_refusal_classification(self):
        self.assertEqual(refusal_signal("error", BUSY), ("sse_capacity", "server_is_overloaded"))
        self.assertEqual(refusal_signal("response.failed", FAILED), ("sse_server_error", "server_error"))
        self.assertIsNone(refusal_signal("error", {"type": "error", "code": "invalid_prompt", "message": "bad"}))
        self.assertIsNone(refusal_signal("response.output_item.added", OUTPUT))


if __name__ == "__main__":
    unittest.main()
