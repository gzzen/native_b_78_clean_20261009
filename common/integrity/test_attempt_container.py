# Path: common/integrity/test_attempt_container.py
# Purpose: Verify OpenCode mounts and host-only DeepSeek credentials.
from __future__ import annotations
import http.client
import json
import socket
import tempfile
import unittest
import threading
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from common.integrity.attempt_gateway import AttemptGateway, GatewayConfig, public_address, tls_client_hello
from common.integrity.attempt_container import docker_command, validate_workspace_outputs


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=5)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(self.path))


class AttemptBoundaryTests(unittest.TestCase):
    def test_runtime_helper_links_allowed_but_not_as_audit_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home/.local/share/opencode"; home.mkdir(parents=True)
            (home / "applypatch").symlink_to("/opt/opencode/opencode")
            validate_workspace_outputs(root)
            (root / "analysis").symlink_to(root / "home", target_is_directory=True)
            with self.assertRaises(RuntimeError):
                validate_workspace_outputs(root)

    def test_host_postprocessing_cannot_follow_private_output_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "attempt"; workspace.mkdir()
            private = root / "private.json"; private.write_text("host-only")
            (workspace / "run_manifest.json").symlink_to(private)
            with self.assertRaises(RuntimeError):
                validate_workspace_outputs(workspace)
            self.assertEqual(private.read_text(), "host-only")

    def test_connect_sni_cannot_select_another_cdn_tenant(self):
        incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
        client = ssl.create_default_context().wrap_bio(incoming, outgoing, server_side=False,
                                                       server_hostname="science.example")
        try:
            client.do_handshake()
        except ssl.SSLWantReadError:
            pass
        hello = outgoing.read()
        for expected, allowed in (("science.example", True), ("other.example", False)):
            left, right = socket.socketpair()
            try:
                left.sendall(hello)
                if allowed:
                    self.assertEqual(tls_client_hello(right, expected), hello)
                else:
                    with self.assertRaises(ValueError):
                        tls_client_hello(right, expected)
            finally:
                left.close(); right.close()

    def test_model_credential_injected_only_outside_container(self):
        seen = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers(); self.wfile.write(b"{}")

            do_GET = do_POST

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "gateway.sock"
                config = GatewayConfig("short-token", "fixed-model", "https://api.deepseek.com/v1", "host-secret",
                                       science_hosts=("science.example",))
                gateway = AttemptGateway(path, config); gateway.start()
                try:
                    with patch("common.integrity.attempt_gateway.dial", side_effect=lambda *a: socket.create_connection(upstream.server_address)), \
                         patch("common.integrity.attempt_gateway.ssl.create_default_context") as tls:
                        tls.return_value.wrap_socket.side_effect = lambda sock, **kwargs: sock
                        connection = UnixConnection(path)
                        connection.request("POST", "/model/chat/completions", '{"model":"fixed-model"}',
                                           {"Authorization":"Bearer short-token"})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200); self.assertEqual(response.read(), b"{}")
                        connection.close()
                        connection = UnixConnection(path)
                        connection.request("GET", "http://science.example/data", headers={"Authorization":"Bearer short-token"})
                        response = connection.getresponse(); response.read(); connection.close()
                    self.assertEqual(seen, [("/v1/chat/completions", "Bearer host-secret"), ("/data", None)])
                finally:
                    gateway.close()
        finally:
            upstream.shutdown(); upstream.server_close()

    def test_dns_private_or_mixed_answers_fail_closed(self):
        for ips in (("127.0.0.1",), ("10.0.0.1",), ("169.254.169.254",),
                    ("::1",), ("224.0.0.1",), ("ff02::1",), ("8.8.8.8", "192.168.1.1")):
            rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]
            with patch("socket.getaddrinfo", return_value=rows), self.assertRaises(ValueError):
                public_address("approved.example")

    def test_gateway_denies_unknown_local_and_evaluator_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.sock"
            config = GatewayConfig("short-token", "fixed-model", "https://api.deepseek.com/v1", "host-secret",
                                   science_hosts=("science.example",), denied_hosts=("science.example",))
            gateway = AttemptGateway(path, config)
            gateway.start()
            try:
                cases = [("CONNECT", "127.0.0.1:8000", None, {}),
                         ("CONNECT", "science.example:443", None, {}),
                         ("CONNECT", "unknown.example:443", None, {}),
                         ("GET", "http://169.254.169.254/latest/meta-data", None, {}),
                         ("POST", "/model/chat/completions", '{"model":"fixed-model"}', {}),
                         ("POST", "/model/chat/completions", '{"model":"wrong-model"}', {"Authorization":"Bearer short-token"})]
                for method, target, body, headers in cases:
                    with self.subTest(target=target):
                        connection = UnixConnection(path)
                        connection.request(method, target, body, headers)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 403)
                        self.assertNotIn(b"host-secret", response.read())
                        connection.close()
                self.assertEqual(gateway.model_calls, 0)
            finally:
                gateway.close()

    def test_docker_only_mounts_current_attempt_and_allowlisted_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "attempt"
            (workspace / "data").mkdir(parents=True)
            (workspace / "isolation_boundary.json").write_text("{}")
            schema, bash_env = root / "schema.json", root / "bash.sh"
            schema.write_text("{}"); bash_env.write_text("")
            prefix = root / "environment"; prefix.mkdir()
            binary = root / "opencode"; binary.write_text("linux binary")
            socket_dir = root / "gateway"; socket_dir.mkdir()
            command = ["opencode", "run", "--standalone", "--model", "deepseek/deepseek-flash", "--format", "json", "prompt"]
            argv = docker_command(command, {"CONDA_PREFIX":str(prefix), "BASH_ENV":str(bash_env),
                                  "DEEPSEEK_API_KEY":"real-secret", "EVALUATOR_API_URL":"hidden"},
                                  workspace, socket_dir, "test-container", "short-token", binary, "image", schema=schema)
            self.assertEqual(argv[argv.index("--network") + 1], "none")
            self.assertIn("--read-only", argv)
            self.assertNotIn("real-secret", " ".join(argv))
            self.assertNotIn("EVALUATOR_API_URL", " ".join(argv))
            mounts = [argv[i + 1] for i, value in enumerate(argv) if value == "--mount"]
            self.assertEqual(len(mounts), 10)
            self.assertTrue(any(f"src={(workspace / 'data').resolve()}" in item and item.endswith(",readonly") for item in mounts))
            self.assertFalse(any("docker.sock" in item or "auth.json" in item for item in mounts))


if __name__ == "__main__":
    unittest.main()
