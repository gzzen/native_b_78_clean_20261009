from __future__ import annotations

import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from common.benchmark.a84_contract import (
    WORKSPACE_TEMP_ENV_KEYS,
    controlled_env,
    workspace_temp_environment,
)


class WorkspaceTempTest(unittest.TestCase):
    def test_two_capsules_receive_disjoint_temp_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_workspace = root / "0000" / "capsule-a"
            second_workspace = root / "0000" / "capsule-b"
            first_workspace.mkdir(parents=True)
            second_workspace.mkdir(parents=True)

            first = workspace_temp_environment(first_workspace)
            second = workspace_temp_environment(second_workspace)

            self.assertNotEqual(first["TMPDIR"], second["TMPDIR"])
            self.assertEqual(Path(first["TMPDIR"]), first_workspace / "tmp")
            self.assertEqual(Path(second["TMPDIR"]), second_workspace / "tmp")
            self.assertEqual(stat.S_IMODE(Path(first["TMPDIR"]).stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(Path(second["TMPDIR"]).stat().st_mode), 0o700)
            for key in WORKSPACE_TEMP_ENV_KEYS:
                self.assertTrue(Path(first[key]).is_relative_to(first_workspace))
                self.assertTrue(Path(second[key]).is_relative_to(second_workspace))

    def test_controlled_environment_exports_workspace_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "capsule"
            workspace.mkdir()
            environment = controlled_env(workspace)
            expected = workspace_temp_environment(workspace, create=False)
            self.assertEqual(
                {key: environment[key] for key in WORKSPACE_TEMP_ENV_KEYS},
                expected,
            )

    def test_python_tempfile_uses_the_owning_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspaces = [root / "0000" / name for name in ("capsule-a", "capsule-b")]
            generated = []
            for workspace in workspaces:
                workspace.mkdir(parents=True)
                path = subprocess.check_output(
                    [sys.executable, "-c", "import tempfile; print(tempfile.mkdtemp())"],
                    env=controlled_env(workspace),
                    text=True,
                ).strip()
                generated.append(Path(path))
            self.assertTrue(generated[0].is_relative_to(workspaces[0] / "tmp"))
            self.assertTrue(generated[1].is_relative_to(workspaces[1] / "tmp"))
            self.assertFalse(generated[0].is_relative_to(workspaces[1]))
            self.assertFalse(generated[1].is_relative_to(workspaces[0]))


if __name__ == "__main__":
    unittest.main()
