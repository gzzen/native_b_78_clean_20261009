from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from common.integrity.isolation_audit import audit_command


class IsolationAuditNegativeGlobTest(unittest.TestCase):
    def test_literal_grep_exclusion_is_not_a_file_access(self) -> None:
        commands = [
            "find . -type f | grep -v '/tmp/' | grep -v '/analysis/' | head -50",
            'grep -v "/var/tmp/" files.txt',
            "grep -Ev -- '../private/' files.txt",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            for command in commands:
                with self.subTest(command=command):
                    self.assertEqual(audit_command(command, Path(temporary)), [])

    def test_grep_exclusion_does_not_hide_real_file_access(self) -> None:
        commands = [
            "grep -v '/tmp/' /tmp/input",
            "grep -vf /tmp/patterns files.txt",
            "grep -v -f /tmp/patterns files.txt",
            "grep -v '/tmp/' > /tmp/output",
            'grep -v "$(cat /tmp/input)" files.txt',
            'grep -v "`cat /tmp/input`" files.txt',
            "grep -v '/tmp/' files.txt; cat /tmp/input",
            "grep -v '/tmp/' ../private/input",
            "python -c \"print(\\\"grep -v '/tmp/input'\\\")\"",
            "grep -v '/tmp/' files.txt -f /tmp/patterns",
            "grep -v -- -- '/tmp/input'",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            for command in commands:
                with self.subTest(command=command):
                    findings = audit_command(command, Path(temporary))
                    self.assertTrue(any(item['actual_workspace_escape'] for item in findings))

    def test_direct_interpreter_absolute_history_access_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            findings = audit_command("python /mnt/experiment/solver/runs/old/solve.py", Path(temporary))
        self.assertEqual(findings[0]["verdict"], "fatal_workspace_escape")

    def test_active_data_symlink_absolute_target_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            (workspace / "data").symlink_to("/mnt/frozen-public-capsule", target_is_directory=True)
            findings = audit_command("cat /mnt/frozen-public-capsule/input.csv", workspace)
        self.assertEqual(findings, [])

    def test_workspace_local_tmp_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "0000" / "capsule-a"
            private_tmp = workspace / "tmp"
            private_tmp.mkdir(parents=True)
            findings = audit_command(f"cd {private_tmp}", workspace)
        self.assertEqual(findings, [])

    def test_shared_tmp_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            findings = audit_command("cd /tmp", Path(temporary))
        self.assertTrue(findings)
        self.assertEqual(findings[0]["verdict"], "fatal_shared_temp_access")

    def test_rg_parent_exclusion_glob_is_not_an_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            findings = audit_command(
                "rg --files -g 'data/**' -g '!../*' -g '!../../*'",
                Path(temporary),
            )
        self.assertEqual(findings, [])

    def test_quote_spliced_rg_parent_exclusion_is_not_an_access(self) -> None:
        command = (
            '/usr/bin/bash -c "pwd && rg --files -g \'environment_contract.json\' '
            '-g \'"\'!../*\'"\' -g \'"\'!**/../*\'"\' | sed -n \'1,120p\'"'
        )
        with tempfile.TemporaryDirectory() as temporary:
            findings = audit_command(command, Path(temporary))
        self.assertEqual(findings, [])

    def test_real_parent_path_remains_an_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            findings = audit_command("cat ../private/answers.json", Path(temporary))
        self.assertTrue(findings)
        self.assertTrue(any(item["actual_workspace_escape"] for item in findings))


if __name__ == "__main__":
    unittest.main()
