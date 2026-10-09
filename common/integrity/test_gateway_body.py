# Path: common/integrity/test_gateway_body.py
# Purpose: Verify large request bodies survive the relay → Unix socket → gateway path intact.
from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from common.integrity.attempt_gateway import AttemptGateway, GatewayConfig
from common.integrity.attempt_relay import relay_stream


class GatewayBodyTests(unittest.TestCase):
    def setUp(self):
        # Short directory: AF_UNIX paths are limited to ~104 bytes on macOS.
        self.directory = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.directory.cleanup)
        self.socket_path = Path(self.directory.name) / "gateway.sock"
        config = GatewayConfig("temporary-token", "deepseek-flash",
                               "https://api.deepseek.com/v1", "host-only-key")
        self.gateway = AttemptGateway(self.socket_path, config)
        self.gateway.start()
        self.addCleanup(self.gateway.close)

    def read_response(self, connection):
        """Return (status, json body) once Content-Length bytes of body arrived."""
        connection.settimeout(5)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = connection.recv(65536)
            self.assertTrue(chunk, "gateway closed without a response")
            response += chunk
        head, _, payload = response.partition(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
        while len(payload) < int(headers["content-length"]):
            chunk = connection.recv(65536)
            self.assertTrue(chunk, "gateway closed mid-body")
            payload += chunk
        self.assertEqual(headers["content-type"], "application/json")
        return int(lines[0].split()[1]), json.loads(payload)

    def exchange(self, request: bytes):
        """Send through the production relay, exactly as the container does."""
        client, relay_client = socket.socketpair()
        relay_upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        relay_upstream.connect(str(self.socket_path))
        relay = threading.Thread(target=relay_stream, args=(relay_client, relay_upstream, 5))
        relay.start()

        def send():
            try:
                client.sendall(request)
            except OSError:
                pass  # The gateway may answer and close before the body is consumed.

        sender = threading.Thread(target=send)
        sender.start()
        try:
            return self.read_response(client)
        finally:
            client.close()
            sender.join(timeout=5)
            relay.join(timeout=5)
            for connection in (relay_client, relay_upstream):
                connection.close()

    def head(self, length: int) -> bytes:
        return ("POST /model/chat/completions HTTP/1.1\r\nHost: gateway\r\n"
                "Authorization: Bearer wrong-token\r\nContent-Type: application/json\r\n"
                f"Content-Length: {length}\r\n\r\n").encode()

    def test_large_body_is_read_completely_before_policy_check(self):
        # A wrong token makes a complete read end in 403 without dialling upstream;
        # a short read would end in 400 gateway_short_body instead.
        for size in (16 * 1024, 256 * 1024, 1024 * 1024):
            with self.subTest(size=size):
                body = json.dumps({"model": "deepseek-flash", "pad": "x" * size}).encode()
                status, payload = self.exchange(self.head(len(body)) + body)
                self.assertEqual(status, 403)
                self.assertEqual(payload["error"]["type"], "gateway_forbidden")

    def test_truncated_body_returns_gateway_error_not_empty_400(self):
        # The relay stops on a client half-close, so talk to the gateway socket directly.
        body = b'{"model": "deepseek-flash"}'
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.connect(str(self.socket_path))
            connection.sendall(self.head(len(body) + 1000) + body)
            connection.shutdown(socket.SHUT_WR)
            status, payload = self.read_response(connection)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["type"], "gateway_short_body")


if __name__ == "__main__":
    unittest.main()
