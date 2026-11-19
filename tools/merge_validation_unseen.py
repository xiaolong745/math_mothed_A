"""Summarize frozen Final Portfolio validation on untuned cases."""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

csv.field_size_limit(1 << 30)


def rows(path):
    try:
        with path.open(newline='', encoding='utf-8') as fh: return list(csv.DictReader(fh))
    except FileNotFoundError: return []


def write(path, data):
    fields = sorted({k for r in data for k in r}) if data else ['case']
    with path.open('w', newline='', encoding='utf-8') as fh:
        out = csv.DictWriter(fh, fieldnames=fields); out.writeheader(); out.writerows(data)


def f(x):
    try: return float(x)
    except (TypeError, ValueError): return None


def main():
    root = Path(__file__).resolve().parents[1]; out = root / 'results/validation_unseen_20260927'; out.mkdir(parents=True, exist_ok=True)
    selected = rows(out / 'selection.csv')
    finals=[]; calls=[]; failures=[]
    for s in selected:
        case=s['case']; p=out/case
        sr=rows(p/'summary.csv'); cr=rows(p/'evaluator_calls.csv')
        finals.extend([r for r in sr if r.get('method')=='final_elite'])
        calls.extend(cr)
        if not (p/'metrics.json').exists() or not any(r.get('method')=='final_elite' for r in sr):
            reason=(p/'timeout.txt').read_text().strip() if (p/'timeout.txt').exists() else 'missing output'
            failures.append({'case':case,'status':'timeout_or_incomplete','reason':reason})
    existing_failure_cases={r['case'] for r in failures}
    for s in selected:
        if not any(r.get('case')==s['case'] for r in finals) and s['case'] not in existing_failure_cases:
            failures.append({'case':s['case'],'status':'timeout_or_incomplete','reason':'no final elite row'})
    write(out/'summary.csv',finals+failures)
    by_problem=[]
    for problem in ('1','2','3'):
        rs=[r for r in finals if r.get('problem')==problem]
        s1=[f(r.get('stage1_makespan')) for r in rs]; s2=[f(r.get('stage2_makespan')) for r in rs]; fin=[f(r.get('makespan')) for r in rs]
        improve=[(a-b)/a for a,b in zip(s1,fin) if a and b]; s2imp=[(a-b)/a for a,b in zip(s1,s2) if a and b]; piimp=[(a-b)/a for a,b in zip(s2,fin) if a and b]
        by_problem.append({'problem':problem,'completed_cases':len(rs),'selected_cases':len(selected),'mean_speedup':statistics.mean([f(r.get('official_singlecore_speedup')) for r in rs if f(r.get('official_singlecore_speedup')) is not None]) if rs else None,'mean_vs_stage1_improvement':statistics.mean(improve) if improve else None,'median_vs_stage1_improvement':statistics.median(improve) if improve else None,'mean_stage2_vs_stage1_improvement':statistics.mean(s2imp) if s2imp else None,'mean_pi_extra_vs_stage2':statistics.mean(piimp) if piimp else None,'final_improved_vs_stage1':sum(x>0 for x in improve),'final_tied_vs_stage1':sum(x==0 for x in improve),'final_degraded_vs_stage1':sum(x<0 for x in improve),'pi_accepted':sum(r.get('accepted_pi_move')=='True' for r in rs),'uncached_calls':sum(c.get('status')=='ok' and c.get('cache_hit')!='True' for c in calls if c.get('problem')==problem),'cached_calls':sum(c.get('cache_hit')=='True' for c in calls if c.get('problem')==problem),'search_seconds':sum(f(r.get('elapsed_combo')) or 0 for r in rs),'evaluator_seconds':sum(f(c.get('runtime')) or 0 for c in calls if c.get('problem')==problem)})
    write(out/'group_metrics.csv',by_problem); write(out/'evaluator_calls.csv',calls)
    incomplete_names=sorted(set(r['case'] for r in failures))
    meta={'selected_cases':len(selected),'completed_cases':len(set(r['case'] for r in finals)),'completed_combinations':len(finals),'incomplete_cases':len(incomplete_names),'incomplete_case_names':incomplete_names,'same_initial_portfolio':True,'algorithm_frozen':True,'core_counts':[4],'official_evaluated_final_rows':all(r.get('official_evaluated')=='True' for r in finals)}
    (out/'metrics.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf8')
    report=['# Untuned Validation','',f'- Selected cases: {len(selected)}; completed: {len(set(r["case"] for r in finals))}; incomplete/timeout: {len(failures)}.','- Selection used only graph inventory features: size, compute cycles, DAG depth, level width, and M/V ratio; no validation result was used.','- All completed cases use k=4, the frozen Stage 1/Stage 2/Pi code and the frozen budget parameters.','- Incomplete cases and timeout reasons are retained in `summary.csv`.','- Results are validation evidence for untuned graphs, not a claim about all 100 input cases.']
    (out/'report.md').write_text('\n'.join(report)+'\n',encoding='utf8')
    print(json.dumps(meta,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
