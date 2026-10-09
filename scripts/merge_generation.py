#!/usr/bin/env python3
"""Merge frozen main/retry generations before private post-hoc scoring."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("freeze", {}).get("status") != "frozen_before_private_scoring":
        raise RuntimeError(f"not frozen: {path}")
    return data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--condition", choices=("native", "b"), required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("generations", nargs="+", type=Path)
    args = ap.parse_args()
    data = [load(path) for path in args.generations]
    key = "question_id" if args.condition == "native" else "capsule_uuid"
    field = "records" if args.condition == "native" else "episodes"
    selected: dict[str, tuple[dict, str]] = {}
    for path, generation in zip(args.generations, data, strict=True):
        for record in generation.get(field, []):
            rid = record[key]
            old = selected.get(rid)
            # A successful retry replaces a failed main attempt. For two
            # successful records, preserve the first (main) result.
            if old is None or (record.get("accepted") and not old[0].get("accepted")):
                selected[rid] = (record, str(path))
    records = [item[0] for item in selected.values()]
    expected = 78 if args.condition == "native" else 19
    if len(records) != expected:
        raise RuntimeError(f"merged {args.condition} seed {args.seed}: {len(records)} records, expected {expected}")
    if args.condition == "native":
        if len({r["question_id"] for r in records}) != 78:
            raise RuntimeError("duplicate/missing native question IDs")
    else:
        if len({r["capsule_uuid"] for r in records}) != 19:
            raise RuntimeError("duplicate/missing B capsule UUIDs")
    base = data[0]
    summary = dict(base.get("generation_summary", {}))
    summary["merged_source_generations"] = [str(p) for p in args.generations]
    summary["merged_records"] = len(records)
    summary["merged_accepted_records"] = sum(bool(r.get("accepted")) for r in records)
    payload = {
        "manifest": base["manifest"],
        field: records,
        "freeze": {
            "status": "frozen_before_private_scoring",
            "merge_status": "post_generation_transport_recovery_merge",
            "source_generations": [str(p.resolve()) for p in args.generations],
        },
        "generation_summary": summary,
        "scoring": {"status": "not_started"},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"condition": args.condition, "seed": args.seed, "records": len(records),
                      "accepted": sum(bool(r.get("accepted")) for r in records),
                      "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
