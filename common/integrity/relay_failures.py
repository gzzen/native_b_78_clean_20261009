# Path: common/integrity/relay_failures.py
# Purpose: Classify attempts that failed because of the model relay, not the model, for fresh re-attempts.
from __future__ import annotations

import re
from typing import Any

RELAY_CAPACITY = "relay_capacity"
RELAY_STREAM_DROP = "relay_stream_drop"
RELAY_FAILURE_CLASSES = (RELAY_CAPACITY, RELAY_STREAM_DROP)
MAX_RELAY_RETRIES = 3

# Codex's own wording ("Selected model is at capacity" is its ServerOverloaded
# error) plus relay "busy" refusals and retry exhaustion on capacity statuses.
_CAPACITY = re.compile(
    r"at capacity|server (?:is )?busy|overloaded|too many requests|"
    r"last status:? *(?:429|503|529)\b|unexpected status:? *(?:429|503|529)\b", re.I)
# Stream disconnects after Codex's own reconnects, relay 5xx/524 and gateway refusals.
_STREAM_DROP = re.compile(
    r"stream disconnected before completion|stream closed before response\.completed|"
    r"idle timeout waiting for SSE|error sending request|"
    r"last status:? *5\d\d\b|unexpected status:? *5\d\d\b|"
    r"gateway_upstream_error|gateway_relay_unavailable", re.I)


def _raw(record: dict) -> Any:
    event = record.get("event", {})
    return event.get("codex_event", event) if isinstance(event, dict) else event


def _message(raw: dict) -> str:
    error = raw.get("error") if isinstance(raw.get("error"), dict) else raw
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else ""


def classify_relay_failure(return_code: int | None, records: list[dict]) -> str | None:
    """Return the relay failure class of a failed turn, or None for any other outcome.

    Only the final turn.failed decides (or the last error when Codex exited
    without one). A completed turn, a timeout or a model-side failure is never a
    relay failure; "Reconnecting..." errors that Codex recovered from are ignored.
    """
    if return_code is None or return_code == 0:
        return None
    raws = [raw for raw in map(_raw, records) if isinstance(raw, dict)]
    if any(raw.get("type") == "turn.completed" for raw in raws):
        return None
    decisive = ([raw for raw in raws if raw.get("type") == "turn.failed"]
                or [raw for raw in raws if raw.get("type") == "error"])
    if not decisive:
        return None
    message = _message(decisive[-1])
    if _CAPACITY.search(message):
        return RELAY_CAPACITY
    if _STREAM_DROP.search(message):
        return RELAY_STREAM_DROP
    return None


__all__ = ["MAX_RELAY_RETRIES", "RELAY_CAPACITY", "RELAY_FAILURE_CLASSES", "RELAY_STREAM_DROP",
           "classify_relay_failure"]
