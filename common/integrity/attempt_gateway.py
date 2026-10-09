"""Host-only per-attempt gateway. Never mounted in the solver container."""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import select
import shlex
import socket
import socketserver
import ssl
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_SCIENCE_HOSTS = ("mygene.info", "maayanlab.cloud", "www.ensembl.org",
                         "grch37.ensembl.org", "rest.ensembl.org")
PROJECT_ENV = Path(__file__).resolve().parents[2] / ".env"
DEFAULT_RELAY_BASE_URL = "https://xfastapi.ai"
# Reasoning models can stream nothing for minutes; matches OpenCode's chunk timeout.
STREAM_IDLE_SECONDS = 300


def project_setting(variable: str) -> str | None:
    """Read one host-only project value without executing or exporting it."""
    if value := os.environ.get(variable):
        return value
    if not PROJECT_ENV.is_file():
        return None
    result = None
    found = False
    for line in PROJECT_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if not separator or name.strip() != variable:
            continue
        if found:
            raise ValueError(f"duplicate {variable} in project .env")
        found = True
        try:
            values = shlex.split(value, comments=True, posix=True)
        except ValueError:
            raise ValueError(f"invalid {variable} in project .env") from None
        if len(values) > 1:
            raise ValueError(f"invalid {variable} in project .env")
        result = values[0] if values else None
    return result or None


def deepseek_api_key() -> str | None:
    return project_setting("DEEPSEEK_API_KEY")


def relay_base_url() -> str:
    """The OpenAI Responses relay; requests go to <base>/responses, as in Codex."""
    base = (project_setting("OPENAI_RELAY_BASE_URL") or DEFAULT_RELAY_BASE_URL).rstrip("/")
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError("OPENAI_RELAY_BASE_URL must be a plain https URL")
    return base


def public_address(host: str) -> str:
    addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(ip).is_global
                            or ipaddress.ip_address(ip).is_multicast
                            or ipaddress.ip_address(ip).is_reserved for ip in addresses):
        raise ValueError("non-public destination denied")
    return sorted(addresses)[0]


def tunnel(left: socket.socket, right: socket.socket) -> None:
    while True:
        ready, _, _ = select.select([left, right], [], [], 120)
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)


def tls_client_hello(connection: socket.socket, expected_host: str) -> bytes:
    """Fail closed on absent/mismatched SNI or ECH before forwarding a CONNECT.

    A pinned CDN IP alone is insufficient: clients could otherwise select a
    different tenant via TLS SNI. We accept one bounded, complete ClientHello.
    """
    def exact(length):
        result = b""
        while len(result) < length:
            chunk = connection.recv(length - len(result))
            if not chunk:
                raise ValueError("incomplete TLS hello")
            result += chunk
        return result

    header = exact(5)
    length = int.from_bytes(header[3:5], "big")
    if header[0] != 22 or not 1 <= length <= 16384:
        raise ValueError("TLS handshake required")
    body = exact(length)
    if body[0] != 1 or int.from_bytes(body[1:4], "big") != len(body) - 4:
        raise ValueError("complete ClientHello required")
    offset = 4 + 2 + 32
    offset += 1 + body[offset]
    offset += 2 + int.from_bytes(body[offset:offset + 2], "big")
    offset += 1 + body[offset]
    end = offset + 2 + int.from_bytes(body[offset:offset + 2], "big")
    offset += 2
    if end != len(body):
        raise ValueError("invalid TLS extensions")
    names = []
    while offset < end:
        kind = int.from_bytes(body[offset:offset + 2], "big")
        size = int.from_bytes(body[offset + 2:offset + 4], "big")
        extension = body[offset + 4:offset + 4 + size]
        if len(extension) != size or kind == 0xfe0d:
            raise ValueError("invalid or encrypted ClientHello")
        if kind == 0:
            if int.from_bytes(extension[:2], "big") != len(extension) - 2:
                raise ValueError("invalid SNI")
            cursor = 2
            while cursor < len(extension):
                name_type = extension[cursor]
                name_size = int.from_bytes(extension[cursor + 1:cursor + 3], "big")
                name = extension[cursor + 3:cursor + 3 + name_size]
                if len(name) != name_size or name_type != 0:
                    raise ValueError("invalid SNI name")
                names.append(name.decode("ascii").lower())
                cursor += 3 + name_size
        offset += 4 + size
    if names != [expected_host.lower()]:
        raise ValueError("SNI does not match approved destination")
    return header + body


def read_exact(stream, length: int) -> bytes:
    """Read until `length` bytes or EOF.

    The handler's unbuffered rfile returns after a single recv(), which holds at
    most one socket buffer: large bodies arrive in several reads.
    """
    parts, left = [], length
    while left:
        chunk = stream.read(left)
        if not chunk:
            break
        parts.append(chunk)
        left -= len(chunk)
    return b"".join(parts)


def dial(host: str, port: int, proxy: str | None = None) -> socket.socket:
    # Resolve once, reject every non-global answer, connect to a pinned address.
    address = public_address(host)
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme != "http" or not parsed.hostname or parsed.username:
            raise ValueError("trusted upstream proxy must be an unauthenticated http URL")
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=30)
        conn.set_tunnel(address, port)
        conn.connect()
        return conn.sock
    return socket.create_connection((address, port), timeout=30)


