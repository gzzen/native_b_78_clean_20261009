# Path: common/integrity/test_resume.py
# Purpose: Verify interruption recovery preserves results, failure outcomes, and policy retry limits without paid calls.
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from common.integrity.resume import ensure_idle, finalize_attempts, resume_plan
from conditions.native import runner as native
from conditions.b import runner as b


def item(number):
    return {"question_id": f"q{number}", "short_id": f"test{number}", "capsule_uuid": f"uuid{number}",
            "question_ids": [f"q{number}"], "question": "Mock question."}


def saved_attempt(root, condition, task, index, accepted, policy=False):
    name = task["question_id"] if condition == "native" else f"{task['short_id']}__{task['capsule_uuid']}"
    workspace = root / f"{index:04d}" / name
    workspace.mkdir(parents=True)
    record = {**task, "workspace": str(workspace), "attempt_index": index,
              "accepted": accepted, "return_code": 86 if policy else 0,
              "runtime_seconds": 1, "controller_status": "passed" if accepted else "failed"}
    if policy:
        record.update(policy_violation={"reason": "mock policy"}, controller_status="interrupted_policy_violation")
    for filename in ("event_timeline.json", "events.jsonl", "stderr.log", "isolation_boundary.json"):
        (workspace / filename).write_text("[]" if filename.endswith(".json") else "")
    if accepted:
        (workspace / ("question_result.json" if condition == "native" else "capsule_result.json")).write_text("{}")
    (workspace / "run_manifest.json").write_text(json.dumps(record))
    return record


