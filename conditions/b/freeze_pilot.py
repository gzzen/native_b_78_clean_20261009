#!/usr/bin/env python3
# Path: conditions/b/freeze_pilot.py
# Purpose: Freeze the completed B grounding pilot before any private post-hoc scoring.
from __future__ import annotations
import hashlib,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'analysis/b_grounding_pilot_seed_20260910_generation.json'
def sh(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 x=json.loads(OUT.read_text());files=[]
 for r in x['episodes']:
  w=Path(r['workspace'])
  for f in (w/'capsule_result.json',w/'event_timeline.json',w/'events.jsonl',w/'stderr.log',w/'run_manifest.json',w/'analysis/grounding.json',w/'analysis/path_audit.json'):
   files.append({'path':str(f.relative_to(ROOT)),'sha256':sh(f) if f.is_file() else None})
 x['freeze']={'status':'frozen_before_private_scoring','artifacts':files};OUT.write_text(json.dumps(x,ensure_ascii=False,indent=2)+'\n');print(len(files))
if __name__=='__main__':main()