@dataclass
class GatewayConfig:
    token: str
    model: str
    upstream: str
    credential: str
    account_id: str | None = None
    science_hosts: tuple[str, ...] = DEFAULT_SCIENCE_HOSTS
    denied_hosts: tuple[str, ...] = ()
    proxy: str | None = None
    model_path: str = "/chat/completions"

    @classmethod
    def from_host(cls, token: str, model: str, provider: str = "deepseek") -> "GatewayConfig":
        account = None
        if provider == "deepseek":
            key, base, path, variable = deepseek_api_key(), "https://api.deepseek.com/v1", "/chat/completions", "DEEPSEEK_API_KEY"
        elif provider == "openai-relay":
            key, base, path, variable = project_setting("OPENAI_RELAY_API_KEY"), relay_base_url(), "/responses", "OPENAI_RELAY_API_KEY"
        else:
            raise ValueError("unsupported model provider")
        if not key:
            raise RuntimeError(f"set {variable} in project .env or on the controller host; it is never mounted into attempts")
        hosts = tuple(x.strip().lower() for x in os.environ.get(
            "NATIVE_B_SCIENCE_HOSTS", ",".join(DEFAULT_SCIENCE_HOSTS)).split(",") if x.strip())
        denied = tuple(x.strip().lower() for x in os.environ.get("NATIVE_B_EVALUATOR_HOSTS", "").split(",") if x.strip())
        evaluator = urlsplit(os.environ.get("EVALUATOR_API_URL", "")).hostname
        if evaluator:
            denied += (evaluator.lower(),)
        return cls(token, model, base, key, account, hosts, denied,
                   os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"), path)


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        request.settimeout(STREAM_IDLE_SECONDS)
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class AttemptGateway:
    def __init__(self, socket_path: Path, config: GatewayConfig):
        self.config = config
        self.model_calls = 0
        self.lock = threading.Lock()
        self.server = UnixServer(str(socket_path), self.handler())
        socket_path.chmod(0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def handler(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            # Unbuffered input avoids losing CONNECT payloads to read-ahead.
            rbufsize = 0

            def log_message(self, *_):
                pass  # No credentials, bodies or scientific results in logs.

            def fail(self, code=403, kind="gateway_forbidden"):
                # A typed JSON body keeps gateway refusals distinguishable from
                # provider errors in client logs.
                self.close_connection = True
                body = json.dumps({"error": {"type": kind, "message": "attempt gateway: " + kind,
                                             "status": code}}).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # The client may already have disconnected.

            def science_allowed(self, host, port):
                return (host and host.lower() in gateway.config.science_hosts
                        and host.lower() not in gateway.config.denied_hosts and port in (80, 443))

            def do_CONNECT(self):
                try:
                    parsed = urlsplit("//" + self.path)
                    if not self.science_allowed(parsed.hostname, parsed.port) or parsed.port != 443:
                        return self.fail()
                    upstream = dial(parsed.hostname, parsed.port, gateway.config.proxy)
                except Exception:
                    return self.fail()
                try:
                    self.send_response(200)
                    self.end_headers()
                    upstream.sendall(tls_client_hello(self.connection, parsed.hostname))
                    tunnel(self.connection, upstream)
                except (ValueError, OSError, IndexError, UnicodeError):
                    pass
                finally:
                    upstream.close()

            def do_GET(self):
                self.forward()

            def do_POST(self):
                self.forward()

            def forward(self):
                connection = None
                response_started = False
                try:
                    if self.headers.get("Transfer-Encoding"):
                        return self.fail(400, "gateway_transfer_encoding_unsupported")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= 16 * 1024 * 1024:
                        return self.fail(413, "gateway_body_too_large")
                    body = read_exact(self.rfile, length)
                    if len(body) != length:
                        return self.fail(400, "gateway_short_body")  # Client disconnected mid-body.
                    headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
                    is_model = self.path == "/model" + gateway.config.model_path
                    if is_model:
                        if self.command != "POST" or self.headers.get("Authorization") != "Bearer " + gateway.config.token:
                            return self.fail()
                        payload = json.loads(body)
                        if payload.get("model") != gateway.config.model:
                            return self.fail()
                        with gateway.lock:
                            gateway.model_calls += 1
                            if gateway.model_calls > 256:
                                return self.fail(429, "gateway_model_call_limit")
                        target = urlsplit(gateway.config.upstream + self.path.removeprefix("/model"))
                        if target.scheme != "https" or target.hostname != urlsplit(gateway.config.upstream).hostname:
                            return self.fail()
                        headers["Authorization"] = "Bearer " + gateway.config.credential
                    else:
                        target = urlsplit(self.path)
                        if (target.scheme != "http" or target.username or target.fragment
                                or not self.science_allowed(target.hostname, target.port or 80)):
                            return self.fail()
                        # Never inject/forward model credentials into scientific requests.
                    port = target.port or (443 if target.scheme == "https" else 80)
                    sock = dial(target.hostname, port, gateway.config.proxy)
                    sock.settimeout(STREAM_IDLE_SECONDS)  # dial's 30 s is for connecting only.
                    if target.scheme == "https":
                        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.hostname)
                    connection = http.client.HTTPConnection(target.hostname, port, timeout=STREAM_IDLE_SECONDS)
                    connection.sock = sock
                    headers["Host"] = target.hostname
                    connection.request(self.command, target.path + ("?" + target.query if target.query else ""), body=body, headers=headers)
                    response = connection.getresponse()
                    response_started = True
                    self.send_response(response.status)
                    for key in ("Content-Type", "Content-Length"):
                        value = response.getheader(key)
                        if value:
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except Exception:
                    # Do not echo upstream exceptions: they may contain auth or host details.
                    # After headers start, close the stream; a second HTTP response
                    # would corrupt it and may write to an already closed socket.
                    if not response_started:
                        self.fail(502, "gateway_upstream_error")
                finally:
                    self.close_connection = True
                    if connection:
                        connection.close()

        return Handler
