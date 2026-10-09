#!/usr/bin/env python3
# Path: conditions/a/runner.py
# Purpose: Byte-preserving migration of the 84-version capsule Native runner into formal standalone A.
"""Condition A execution substrate imported by standalone Condition B."""
from __future__ import annotations
import argparse, concurrent.futures, datetime as dt, hashlib, json, sys, time
from collections import defaultdict
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import common.benchmark.a84_contract as substrate
from common.benchmark.a84_contract import (MODEL,NATIVE_PROMPT,NATIVE_SCHEMA,QUESTIONS,REASONING_EFFORT,TIMEOUT_SECONDS,candidate_policy,controlled_env,environment_manifest,load_rows,public_question,sha256_file,write_json)
from common.integrity.opencode_capture import capture_opencode,command_metrics,opencode_command,write_result,agent_manifest
from common.integrity.attempt_container import isolated_attempt
from common.integrity.evaluator_access_policy import answer_blind_env,generation_event_guard,POLICY_EXIT_CODE
from common.integrity.capsule_workspace import prepare_workspace,validate_native_result
from conditions.a.runtime.compliance_audit import compliance_audit
NATIVE_PROMPT=ROOT/'conditions/a/prompt.md'
LEGACY_ROOT=ROOT
substrate.NATIVE_BASH_ENV=ROOT/'conditions/a/agent_bix_bash_env.sh'
REFERENCE=ROOT/'archive/a_wsp_v121/reference/a_wsp_v121_formal_comparison.json';OUT=ROOT/'analysis/a_v1_graded_compliance_generation.json';REPORT=ROOT/'reports/A_V1_GRADED_COMPLIANCE_GENERATION.md';MANIFEST=ROOT/'analysis/a_v1_graded_compliance_manifest.json'
SOLVER_PROMPT=NATIVE_PROMPT.read_text(encoding='utf-8')+'\n\nPersist every task-specific program, query, or multi-line script before execution as a complete runnable file under `analysis/scripts/`. Do not execute task-specific code only inline, from a temporary file, or through an interactive session. For every resolved question, its candidate evidence must name the existing `analysis/scripts/...` source file(s) that performed its substantive computation; if no reproducible computation is available, return that question unresolved.\n'
def caps(selected_short_ids=None,selected_capsule_uuids=None):
 g=defaultdict(list)
 for r in load_rows():g[r['capsule_uuid']].append(public_question(r))
 if len(g)!=19 or sum(map(len,g.values()))!=78:raise RuntimeError('expected 78/19')
 items=[{'short_id':v[0]['short_id'],'capsule_uuid':k,'question_ids':[q['question_id'] for q in v]} for k,v in sorted(g.items(),key=lambda x:(x[1][0]['short_id'],x[0]))]
 if selected_short_ids is not None:
  found={item['short_id'] for item in items};missing=selected_short_ids-found
  if missing:raise ValueError(f'unknown short IDs: {sorted(missing)}')
  items=[item for item in items if item['short_id'] in selected_short_ids]
 if selected_capsule_uuids is not None:
  found={item['capsule_uuid'] for item in items};missing=selected_capsule_uuids-found
  if missing:raise ValueError(f'unknown capsule UUIDs: {sorted(missing)}')
  items=[item for item in items if item['capsule_uuid'] in selected_capsule_uuids]
 return items
