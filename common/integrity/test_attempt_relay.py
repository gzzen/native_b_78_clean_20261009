# Path: common/integrity/test_attempt_relay.py
# Purpose: Verify relay byte forwarding, inactivity, EOF and disconnected peer cleanup.
from __future__ import annotations

import socket
import threading
import unittest
from unittest.mock import MagicMock, patch

from common.integrity.attempt_relay import relay_stream


class AttemptRelayTests(unittest.TestCase):
    def test_bidirectional_bytes_and_clean_eof(self):
        client, relay_client = socket.socketpair()
        upstream, relay_upstream = socket.socketpair()
        for connection in (client, upstream):
            connection.settimeout(2)
        thread = threading.Thread(target=relay_stream, args=(relay_client, relay_upstream, 0.5))
        thread.start()
        try:
            client.sendall(b"request bytes")
            self.assertEqual(upstream.recv(65536), b"request bytes")
            upstream.sendall(b"data: response\n\n")
            self.assertEqual(client.recv(65536), b"data: response\n\n")
            client.shutdown(socket.SHUT_WR)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        finally:
            thread.join(timeout=2)
            for connection in (client, relay_client, upstream, relay_upstream):
                connection.close()

    def test_reset_or_broken_pipe_in_either_direction_does_not_raise(self):
        for error in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            for direction in ("client", "upstream"):
                for operation in ("recv", "sendall"):
                    with self.subTest(error=error, direction=direction, operation=operation):
                        client, upstream = MagicMock(), MagicMock()
                        source = client if direction == "client" else upstream
                        target = upstream if source is client else client
                        source.recv.return_value = b"payload"
                        broken = source if operation == "recv" else target
                        getattr(broken, operation).side_effect = error("disconnected")
                        with patch("common.integrity.attempt_relay.select.select", return_value=([source], [], [])):
                            relay_stream(client, upstream)

    def test_idle_connection_stops(self):
        client, upstream = MagicMock(), MagicMock()
        with patch("common.integrity.attempt_relay.select.select", return_value=([], [], [])):
            relay_stream(client, upstream)
        client.recv.assert_not_called()
        upstream.recv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
