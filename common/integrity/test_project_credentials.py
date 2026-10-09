# Path: common/integrity/test_project_credentials.py
# Purpose: Verify project credential loading without shell execution or environment leakage.
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.integrity.attempt_gateway import GatewayConfig


class ProjectCredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env_file = Path(self.directory.name) / ".env"
        self.env_patch = patch("common.integrity.attempt_gateway.PROJECT_ENV", self.env_file)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def config(self):
        return GatewayConfig.from_host("temporary-token", "deepseek-flash")

    def test_project_key_only_enters_gateway_not_environment(self):
        self.env_file.write_text("DEEPSEEK_API_KEY=project-only\nEVALUATOR_API_SECRET=ignored\n")
        config = self.config()
        self.assertEqual(config.credential, "project-only")
        self.assertEqual(config.upstream, "https://api.deepseek.com/v1")
        self.assertNotIn("DEEPSEEK_API_KEY", os.environ)
        self.assertNotIn("EVALUATOR_API_SECRET", os.environ)

    def test_exported_key_takes_precedence(self):
        self.env_file.write_text("DEEPSEEK_API_KEY=project-key\n")
        os.environ["DEEPSEEK_API_KEY"] = "shell-key"
        self.assertEqual(self.config().credential, "shell-key")

    def test_quoted_export_and_comments(self):
        self.env_file.write_text("# Comment\nexport DEEPSEEK_API_KEY = 'key#literal' # Comment\n")
        self.assertEqual(self.config().credential, "key#literal")

    def test_missing_and_empty_keys_fail(self):
        with self.assertRaises(RuntimeError):
            self.config()
        for text in ("DEEPSEEK_API_KEY=\n", 'DEEPSEEK_API_KEY=""\n', "OTHER=value\n"):
            with self.subTest(text=text):
                self.env_file.write_text(text)
                with self.assertRaises(RuntimeError):
                    self.config()

    def test_shell_substitution_is_never_executed(self):
        marker = Path(self.directory.name) / "executed"
        literal = f"$(touch {marker})"
        self.env_file.write_text(f"DEEPSEEK_API_KEY='{literal}'\n")
        self.assertEqual(self.config().credential, literal)
        self.assertFalse(marker.exists())

    def test_invalid_values_do_not_leak_credentials(self):
        for value in ("'secret-unclosed", "secret-one secret-two"):
            with self.subTest(value=value):
                self.env_file.write_text(f"DEEPSEEK_API_KEY={value}\n")
                with self.assertRaises(ValueError) as error:
                    self.config()
                self.assertNotIn("secret", str(error.exception))

    def test_duplicate_keys_fail(self):
        self.env_file.write_text("DEEPSEEK_API_KEY=first\nDEEPSEEK_API_KEY=second\n")
        with self.assertRaises(ValueError):
            self.config()

    def relay_config(self):
        return GatewayConfig.from_host("temporary-token", "gpt-5.6-sol", "openai-relay")

    def test_relay_uses_its_own_key_and_responses_route(self):
        # A general OpenAI key must never be sent to a third-party relay.
        os.environ["OPENAI_API_KEY"] = "official-openai-key"
        self.env_file.write_text("DEEPSEEK_API_KEY=deepseek-only\n")
        with self.assertRaises(RuntimeError):
            self.relay_config()
        self.env_file.write_text("DEEPSEEK_API_KEY=deepseek-only\nOPENAI_RELAY_API_KEY=relay-only\n")
        config = self.relay_config()
        self.assertEqual(config.credential, "relay-only")
        self.assertEqual(config.upstream, "https://xfastapi.ai")
        self.assertEqual(config.model_path, "/responses")
        self.assertEqual(config.model, "gpt-5.6-sol")
        self.assertNotIn("OPENAI_RELAY_API_KEY", os.environ)
        self.assertEqual(self.config().model_path, "/chat/completions")

    def test_relay_base_url_must_be_plain_https(self):
        self.env_file.write_text("OPENAI_RELAY_API_KEY=relay-only\nOPENAI_RELAY_BASE_URL=https://relay.example/v1/\n")
        self.assertEqual(self.relay_config().upstream, "https://relay.example/v1")
        for base in ("http://relay.example", "https://user@relay.example", "https://relay.example?x=1"):
            with self.subTest(base=base):
                self.env_file.write_text(f"OPENAI_RELAY_API_KEY=relay-only\nOPENAI_RELAY_BASE_URL={base}\n")
                with self.assertRaises(ValueError):
                    self.relay_config()


if __name__ == "__main__":
    unittest.main()
