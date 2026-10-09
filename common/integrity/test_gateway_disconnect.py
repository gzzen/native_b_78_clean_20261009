# Path: common/integrity/test_gateway_disconnect.py
# Purpose: Verify gateway cleanup and valid HTTP responses when streamed connections fail.
from __future__ import annotations

import io
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from common.integrity.attempt_gateway import AttemptGateway, GatewayConfig


class GatewayDisconnectTests(unittest.TestCase):
    def setUp(self):
        gateway = object.__new__(AttemptGateway)
        gateway.config = GatewayConfig("temporary-token", "deepseek-flash",
                                       "https://api.deepseek.com/v1", "host-only-key")
        gateway.model_calls = 0
        gateway.lock = threading.Lock()
        self.handler = object.__new__(gateway.handler())
        self.handler.command = "POST"
        self.handler.path = "/model/chat/completions"
        body = json.dumps({"model": "deepseek-flash"}).encode()
        self.handler.headers = {"Content-Length": str(len(body)),
                                "Authorization": "Bearer temporary-token"}
        self.handler.rfile = io.BytesIO(body)
        self.handler.wfile = MagicMock()
        self.handler.send_response = MagicMock()
        self.handler.send_header = MagicMock()
        self.handler.end_headers = MagicMock()
        self.handler.close_connection = False
        self.connection = MagicMock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.getheader.return_value = None
        self.response.read1.side_effect = [b"data: response\n\n", b""]

    def forward(self):
        with patch("common.integrity.attempt_gateway.dial"), \
                patch("common.integrity.attempt_gateway.ssl.create_default_context"), \
                patch("common.integrity.attempt_gateway.http.client.HTTPConnection",
                      return_value=self.connection):
            self.handler.forward()

    def test_client_disconnect_during_body_does_not_send_second_response(self):
        for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            with self.subTest(error=error):
                self.setUp()
                self.handler.wfile.write.side_effect = error("disconnected")
                self.forward()
                self.handler.send_response.assert_called_once_with(200)
                self.connection.close.assert_called_once()
                self.assertTrue(self.handler.close_connection)

    def test_client_disconnect_during_headers_does_not_send_second_response(self):
        self.handler.end_headers.side_effect = BrokenPipeError("disconnected")
        self.forward()
        self.handler.send_response.assert_called_once_with(200)
        self.connection.close.assert_called_once()

    def test_upstream_error_after_headers_closes_stream_without_second_response(self):
        self.response.read1.side_effect = [b"data: partial\n\n", TimeoutError("upstream timeout")]
        self.forward()
        self.handler.send_response.assert_called_once_with(200)
        self.connection.close.assert_called_once()
        self.assertTrue(self.handler.close_connection)

    def test_upstream_error_before_headers_returns_502_without_exception_details(self):
        self.connection.getresponse.side_effect = TimeoutError("host-only-key")
        self.forward()
        self.handler.send_response.assert_called_once_with(502)
        self.handler.send_header.assert_any_call("Content-Type", "application/json")
        self.handler.wfile.write.assert_called_once()
        body = self.handler.wfile.write.call_args.args[0]
        self.assertEqual(json.loads(body)["error"]["type"], "gateway_upstream_error")
        self.assertNotIn(b"host-only-key", body)
        self.connection.close.assert_called_once()

    def test_disconnected_error_response_does_not_raise_again(self):
        self.handler.end_headers.side_effect = BrokenPipeError("disconnected")
        self.handler.fail(502)
        self.handler.send_response.assert_called_once_with(502)
        self.assertTrue(self.handler.close_connection)


if __name__ == "__main__":
    unittest.main()
