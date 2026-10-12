# Path: common/integrity/test_attempt_container.py
# Purpose: Verify Codex attempt mounts and host-only relay credentials.
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
from common.integrity.attempt_container import codex_binaries, docker_command, validate_workspace_outputs


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
            # Codex links helper aliases to its own binary under CODEX_HOME/tmp/arg0.
            home = root / "home/.codex/tmp/arg0"; home.mkdir(parents=True)
            (home / "apply_patch").symlink_to("/opt/codex/bin/codex")
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
                config = GatewayConfig("short-token", "fixed-model", "https://xfastapi.ai", "host-secret",
                                       science_hosts=("science.example",))
                gateway = AttemptGateway(path, config); gateway.start()
                try:
                    with patch("common.integrity.attempt_gateway.dial", side_effect=lambda *a: socket.create_connection(upstream.server_address)), \
                         patch("common.integrity.attempt_gateway.ssl.create_default_context") as tls:
                        tls.return_value.wrap_socket.side_effect = lambda sock, **kwargs: sock
                        connection = UnixConnection(path)
                        connection.request("POST", "/model/responses", '{"model":"fixed-model","stream":true}',
                                           {"Authorization":"Bearer short-token"})
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200); self.assertEqual(response.read(), b"{}")
                        connection.close()
                        connection = UnixConnection(path)
                        connection.request("GET", "http://science.example/data", headers={"Authorization":"Bearer short-token"})
                        response = connection.getresponse(); response.read(); connection.close()
                    self.assertEqual(seen, [("/responses", "Bearer host-secret"), ("/data", None)])
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
            config = GatewayConfig("short-token", "fixed-model", "https://xfastapi.ai", "host-secret",
                                   science_hosts=("science.example",), denied_hosts=("science.example",))
            gateway = AttemptGateway(path, config)
            gateway.start()
            try:
                cases = [("CONNECT", "127.0.0.1:8000", None, {}),
                         ("CONNECT", "science.example:443", None, {}),
                         ("CONNECT", "unknown.example:443", None, {}),
                         ("GET", "http://169.254.169.254/latest/meta-data", None, {}),
                         ("POST", "/model/responses", '{"model":"fixed-model"}', {}),
                         ("POST", "/model/chat/completions", '{"model":"fixed-model"}', {"Authorization":"Bearer short-token"}),
                         ("POST", "/model/responses", '{"model":"wrong-model"}', {"Authorization":"Bearer short-token"})]
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

    def test_codex_binaries_must_match_the_pinned_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = {name: Path(directory) / name for name in ("codex", "codex-code-mode-host")}
            for path in fake.values():
                path.write_text("not the pinned binary")
            with patch.dict("os.environ", {"NATIVE_B_CODEX_BINARY": str(fake["codex"]),
                                           "NATIVE_B_CODEX_CODE_MODE_HOST": str(fake["codex-code-mode-host"])}):
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    codex_binaries()
            with patch.dict("os.environ", {"NATIVE_B_CODEX_BINARY": str(Path(directory) / "absent")}):
                with self.assertRaisesRegex(RuntimeError, "offline bundle"):
                    codex_binaries()

    def test_docker_only_mounts_current_attempt_and_allowlisted_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "attempt"
            (workspace / "data").mkdir(parents=True)
            (workspace / "isolation_boundary.json").write_text("{}")
            codex_home = workspace / "home/.codex"; codex_home.mkdir(parents=True)
            config_path, catalog_path = codex_home / "config.toml", codex_home / "model_catalog.json"
            config_path.write_text(""); catalog_path.write_text("{}")
            schema, bash_env = root / "schema.json", root / "bash.sh"
            schema.write_text("{}"); bash_env.write_text("")
            prefix = root / "environment"; prefix.mkdir()
            binaries = {name: root / name for name in ("codex", "codex-code-mode-host")}
            for path in binaries.values():
                path.write_text("linux binary")
            socket_dir = root / "gateway"; socket_dir.mkdir()
            command = ["codex", "exec", "--json", "--model", "gpt-5.6-sol", "prompt"]
            argv = docker_command(command, {"CONDA_PREFIX": str(prefix), "BASH_ENV": str(bash_env),
                                  "OPENAI_API_KEY": "real-secret", "OPENAI_RELAY_API_KEY": "relay-secret",
                                  "CODEX_HOME": str(Path.home() / ".codex"), "EVALUATOR_API_URL": "hidden"},
                                  workspace, socket_dir, "test-container", "short-token", binaries, "image",
                                  schema=schema, config_path=config_path, catalog_path=catalog_path)
            joined = " ".join(argv)
            self.assertEqual(argv[argv.index("--network") + 1], "none")
            self.assertIn("--read-only", argv)
            for leaked in ("real-secret", "relay-secret", "EVALUATOR_API_URL", str(Path.home() / ".codex")):
                self.assertNotIn(leaked, joined)
            environment = dict(argv[i + 1].split("=", 1) for i, value in enumerate(argv) if value == "--env")
            self.assertEqual(environment["CODEX_HOME"], str(codex_home.resolve()))
            self.assertIn(":/opt/codex/bin:", environment["PATH"])
            self.assertEqual(argv[argv.index("/boundary/relay.py") + 1], "/opt/codex/bin/codex")
            mounts = [argv[i + 1] for i, value in enumerate(argv) if value == "--mount"]
            self.assertEqual(len(mounts), 12)
            self.assertTrue(any(f"src={(workspace / 'data').resolve()}" in item and item.endswith(",readonly") for item in mounts))
            for path in (config_path, catalog_path):
                self.assertTrue(any(f"dst={path.resolve()}" in item and item.endswith(",readonly") for item in mounts), path)
            for name in binaries:
                self.assertTrue(any(item.endswith(f"dst=/opt/codex/bin/{name},readonly") for item in mounts), name)
            self.assertFalse(any("docker.sock" in item or "auth.json" in item for item in mounts))


if __name__ == "__main__":
    unittest.main()
