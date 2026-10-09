# Path: common/integrity/test_progress.py
# Purpose: Verify live, flushed, content-free progress and concurrent completion counts.
from __future__ import annotations

import concurrent.futures
import io
import os
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from common.integrity.opencode_capture import capture_opencode
from common.integrity.progress import AttemptProgress, RunProgress, progress_log


class TimestampedStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = []

    def flush(self):
        self.flushes.append((time.monotonic(), self.getvalue()))
        super().flush()


class ProgressTests(unittest.TestCase):
    def test_tool_and_heartbeat_are_flushed_before_agent_finishes(self):
        event = {"type": "tool_use", "part": {"tool": "shell", "state": {
            "status": "completed", "input": {"command": "echo confidential-command"},
            "output": "confidential-result"}}}
        program = f"import json,time; print(json.dumps({event!r}),flush=True); time.sleep(0.6)"
        stderr, stdout = TimestampedStream(), io.StringIO()
        with redirect_stderr(stderr), redirect_stdout(stdout), \
                patch("common.integrity.opencode_capture.AttemptProgress",
                      side_effect=lambda label: AttemptProgress(label, interval=0.05)):
            code, records, _, _ = capture_opencode([sys.executable, "-c", program], dict(os.environ),
                                                  5, progress_label="Native/q1/attempt1")
        self.assertEqual(code, 0)
        self.assertEqual(len(records), 1)
        self.assertEqual(stdout.getvalue(), "")
        output = stderr.getvalue()
        self.assertIn("tool=shell status=completed", output)
        self.assertIn("running elapsed=", output)
        self.assertIn("agent finished exit=0", output)
        self.assertNotIn("confidential", output)
        tool_time = next(t for t, text in stderr.flushes if "tool=shell" in text)
        finished_time = next(t for t, text in stderr.flushes if "agent finished" in text)
        self.assertGreater(finished_time - tool_time, 0.3)

    def test_unknown_tools_and_errors_do_not_print_payloads(self):
        stream = io.StringIO()
        progress = AttemptProgress("Native/q1")
        with redirect_stderr(stream):
            progress(1, {"type": "item.completed", "item": {
                "opencode_tool": "confidential-tool", "status": "confidential-status",
                "command": "confidential-command"}})
            progress(2, {"type": "error", "error": "confidential-error"})
            progress(3, {"type": "text", "part": {"text": "confidential-answer"}})
        self.assertNotIn("confidential", stream.getvalue())
        self.assertIn("tool=other status=observed", stream.getvalue())

    def test_concurrent_completion_counts_and_record_order(self):
        records = [{"accepted": index % 2 == 0} for index in range(12)]
        stream = io.StringIO()
        with redirect_stderr(stream):
            progress = RunProgress("B", len(records), 3)
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
                returned = list(pool.map(progress.complete, records))
        self.assertEqual(returned, records)
        self.assertEqual(progress.finished, 12)
        self.assertIn("completed=12/12 passed=6 failed=6", stream.getvalue())

    def test_closed_log_pipe_does_not_stop_generation(self):
        class ClosedStream:
            def write(self, text):
                raise BrokenPipeError("closed")

        with redirect_stderr(ClosedStream()):
            progress_log("Native/q1", "starting")


if __name__ == "__main__":
    unittest.main()
