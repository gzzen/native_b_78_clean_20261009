# Path: common/integrity/isolation_audit.py
# Purpose: Answer-blind filesystem isolation audit with conservative path and literal-pattern handling.
"""Canonical filesystem-path isolation audit for the standalone bundle."""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path
from typing import Any, Iterable

TAXONOMY = {"allowed_internal_relative_path", "allowed_internal_symlink", "fatal_workspace_escape",
            "fatal_external_absolute_discovery", "fatal_private_reference", "fatal_shared_temp_access",
            "ambiguous_path_resolution"}
# A private filename by itself is not a breach: solvers commonly write a
# workspace-local `*_answers.json`.  Private access is a resolved-path fact.
PRIVATE_PATH_MARKERS = ("secure_bixbench",)
ABSOLUTE_PATH = re.compile(r"(?<![\w.-])/(?:[^\s'\";|&()]+)")
SYSTEM_SINKS = {Path("/dev/null"), Path("/dev/stdout"), Path("/dev/stderr")}
SHARED_TEMP_ROOTS = (Path("/tmp"), Path("/var/tmp"))


def is_path_within(path: Path, allowed_root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(allowed_root.resolve())
        return True
    except ValueError:
        return False


def is_within_any_allowed_root(path: Path, allowed_roots: Iterable[Path]) -> bool:
    return any(is_path_within(path, root) for root in allowed_roots)


def _record(raw: str, base: Path, roots: list[Path], kind: str, *, symlink: bool = False) -> dict[str, Any]:
    candidate = Path(raw)
    resolved = candidate.resolve(strict=False) if candidate.is_absolute() else (base / candidate).resolve(strict=False)
    matched_root = next((root for root in roots if is_path_within(resolved, root)), None)
    inside = matched_root is not None
    return {"raw_path": raw, "command_cwd": str(base), "resolved_path": str(resolved),
            "allowed_root": str(matched_root.resolve()) if matched_root else None,
            "contains_parent_syntax": ".." in candidate.parts, "actual_workspace_escape": not inside,
            "verdict": ("allowed_internal_symlink" if symlink else "allowed_internal_relative_path") if inside else "fatal_workspace_escape"}


def _compound_segments(command: str) -> list[str]:
    """Extract a bash -lc payload, then conservatively split top-level compounds."""
    try:
        outer = shlex.split(command)
    except ValueError:
        return [command]
    payload = command
    for index, token in enumerate(outer[:-1]):
        if Path(token).name in {"bash", "sh"} and outer[index + 1] == "-lc":
            payload = outer[index + 2] if len(outer) > index + 2 else command
            break
    # Paths here are simple shell arguments; retain unparseable segments so they
    # fail closed only if they contain a path-risk signal.
    return [part.strip() for part in re.split(r"(?:&&|;|\n)", payload) if part.strip()]


def _strip_terminal_punctuation(value: str) -> str:
    """Remove prose punctuation, never meaningful path components."""
    return value.rstrip(".,:;!?")


def _environment_roots() -> tuple[Path, ...]:
    """Roots containing interpreter/package implementation, not experiment data."""
    return tuple(Path(value).resolve() for value in {sys.prefix, sys.base_prefix, "/usr", "/bin", "/lib", "/lib64"})


def _is_environment_path(path: Path) -> bool:
    return any(is_path_within(path, root) for root in _environment_roots())


def _is_system_sink(path: Path) -> bool:
    return path in SYSTEM_SINKS


def _is_shared_temp_path(path: Path) -> bool:
    return any(path == root or is_path_within(path, root) for root in SHARED_TEMP_ROOTS)


def _absolute_paths(command: str) -> list[Path]:
    return [Path(_strip_terminal_punctuation(raw)) for raw in ABSOLUTE_PATH.findall(command)]


def _mask_literal_grep_exclusions(command: str) -> str:
    """Hide static grep -v text patterns, retaining file operands and expansions.

    Recognize only a direct shell command and a quoted literal first pattern.
    Unknown flags/forms remain subject to the conservative path audit.
    """
    words = list(re.finditer(
        r"(?:'[^']*'|\"(?:\\.|[^\"\\])*\"|\\[^\n]|[^\s'\";&|<>()])+|[;&|<>()]+|\n",
        command,
    ))
    spans: list[tuple[int, int]] = []
    for index, word in enumerate(words):
        if index and words[index - 1].group() not in {"|", "||", ";", "&&", "&", "\n"}:
            continue
        if word.group() not in {"grep", "/usr/bin/grep", "/bin/grep"}:
            continue
        inverted = False
        options_ended = False
        for operand in words[index + 1:]:
            raw = operand.group()
            if not options_ended and re.fullmatch(r"-[vEFGinHhcsqx]+", raw):
                inverted = inverted or "v" in raw
                continue
            if not options_ended and raw == "--":
                options_ended = True
                continue
            # No command substitution, variable expansion, or ambiguous quote
            # splicing is exempted. Pattern files (-f) are never masked.
            if (inverted and len(raw) >= 2 and raw[0] in {"'", '"'}
                    and raw[-1] == raw[0] and not any(c in raw for c in "$`\\")):
                try:
                    literal = shlex.split(raw)
                except ValueError:
                    break
                if len(literal) == 1:
                    spans.append((operand.start(), operand.end()))
            break
    for start, end in reversed(spans):
        command = command[:start] + "'excluded-text-pattern'" + command[end:]
    return command


def audit_command(command: str, workspace: Path) -> list[dict[str, Any]]:
    """Audit parseable filesystem paths; unknown parent syntax fails closed."""
    root = workspace.resolve(); roots = [root, root / "data", root / "analysis", root / "results"]
    lowered = command.lower()
    path_command = _mask_literal_grep_exclusions(command)
    absolute = _absolute_paths(path_command)
    if any(marker in lowered for marker in PRIVATE_PATH_MARKERS):
        return [{"verdict": "fatal_private_reference", "actual_workspace_escape": True, "command": command}]
    # `answers.json` becomes private only if it resolves outside this workspace.
    for path in absolute:
        if path.name == "answers.json" and not is_path_within(path, root):
            return [{"verdict": "fatal_private_reference", "actual_workspace_escape": True, "command": command}]
        # A fixed path in a host-wide temporary directory can expose artifacts
        # left by another capsule/run.  Temporary files must live below the
        # current workspace (for example workspace/tmp/).
        if _is_shared_temp_path(path) and not is_path_within(path, root):
            return [{"verdict": "fatal_shared_temp_access", "actual_workspace_escape": True, "command": command,
                     "raw_path": str(path)}]
        # Direct interpreter reads are just as unsafe as `cat`/`find` discovery.
        # Permit installed environment code and the active capsule's data link,
        # but reject other host experiment/home trees regardless of executable.
        if (path.parts[1:2] in [("mnt",), ("home",), ("root",)]
                and not is_within_any_allowed_root(path, roots)
                and not _is_environment_path(path)):
            return [{"verdict": "fatal_workspace_escape", "actual_workspace_escape": True,
                     "command": command, "raw_path": str(path)}]
    normalized = path_command.replace(str(root), "<workspace>")
    if re.search(r"\b(?:find|rg|grep|sed|cat|ls)\s+/(?:mnt|home|root)", normalized):
        # Inspecting the active interpreter's installed package source is an
        # environment probe.  Discovering other filesystem trees is not.
        if not absolute or not all(
            _is_environment_path(path) or _is_system_sink(path)
            or is_within_any_allowed_root(path, roots) for path in absolute
        ):
            return [{"verdict": "fatal_external_absolute_discovery", "actual_workspace_escape": True, "command": command}]
    evidence: list[dict[str, Any]] = []
    # `ln` stores a relative target relative to the link parent, not shell cwd.
    # Scan every compound segment, including `bash -lc '... && ln ...'` payloads.
    for line in _compound_segments(path_command):
        try:
            tokens = shlex.split(line)
        except ValueError:
            # Codex event JSON can preserve a shell's quote-splicing form for
            # rg negative globs (for example `-g '"'!**/../*'"'`).  Those
            # patterns explicitly exclude parent paths.  Remove only
            # whitespace-free `!…../…` tokens from an `rg --files -g` command
            # before applying the conservative unparseable-parent check.
            parent_check = line
            if re.search(r"\brg\s+--files\b", line) and " -g " in line:
                parent_check = re.sub(r"\S*!\S*\.\.\S*", "", line)
            if ".." in parent_check:
                evidence.append({"verdict": "ambiguous_path_resolution", "actual_workspace_escape": True, "raw_path": line})
            continue
        ln_index = next((index for index, token in enumerate(tokens) if Path(token).name == "ln"), None)
        if ln_index is not None:
            args = [token for token in tokens[ln_index + 1:] if not token.startswith("-")]
            if len(args) >= 2:
                target, link = args[-2], args[-1]
                # Do not resolve the link itself before locating its parent: an
                # existing symlink would otherwise move that parent to target.
                link_path = (root / link).absolute() if not Path(link).is_absolute() else Path(link).absolute()
                try:
                    link_path.relative_to(root)
                    link_inside = True
                except ValueError:
                    link_inside = False
                if not link_inside:
                    evidence.append({"raw_path": link, "verdict": "fatal_workspace_escape", "actual_workspace_escape": True})
                else:
                    # Prefer executed filesystem evidence. `resolve()` on the
                    # link follows its stored relative target from link.parent.
                    if link_path.is_symlink() or link_path.exists():
                        observed = link_path.resolve(strict=False)
                        matched = next((allowed for allowed in roots if is_path_within(observed, allowed)), None)
                        evidence.append({"raw_path": target, "link_path": str(link_path), "command_cwd": str(root),
                                         "resolved_path": str(observed), "allowed_root": str(matched.resolve()) if matched else None,
                                         "contains_parent_syntax": ".." in Path(target).parts,
                                         "actual_workspace_escape": matched is None,
                                         "evidence_source": "executed_symlink_realpath",
                                         "verdict": "allowed_internal_symlink" if matched else "fatal_workspace_escape"})
                    else:
                        item = _record(target, link_path.parent, roots, "symlink", symlink=True)
                        item["link_path"] = str(link_path); item["evidence_source"] = "declared_symlink_target"
                        evidence.append(item)
                continue
        for token_index, token in enumerate(tokens):
            # `rg --files -g '!../*'` is a deny pattern, not a filesystem
            # access.  Keep real parent-path arguments fail-closed while
            # allowing an explicit negative glob attached to rg's glob flag.
            previous = tokens[token_index - 1] if token_index else ""
            if previous in {"-g", "--glob"} and token.startswith("!"):
                continue
            if ".." in Path(token).parts:
                evidence.append(_record(token, root, roots, "relative"))
    return evidence


def audit_events_v121(records: list[dict[str, Any]], workspace: Path) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for number, record in enumerate(records, 1):
        item = record.get("event", {}).get("item", {}) if isinstance(record.get("event"), dict) else {}
        command = str(item.get("command", ""))
        for finding in audit_command(command, workspace):
            finding["event_number"] = number; evidence.append(finding)
    fatal = [item for item in evidence if item["verdict"].startswith("fatal_") or item["verdict"] == "ambiguous_path_resolution"]
    if fatal:
        raise ValueError("v1.2.1 path audit failed: " + "; ".join(f"event {x['event_number']}: {x['verdict']}" for x in fatal[:5]))
    return evidence