class ResumeTests(unittest.TestCase):
    def test_b_resume_manifest_keeps_contract_checks_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            task = item(0)
            with patch.object(b, "MANIFEST", path), \
                 patch.object(b, "load_rows", return_value=[task]), \
                 patch.object(b, "environment_manifest", return_value={"sha256": "env", "values": {"PATH": "/bin"}}), \
                 patch.object(b, "sha256_file", return_value="file"), \
                 patch.object(b, "agent_manifest", return_value={"capture_sha256": "adapter"}):
                original = b.preflight([task], 3600, None)
                original["runner_sha256"] = "prior-version"
                path.write_text(json.dumps(original))
                saved = path.read_bytes()
                self.assertEqual(b.preflight([task], 3600, None, resume=True), original)
                with self.assertRaisesRegex(RuntimeError, "manifest mismatch"):
                    b.preflight([task], 600, None, resume=True)
                with self.assertRaisesRegex(RuntimeError, "manifest mismatch"):
                    b.preflight([task], 3600, None)
                self.assertEqual(path.read_bytes(), saved)

    def test_plan_preserves_terminal_failures_and_pending_policy_retry(self):
        for condition in ("native", "b"):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                tasks = [item(i) for i in range(4)]
                saved_attempt(root, condition, tasks[0], 0, True)
                saved_attempt(root, condition, tasks[1], 0, False)
                saved_attempt(root, condition, tasks[2], 0, False, True)
                saved_attempt(root, condition, tasks[3], 0, False, True)
                saved_attempt(root, condition, tasks[3], 1, False, True)
                plan = resume_plan(root, condition, tasks)
                key = "question_id" if condition == "native" else "capsule_uuid"
                self.assertEqual(len(plan["completed"]), 3)
                self.assertFalse(plan["completed"][tasks[1][key]]["accepted"])
                exhausted = plan["completed"][tasks[3][key]]
                self.assertEqual(exhausted["controller_status"], "failed_closed_repeated_policy_violation")
                self.assertEqual(exhausted["total_agent_calls"], 2)
                self.assertEqual(list(plan["prior"]), [tasks[2][key]])

    def test_both_runners_resume_only_unfinished_units_and_freeze_full_scope(self):
        for condition, module in (("native", native), ("b", b)):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                run_root = root / "runs/test"
                tasks = [item(i) for i in range(4)]
                saved_attempt(run_root, condition, tasks[0], 0, True)
                saved_attempt(run_root, condition, tasks[1], 0, False)
                saved_attempt(run_root, condition, tasks[2], 0, False, True)
                name = tasks[3]["question_id"] if condition == "native" else f"{tasks[3]['short_id']}__{tasks[3]['capsule_uuid']}"
                partial = run_root / "0000" / name
                partial.mkdir(parents=True)
                (partial / "unfinished.txt").write_text("preserve interrupted artifacts")
                original = {path: path.read_bytes() for path in run_root.rglob("*") if path.is_file()}
                manifest, output, report = root / "manifest.json", root / "generation.json", root / "reports/report.md"
                manifest.write_text('{"original":true}')
                observed = []

                def execute(*args):
                    if condition == "native":
                        task, fresh_root, timeout, index = args
                    else:
                        task, rows, fresh_root, timeout, index = args
                    self.assertTrue(fresh_root.parent == run_root)
                    self.assertTrue(fresh_root.name.startswith("resume_"))
                    observed.append((task["question_id"], index))
                    return saved_attempt(fresh_root, condition, task, index, True)

                argv = ["runner.py", "--execute", "--resume", "--workers", "1", "--run-root", str(run_root),
                        "--manifest", str(manifest), "--output", str(output), "--report", str(report)]
                with contextlib.ExitStack() as stack:
                    for name in ("ROOT", "MANIFEST", "MODEL", "OUT", "REASONING_EFFORT", "REPORT"):
                        stack.enter_context(patch.object(module, name, root if name == "ROOT" else getattr(module, name)))
                    substrate = module.substrate if condition == "native" else module.BASE_CONDITION
                    stack.enter_context(patch.object(substrate, "MODEL", substrate.MODEL))
                    stack.enter_context(patch.object(substrate, "REASONING_EFFORT", substrate.REASONING_EFFORT))
                    if condition == "b":
                        stack.enter_context(patch.object(module.BASE_CONDITION.substrate, "MODEL", module.BASE_CONDITION.substrate.MODEL))
                        stack.enter_context(patch.object(module.BASE_CONDITION.substrate, "REASONING_EFFORT", module.BASE_CONDITION.substrate.REASONING_EFFORT))
                        stack.enter_context(patch.object(module, "load_rows", return_value=tasks))
                    stack.enter_context(patch.object(module, "rows" if condition == "native" else "capsules", return_value=tasks))
                    stack.enter_context(patch.object(module, "preflight", return_value={}))
                    stack.enter_context(patch("common.integrity.resume.ensure_idle"))
                    stack.enter_context(patch.object(module, "run_question_attempt" if condition == "native" else "run_capsule_attempt", side_effect=execute))
                    stack.enter_context(patch("sys.argv", argv))
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    module.main()
                    self.assertEqual(observed, [("q2", 1), ("q3", 0)])
                    data = json.loads(output.read_text())
                    records = data["records" if condition == "native" else "episodes"]
                    self.assertEqual(len(records), 4)
                    self.assertEqual(sum(record["accepted"] for record in records), 3)
                    self.assertEqual(records[2]["total_agent_calls"], 2)
                    self.assertEqual(data["freeze"]["status"], "frozen_before_private_scoring")
                    self.assertIn("resume", data)
                    journal = json.loads(Path(data["resume"]["path"]).read_text())
                    self.assertEqual(journal["interrupted_workspaces"], [str(partial)])
                    # Calling --resume again must skip the already frozen run.
                    observed.clear()
                    frozen_bytes = output.read_bytes()
                    module.main()
                    self.assertEqual(observed, [])
                    self.assertEqual(output.read_bytes(), frozen_bytes)
                for path, content in original.items():
                    self.assertEqual(path.read_bytes(), content)
                self.assertEqual(manifest.read_text(), '{"original":true}')

    def test_identity_log_and_duplicate_checks_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = item(0)
            record = saved_attempt(root, "native", task, 0, True)
            path = Path(record["workspace"]) / "run_manifest.json"
            record["workspace"] = str(root / "other")
            path.write_text(json.dumps(record))
            with self.assertRaisesRegex(RuntimeError, "workspace binding"):
                resume_plan(root, "native", [task])
            record["workspace"] = str(path.parent)
            path.write_text(json.dumps(record))
            (path.parent / "stderr.log").unlink()
            with self.assertRaisesRegex(RuntimeError, "capture logs"):
                resume_plan(root, "native", [task])
            (path.parent / "stderr.log").write_text("")
            saved_attempt(root / "resume_later", "native", task, 0, True)
            with self.assertRaisesRegex(RuntimeError, "duplicate terminal"):
                resume_plan(root, "native", [task])

    def test_partial_checkpoint_does_not_read_an_external_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "runs/0000/q0"
            workspace.mkdir(parents=True)
            secret = root / "not-mounted.json"
            secret.write_text('{"confidential":"not a checkpoint"}')
            (workspace / "run_manifest.json").symlink_to(secret)
            with self.assertRaisesRegex(RuntimeError, "unsafe output symlink"):
                resume_plan(root / "runs", "native", [item(0)])

    def test_idle_check_rejects_live_controller_or_orphan_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch("scripts.check_experiment_progress.runner_processes", return_value=({str(root): [987654]}, None)):
                with self.assertRaisesRegex(RuntimeError, "controller"):
                    ensure_idle(root, root)
            with patch("scripts.check_experiment_progress.runner_processes", return_value=({}, None)), \
                 patch("common.integrity.resume.subprocess.run", side_effect=[
                     subprocess.CompletedProcess([], 0, "abcdef\n", ""),
                     subprocess.CompletedProcess([], 0, json.dumps([{"Id": "abcdef", "Mounts": [{"Source": str(root / "0000/q")}]}]), "")]):
                with self.assertRaisesRegex(RuntimeError, "container"):
                    ensure_idle(root, root)

    def test_resumed_policy_retry_does_not_get_an_extra_attempt(self):
        for module, function, condition in ((native, native.run_question, "native"), (b, b.run_capsule, "b")):
            with self.subTest(condition=condition), tempfile.TemporaryDirectory() as temporary:
                root, task = Path(temporary), item(0)
                first = saved_attempt(root / "original", condition, task, 0, False, True)
                second = saved_attempt(root / "new", condition, task, 1, False, True)
                with patch.object(module, "run_question_attempt" if condition == "native" else "run_capsule_attempt", return_value=second) as call:
                    args = (task, root / "new", 10) if condition == "native" else (task, [task], root / "new", 10)
                    final = function(*args, prior_attempts=[first])
                call.assert_called_once()
                self.assertEqual(final["controller_status"], "failed_closed_repeated_policy_violation")
                self.assertEqual(final["total_agent_calls"], 2)


if __name__ == "__main__":
    unittest.main()
