# Path: common/integrity/progress.py
# Purpose: Flush content-free controller progress to stderr without changing experiment artifacts.
from __future__ import annotations

import re
import sys
import threading
from datetime import datetime, timezone

_OUTPUT_LOCK = threading.Lock()
_TOOLS = {"shell", "bash", "read", "write", "edit", "patch", "apply_patch", "glob", "grep", "webfetch"}


def progress_log(label: str, message: str) -> None:
    label = re.sub(r"[^a-zA-Z0-9._/:-]", "_", label)[:160]
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    with _OUTPUT_LOCK:
        try:
            print(f"[{timestamp}] [{label}] {message}", file=sys.stderr, flush=True)
        except OSError:
            pass  # A closed terminal/log pipe must not terminate generation.


class AttemptProgress:
    def __init__(self, label: str, interval: float = 30):
        self.label = label
        self.interval = interval
        self.next_heartbeat = interval
        self.events = 0
        self.tools = 0
        self.assistant_seen = False

    def __call__(self, elapsed: float, event: dict | None) -> None:
        if event is None:
            if elapsed >= self.next_heartbeat:
                progress_log(self.label, f"running elapsed={elapsed:.0f}s events={self.events} tools={self.tools}")
                self.next_heartbeat = elapsed + self.interval
            return
        self.events += 1
        item = event.get("item", {})
        if not isinstance(item, dict):
            item = {}
        tool = item.get("opencode_tool")
        if tool is not None:
            self.tools += 1
            name = tool if isinstance(tool, str) and tool in _TOOLS else "other"
            status = item.get("status")
            status = status if isinstance(status, str) and status in {"pending", "running", "completed", "error"} else "observed"
            progress_log(self.label, f"tool={name} status={status} elapsed={elapsed:.0f}s")
        elif event.get("type") == "text" and not self.assistant_seen:
            self.assistant_seen = True
            progress_log(self.label, f"assistant output received elapsed={elapsed:.0f}s")
        elif event.get("type") == "error":
            progress_log(self.label, f"agent error received elapsed={elapsed:.0f}s; inspect saved logs")


class RunProgress:
    def __init__(self, label: str, total: int, workers: int):
        self.label = label
        self.total = total
        self.finished = 0
        self.passed = 0
        self.lock = threading.Lock()
        progress_log(label, f"starting total={total} workers={min(workers, total)}")

    def complete(self, record: dict) -> dict:
        with self.lock:
            self.finished += 1
            self.passed += bool(record.get("accepted"))
            progress_log(self.label, f"completed={self.finished}/{self.total} passed={self.passed} failed={self.finished - self.passed}")
        return record
