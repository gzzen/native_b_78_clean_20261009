# Path: common/integrity/test_native_manifest.py
# Purpose: Verify stale manifest diagnostics and preservation of existing experiment artifacts.
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conditions.native import runner


class NativeManifestTests(unittest.TestCase):
    def test_resume_records_code_change_but_requires_the_same_experiment_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            items = [{"question_id": "q1", "short_id": "test", "capsule_uuid": "uuid"}]
            with patch.object(runner, "MANIFEST", path), \
                 patch.object(runner, "environment_manifest", return_value={"sha256": "env", "values": {"PATH": "/bin"}}), \
                 patch.object(runner, "prompt_sha256s", return_value={"q1": "prompt"}), \
                 patch.object(runner, "sha256_file", return_value="file"), \
                 patch.object(runner, "filesystem_boundary", return_value={}), \
                 patch.object(runner, "agent_manifest", return_value={"capture_sha256": "adapter"}):
                original = runner.preflight(items, 3600, 2)
                original["runner_sha256"] = "prior-runner-version"
                path.write_text(json.dumps(original))
                saved = path.read_bytes()
                self.assertEqual(runner.preflight(items, 3600, 2, resume=True), original)
                self.assertEqual(path.read_bytes(), saved)
                with self.assertRaisesRegex(RuntimeError, "runner_sha256"):
                    runner.preflight(items, 3600, 2)
                with self.assertRaisesRegex(RuntimeError, "timeout_seconds"):
                    runner.preflight(items, 600, 2, resume=True)
                self.assertEqual(path.read_bytes(), saved)

    def test_changed_code_and_timeout_are_named_without_overwriting_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            environment = {"sha256": "env-hash", "values": {"PATH": "/runtime/bin"}}
            items = [{"question_id": "q1", "short_id": "bix-test", "capsule_uuid": "capsule-test"}]
            with patch.object(runner, "MANIFEST", manifest_path), \
                    patch.object(runner, "environment_manifest", return_value=environment), \
                    patch.object(runner, "prompt_sha256s", return_value={"q1": "prompt-hash"}), \
                    patch.object(runner, "sha256_file", return_value="file-hash"), \
                    patch.object(runner, "filesystem_boundary", return_value={}), \
                    patch.object(runner, "agent_manifest", return_value={"capture_sha256": "original"}) as agent:
                runner.preflight(items, 3600, 1)
                original_bytes = manifest_path.read_bytes()
                self.assertEqual(runner.preflight(items, 3600, 1), json.loads(original_bytes))
                agent.return_value = {"capture_sha256": "updated"}
                with self.assertRaises(RuntimeError) as error:
                    runner.preflight(items, 600, 1)
                message = str(error.exception)
                self.assertIn(str(manifest_path), message)
                self.assertIn("agent, timeout_seconds", message)
                for argument in ("--manifest", "--run-root", "--output", "--report"):
                    self.assertIn(argument, message)
                self.assertEqual(manifest_path.read_bytes(), original_bytes)


if __name__ == "__main__":
    unittest.main()