def preflight(items,timeout):
 ref=json.loads(REFERENCE.read_text())['manifest']; current={'model':MODEL,'reasoning_effort':REASONING_EFFORT,'timeout_seconds':timeout,'question_source_sha256':sha256_file(QUESTIONS),'environment':environment_manifest(),'candidate_policy':candidate_policy(),'native_prompt_sha256':hashlib.sha256(SOLVER_PROMPT.encode()).hexdigest(),'native_schema_sha256':sha256_file(NATIVE_SCHEMA)}
 expected={k:ref[k] for k in current}
 diffs={k:{'expected':expected[k],'observed':current[k]} for k in current if expected[k]!=current[k]}
 if "environment" in diffs and (MODEL, REASONING_EFFORT) != ("gpt-5.6-terra", "medium"):
  old_env=diffs["environment"]["expected"]["values"]
  new_env=diffs["environment"]["observed"]["values"]
  comparable_old={k:v for k,v in old_env.items() if k not in {"PATH","model","reasoning_effort"}}
  comparable_new={k:v for k,v in new_env.items() if k not in {"PATH","model","reasoning_effort"}}
  if comparable_old==comparable_new:
   diffs.pop("environment")
 if 'native_prompt_sha256' in diffs:
  diffs.pop('native_prompt_sha256')
  current['solver_visible_protocol_delta']='Historical Native one-shot prompt plus mandatory task-code persistence under analysis/scripts.'
 # A non-default model/effort is a deliberately labelled replication variant,
 # not a claim of byte-identical parity with the historical Terra/medium run.
 model_variant={"model", "reasoning_effort"}
 if set(diffs).issubset(model_variant) and (MODEL, REASONING_EFFORT) != ("gpt-5.6-terra", "medium"):
  current["model_variant"]={"historical_reference":{"model":"gpt-5.6-terra","reasoning_effort":"medium"},"selected":{"model":MODEL,"reasoning_effort":REASONING_EFFORT}}
  diffs={}
 if set(diffs)=={'environment'}:
  old_env=expected['environment']['values']; new_env=current['environment']['values']
  if {k: v for k,v in old_env.items() if k!='PATH'}=={k: v for k,v in new_env.items() if k!='PATH'}:
   diffs={}
   current['environment_executor_path_drift']={'status':'allowed_non-solver-semantic-drift','historical_path_sha256':expected['environment']['sha256'],'current_path_sha256':current['environment']['sha256'],'reason':'Codex temporary executable and IDE paths are session-specific; all non-PATH environment fields matched.'}
 if diffs:raise RuntimeError('material substrate mismatch: '+json.dumps(diffs,ensure_ascii=False))
 p={'experiment':'A-v1 capsule executable workflow with graded compliance','reference_a_analysis_sha256':sha256_file(REFERENCE),'substrate':current,'capsules':items,'native_policy':{'max_attempts':1,'retry_rate':0,'mean_attempts_per_capsule':1.0,'a_wsp':False,'reflection':False,'multihead':False,'generation_scorer_or_evaluator':False,'compliance_policy':'fatal_question_local_soft'}}
 if MANIFEST.exists():
  existing=json.loads(MANIFEST.read_text())
  comparable_existing=json.loads(json.dumps(existing));comparable_current=json.loads(json.dumps(p))
  for candidate in (comparable_existing,comparable_current):
   environment=candidate['substrate'].pop('environment',None)
   candidate['substrate'].pop('environment_executor_path_drift',None)
   if environment: environment.pop('sha256',None);environment.get('values',{}).pop('PATH',None)
  if comparable_existing!=comparable_current:raise RuntimeError('existing one-shot manifest mismatch')
  p=existing
 else:write_json(MANIFEST,p)
 return p
def one(item,rows,root,timeout):
 w,_=prepare_workspace('0000',root,rows);cmd=opencode_command(SOLVER_PROMPT,NATIVE_SCHEMA,MODEL)
 with isolated_attempt(cmd,answer_blind_env(controlled_env(w)),w,schema=NATIVE_SCHEMA,reasoning_effort=REASONING_EFFORT) as (argv,env):
  rc,ev,err,rt=capture_opencode(argv,env,timeout,event_guard=generation_event_guard(w),policy_exit_code=POLICY_EXIT_CODE)
 write_json(w/'event_timeline.json',ev);(w/'events.jsonl').write_text(''.join(json.dumps(z['event'])+'\n' for z in ev));(w/'stderr.log').write_text(err)
 r={'short_id':item['short_id'],'capsule_uuid':item['capsule_uuid'],'question_ids':item['question_ids'],'workspace':str(w),'attempt_count':1,'retry_attempted':False,'total_agent_calls':1,'return_code':rc,'runtime_seconds':rt,'controller_status':'failed','accepted':False,'event_metrics':command_metrics(ev)}
 try:
  if rc!=0:raise RuntimeError('generation timeout' if rc is None else f'generation exit={rc}')
  result=write_result(ev,NATIVE_SCHEMA,w/'capsule_result.json');qs=validate_native_result(result,rows)
  audit=compliance_audit(w,ev,qs);write_json(w/'analysis/compliance_audit.json',audit)
  if audit['fatal']:raise RuntimeError('fatal compliance breach: '+json.dumps(audit['fatal'],ensure_ascii=False))
  r.update({'controller_status':'passed','accepted':True,'isolation_audit':True,'path_audit_evidence_count':len(audit['path_audit_evidence']),'question_local_failures':audit['question_local'],'soft_warnings':audit['soft'],'code_provenance':{'source_files':[{'path':path,'sha256':sha256_file(w/path)} for path in audit['persisted_source_files']],'inline_code_commands':[warning['detail'] for warning in audit['soft'] if warning['kind'].startswith('inline')],'executed_persisted_source':audit['executed_persisted_source']},'question_completion':len(qs),'resolved':sum(q['status']=='resolved' and q['question_id'] not in audit['question_local'] for q in qs),'unresolved':sum(q['status']=='unresolved' or q['question_id'] in audit['question_local'] for q in qs)})
 except Exception as x:r.update({'failure_reason':f'{type(x).__name__}: {x}','isolation_audit':False})
 write_json(w/'run_manifest.json',r);return r
