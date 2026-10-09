"""Container-local TCP to Unix-socket relay; contains no credentials/policy."""
from __future__ import annotations
import os
import select
import socket
import socketserver
import subprocess
import sys
import threading


class Relay(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def relay_stream(client, upstream, timeout=300):
    """Forward bytes until EOF, inactivity, or a peer disconnects."""
    try:
        while True:
            ready, _, _ = select.select([client, upstream], [], [], timeout)
            if not ready:
                return
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                (upstream if source is client else client).sendall(data)
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
        return  # Cancellation/connection closure is a normal relay lifecycle.


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.connect("/boundary/socket/gateway.sock")
            relay_stream(self.request, upstream)


def main():
    with Relay(("127.0.0.1", 8765), Handler) as relay:
        threading.Thread(target=relay.serve_forever, daemon=True).start()
        code = subprocess.call(sys.argv[1:], env=dict(os.environ))
        relay.shutdown()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
