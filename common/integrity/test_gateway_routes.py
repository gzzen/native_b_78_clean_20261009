# Path: common/integrity/test_gateway_routes.py
# Purpose: Verify each model provider is forwarded only on its own route, host and credential.
from __future__ import annotations

import io
import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from common.integrity.attempt_gateway import STREAM_IDLE_SECONDS, AttemptGateway, GatewayConfig

DEEPSEEK = GatewayConfig("attempt-token", "deepseek-flash", "https://api.deepseek.com/v1", "deepseek-key")
RELAY = GatewayConfig("attempt-token", "gpt-5.6-sol", "https://xfastapi.ai", "relay-key",
                      model_path="/responses")


class GatewayRouteTests(unittest.TestCase):
    def forward(self, config, path, model, token="attempt-token"):
        gateway = object.__new__(AttemptGateway)
        gateway.config = config
        gateway.model_calls = 0
        gateway.lock = threading.Lock()
        handler = object.__new__(gateway.handler())
        body = json.dumps({"model": model, "input": []}).encode()
        handler.command, handler.path = "POST", path
        handler.headers = {"Content-Length": str(len(body)), "Authorization": "Bearer " + token}
        handler.rfile, handler.wfile = io.BytesIO(body), MagicMock()
        handler.send_response, handler.send_header, handler.end_headers = MagicMock(), MagicMock(), MagicMock()
        handler.close_connection = False
        connection = MagicMock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.getheader.return_value = None
        connection.getresponse.return_value.read1.side_effect = [b""]
        with patch("common.integrity.attempt_gateway.dial") as dial, \
                patch("common.integrity.attempt_gateway.ssl.create_default_context"), \
                patch("common.integrity.attempt_gateway.http.client.HTTPConnection",
                      return_value=connection) as http_connection:
            handler.forward()
        return handler, dial, http_connection, connection, body

    def test_relay_forwards_responses_with_relay_key(self):
        handler, dial, http_connection, connection, body = self.forward(RELAY, "/model/responses", "gpt-5.6-sol")
        handler.send_response.assert_called_once_with(200)
        dial.assert_called_once_with("xfastapi.ai", 443, None)
        dial.return_value.settimeout.assert_called_once_with(STREAM_IDLE_SECONDS)
        method, target = connection.request.call_args.args
        headers = connection.request.call_args.kwargs["headers"]
        self.assertEqual((method, target), ("POST", "/responses"))
        self.assertEqual(connection.request.call_args.kwargs["body"], body)
        self.assertEqual(headers["Authorization"], "Bearer relay-key")
        self.assertEqual(headers["Host"], "xfastapi.ai")

    def test_deepseek_route_is_unchanged(self):
        handler, dial, _, connection, _ = self.forward(DEEPSEEK, "/model/chat/completions", "deepseek-flash")
        handler.send_response.assert_called_once_with(200)
        dial.assert_called_once_with("api.deepseek.com", 443, None)
        self.assertEqual(connection.request.call_args.args, ("POST", "/v1/chat/completions"))
        self.assertEqual(connection.request.call_args.kwargs["headers"]["Authorization"], "Bearer deepseek-key")

    def test_wrong_route_model_or_token_never_reaches_upstream(self):
        for config, path, model, token in (
                (RELAY, "/model/chat/completions", "gpt-5.6-sol", "attempt-token"),
                (DEEPSEEK, "/model/responses", "deepseek-flash", "attempt-token"),
                (RELAY, "/model/responses", "gpt-5.5", "attempt-token"),
                (RELAY, "/model/responses", "gpt-5.6-sol", "wrong-token")):
            with self.subTest(path=path, model=model, token=token):
                handler, dial, _, _, _ = self.forward(config, path, model, token)
                handler.send_response.assert_called_once_with(403)
                dial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