def main():
 global MANIFEST,MODEL,OUT,REASONING_EFFORT,REPORT
 p=argparse.ArgumentParser();p.add_argument('--execute',action='store_true');p.add_argument('--workers',type=int,default=59);p.add_argument('--short-ids',help='comma-separated explicit capsule subset');p.add_argument('--capsule-uuids',help='comma-separated exact capsule UUID subset');p.add_argument('--timeout',type=int,default=TIMEOUT_SECONDS);p.add_argument('--model',default=MODEL);p.add_argument('--reasoning-effort',default=REASONING_EFFORT);p.add_argument('--run-root',type=Path,default=ROOT/'runs/a/a_v1_graded_compliance');p.add_argument('--manifest',type=Path,default=MANIFEST);p.add_argument('--output',type=Path,default=OUT);p.add_argument('--report',type=Path,default=REPORT);a=p.parse_args();MANIFEST=a.manifest.resolve();OUT=a.output.resolve();REPORT=a.report.resolve();MODEL=a.model;REASONING_EFFORT=a.reasoning_effort;substrate.MODEL=MODEL;substrate.REASONING_EFFORT=REASONING_EFFORT
 if a.workers<1:raise SystemExit('workers must be positive')
 selected_short_ids={x.strip() for x in a.short_ids.split(',') if x.strip()} if a.short_ids else None
 selected_capsule_uuids={x.strip() for x in a.capsule_uuids.split(',') if x.strip()} if a.capsule_uuids else None
 items=caps(selected_short_ids,selected_capsule_uuids);m=preflight(items,a.timeout)
 question_count=sum(len(i['question_ids']) for i in items)
 if not a.execute:print(json.dumps({'status':'substrate_audit_passed','capsules':len(items),'questions':question_count,'max_attempts':1},indent=2));return
 if a.run_root.exists() and any(a.run_root.iterdir()):raise SystemExit('refusing nonempty run root')
 st=time.monotonic();rows=load_rows();tasks=[(i,[public_question(r) for r in rows if r['capsule_uuid']==i['capsule_uuid']]) for i in items]
 with concurrent.futures.ThreadPoolExecutor(max_workers=min(a.workers,len(tasks))) as pool: records=list(pool.map(lambda x:one(x[0],x[1],a.run_root.resolve(),a.timeout),tasks))
 freeze=[]
 for r in records:
  w=Path(r['workspace'])
  for f in (w/'capsule_result.json',w/'event_timeline.json',w/'events.jsonl',w/'stderr.log',w/'run_manifest.json',w/'isolation_boundary.json',w/'analysis'/'compliance_audit.json',*sorted((w/'analysis'/'scripts').rglob('*') if (w/'analysis'/'scripts').is_dir() else [])):
   if not f.is_file():continue
   freeze.append({'path':str(f.relative_to(ROOT)),'sha256':sha256_file(f) if f.is_file() else None})
 payload={'manifest':m,'native_oneshot':records,'freeze':{'status':'frozen_before_private_scoring','at_utc':dt.datetime.now(dt.timezone.utc).isoformat(),'artifacts':freeze,'manifest_sha256':sha256_file(MANIFEST),'reference_a_sha256':sha256_file(REFERENCE)},'generation_summary':{'capsules':len(items),'questions':question_count,'controller_passed_capsules':sum(r['accepted'] for r in records),'total_agent_calls':len(items),'retry_rate':0,'mean_attempts_per_capsule':1.0,'runtime_seconds_all_attempts':sum(r['runtime_seconds'] for r in records),'wall_runtime_seconds':time.monotonic()-st},'scoring':{'status':'not_started'}};write_json(OUT,payload)
 REPORT.write_text(f'# A-v1 Capsule Executable Workflow — Graded Compliance\n\n> 文件路径：`{REPORT}`\n\n> 文件说明：本文件以历史 84-version capsule one-shot prompt 为底座，要求任务代码落盘与每题 source evidence，并采用 FATAL / QUESTION_LOCAL / SOFT 合规分类；private post-hoc scoring 在冻结后另行执行。\n\nA used exactly {len(items)} fresh OpenCode calls for {question_count} questions, `max_attempts=1`, retry rate `0`, and mean attempts/capsule `1.0`.\n\nA controller-passed capsules: {payload["generation_summary"]["controller_passed_capsules"]}/{len(items)}. No scorer/evaluator was used during generation.\n')
 print(json.dumps(payload['generation_summary'],indent=2))
if __name__=='__main__':main()
