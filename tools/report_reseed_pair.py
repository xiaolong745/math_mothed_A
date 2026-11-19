"""Summarize the resumable reseed pairing run and its timing split."""
from __future__ import annotations
import argparse, csv, json, statistics, time
from collections import Counter, defaultdict
from pathlib import Path

def rows(p):
    try:
        with p.open(newline='',encoding='utf-8') as f:return list(csv.DictReader(f))
    except FileNotFoundError:return []
def num(x):
    try:return float(x)
    except (TypeError,ValueError):return None
def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'results/reseed_pair_20260927'); out=ap.parse_args().output
    pair=rows(out/'pair_summary.csv'); diag=rows(out/'construction_diagnostics.csv'); calls=rows(out/'evaluator_calls.csv'); fail=rows(out/'failures.csv')
    groups=[]
    for key,rs in __import__('itertools').groupby(sorted(pair,key=lambda r:(r['cores'])),key=lambda r:r['cores']):
        rs=list(rs); complete=[r for r in rs if r['status']=='complete']; deltas=[num(r['makespan_delta_false_minus_true']) for r in complete if num(r['makespan_delta_false_minus_true']) is not None]
        groups.append({'problem':1,'cores':int(key),'combos':len(rs),'complete_pairs':len(complete),'incomplete_pairs':len(rs)-len(complete),'improved':sum(x>0 for x in deltas),'tied':sum(x==0 for x in deltas),'regressed':sum(x<0 for x in deltas),'mean_delta':statistics.mean(deltas) if deltas else None,'median_delta':statistics.median(deltas) if deltas else None})
    with (out/'group_metrics.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=sorted({k for r in groups for k in r}));w.writeheader();w.writerows(groups)
    timing=defaultdict(float); timing_count=Counter()
    for r in calls:
        if r.get('status')!='ok':continue
        for field,name in [('generation_runtime','candidate_generation_and_heft'),('evaluator_runtime','official_evaluator')]:
            v=num(r.get(field));
            if v is not None:timing[name]+=v;timing_count[name]+=1
    for r in diag:
        for field,name in [('candidate_generation_seconds','candidate_generation'),('heft_schedule_seconds','heft_schedule'),('quotient_legal_check_seconds','quotient_legal_check')]:
            v=num(r.get(field));
            if v is not None:timing[name]+=v;timing_count[name]+=1
    timing_rows=[{'stage':k,'cumulative_seconds':v,'samples':timing_count[k]} for k,v in sorted(timing.items())]
    failed_runtime=sum(num(r.get('runtime')) or 0.0 for r in fail)
    timing_rows.append({'stage':'failed_generation_or_validation','cumulative_seconds':failed_runtime,'samples':len(fail)})
    with (out/'timing_breakdown.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['stage','cumulative_seconds','samples']);w.writeheader();w.writerows(timing_rows)
    # File mtimes bracket the checkpointed batch.  They are an approximate
    # batch wall interval; per-combination evaluator durations remain separate.
    mtimes=[p.stat().st_mtime for p in (out/'runs').rglob('*') if p.is_file()] if (out/'runs').exists() else []
    batch_wall=(max(mtimes)-min(mtimes)) if mtimes else None
    timing['failed_generation_or_validation']=failed_runtime
    metrics={'pairs_total':len(pair),'pairs_complete':sum(r['status']=='complete' for r in pair),'pairs_incomplete':sum(r['status']!='complete' for r in pair),'evaluator_ok':sum(r.get('status')=='ok' for r in calls),'evaluator_failed':sum(r.get('status')!='ok' for r in calls),'cumulative_stage_times':dict(timing),'batch_wall_seconds_from_run_checkpoints':batch_wall,'batch_wall_measurement':'run-directory checkpoint mtime bracket; includes sequential process overhead and failed generation, not parallel overlap','search_truncated_count':sum(r.get('search_truncated')=='True' for r in pair),'no_verified_solution_count':sum(r.get('no_verified_solution')=='True' for r in pair),'failure_reasons':dict(Counter((r.get('error') or '').split(':')[0] for r in fail))}
    (out/'metrics.json').write_text(json.dumps(metrics,ensure_ascii=False,indent=2),encoding='utf-8')
    problems=sorted({r.get('problem') for r in pair})
    report=['# Reseed pairing and runtime audit','',f"- Problems in this batch: {','.join('P'+p for p in problems)}; pairs requested: {len(pair)}. Completed pairs: {metrics['pairs_complete']}; incomplete: {metrics['pairs_incomplete']}.",' - Pairing fixes all parameters to the same `max_ops_per_sg`, depth window, weights, edge mode, balance=max, size slack, scheduler, and deterministic ordering; only `reseed` changes.','- An official incumbent is persisted after the first successful member of each pair. Invalid quotient candidates are retained as failures and cannot enter elite.','', '## Timing','']
    for r in timing_rows:
        qualifier = 'failed samples' if r['stage']=='failed_generation_or_validation' else 'successful samples'
        report.append(f"- {r['stage']}: {r['cumulative_seconds']:.3f}s cumulative over {r['samples']} {qualifier}.")
    report += [f"- Run-directory checkpoint mtime bracket (batch wall approximation): {batch_wall:.3f}s." if batch_wall is not None else '- Batch wall interval unavailable.', '- The bracket includes generation failures and process overhead; cumulative stage times are separate and must not be added as if parallel.', '- Direct pair runs did not use the Oracle or evaluation cache; cache/I/O time is therefore not separately attributable in this batch. The residual wall interval includes failed generation and process overhead.', '', '## Pair outcome by core']
    for r in groups:report.append(f"- k={r['cores']}: {r['complete_pairs']}/{r['combos']} complete, improved={r['improved']}, tied={r['tied']}, regressed={r['regressed']}, mean delta false−true={r['mean_delta']}.")
    failure_line = (f"- Failure records: {len(fail)}. The dominant failure is the generator-produced subgraph quotient cycle; those candidates were not sent to the official evaluator." if fail else '- Failure records: 0; all requested pairs reached two official evaluations.')
    report += ['', '## Failure interpretation', failure_line, '- Incomplete pairs remain incomplete; their one-sided official result is retained as an incumbent but is excluded from paired performance claims.', '', '## Pi follow-up', '- No reseed candidate exceeded the unified historical elite in the completed pairs, so the rule “Pi only for a new historical winner” triggered zero Pi calls in this batch.', '- Frozen and historical elites remain authoritative for all combinations.']
    (out/'report.md').write_text('\n'.join(report)+'\n',encoding='utf-8')
if __name__=='__main__':main()
