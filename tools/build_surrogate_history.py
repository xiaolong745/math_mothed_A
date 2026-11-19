"""Build and audit the dependency-free evaluator-history surrogate."""
from __future__ import annotations
import argparse, csv, json, math
from collections import defaultdict
from pathlib import Path

from scheduler.candidate import Candidate, candidate_signature, validate_candidate
from scheduler.evaluation.surrogate import FEATURE_NAMES, HistorySurrogate, feature_dict
from scheduler.graph_model import build_graph_model

def _load(path):
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}

def _write(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    fields=sorted({k for r in rows for k in r}) if rows else ['case']
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

def _candidate(item):
    plan=item.get('plan') or {}
    return Candidate({int(n):int(g) for n,g in plan['node_to_subgraph'].items()},
                     [[int(x) for x in o] for o in plan['core_schedules']],
                     item.get('generator_name',item.get('source','history')),
                     dict(item.get('params') or {}))

def main(argv=None):
    root=Path(__file__).resolve().parents[1]
    ap=argparse.ArgumentParser();ap.add_argument('--results-root',type=Path,default=root/'results');ap.add_argument('--data-dir',type=Path,default=root/'data');ap.add_argument('--output',type=Path,default=root/'results/surrogate_audit_20260927');args=ap.parse_args(argv)
    candidates=[]; sources=[]
    for path in sorted(args.results_root.rglob('best_configs.json')):
        if args.output in path.parents:continue
        data=_load(path)
        for key,item in data.items():
            if not isinstance(item,dict) or not item.get('plan') or item.get('makespan') is None:continue
            if item.get('official_evaluated') is False:continue
            try:case,prob,core=key.split('|');problem=int(prob[1:]);cores=int(core[1:])
            except (ValueError,IndexError):continue
            graph_path=args.data_dir/f'{case}.json'
            if not graph_path.exists():continue
            try:
                model=build_graph_model(_load(graph_path));candidate=_candidate(item)
                legal,reason=validate_candidate(model,candidate)
                if not legal:continue
                sig=repr(candidate_signature(model,candidate))
                features=feature_dict(model,candidate,problem,cores)
            except Exception:continue
            candidates.append({'case':case,'problem':problem,'cores':cores,'makespan':int(item['makespan']),'added_copy_bytes':int(item.get('added_copy_bytes') or 0),'source':str(path),'generator_name':candidate.generator_name,'signature':sig,'features':features})
    # Keep the best official observation for exactly identical P/C/Pi plans.
    unique={}
    for row in candidates:
        key=(row['case'],row['problem'],row['cores'],row['signature'])
        old=unique.get(key)
        if old is None or (row['makespan'],row['added_copy_bytes'])<(old['makespan'],old['added_copy_bytes']):unique[key]=row
    rows=list(unique.values())
    flat=[]
    for r in rows:flat.append({k:r[k] for k in ('case','problem','cores','makespan','added_copy_bytes','source','generator_name','signature')}|r['features'])
    _write(args.output/'training_features.csv',flat)
    if len(rows)<3:raise RuntimeError(f'only {len(rows)} training rows')
    model=HistorySurrogate(ridge=2.0).fit(rows)
    (args.output/'model.json').write_text(json.dumps(model.to_dict(),indent=2),encoding='utf-8')
    # Case-grouped five-fold audit.  This measures ranking error on held-out
    # cases; it is not used to alter frozen search parameters.
    cases=sorted({r['case'] for r in rows}); folds=[cases[i::5] for i in range(5)]
    audit=[]
    for i,holdout in enumerate(folds):
        train=[r for r in rows if r['case'] not in holdout];test=[r for r in rows if r['case'] in holdout]
        if len(train)<3 or not test:continue
        fitted=HistorySurrogate(ridge=2.0).fit(train); errors=[]
        for r in test:
            pred=fitted.predict(r['features']); errors.append((pred-r['makespan'])/max(1,r['makespan']))
        ordered=sorted(test,key=lambda r:r['makespan']); predicted=sorted(test,key=lambda r:fitted.predict(r['features']))
        top_actual={ (r['case'],r['problem'],r['cores']) for r in ordered[:max(1,len(ordered)//5)] }
        top_pred={ (r['case'],r['problem'],r['cores']) for r in predicted[:max(1,len(predicted)//5)] }
        audit.append({'fold':i,'holdout_cases':','.join(holdout),'train_rows':len(train),'test_rows':len(test),'mean_abs_relative_error':sum(abs(x) for x in errors)/len(errors),'median_relative_error':sorted(errors)[len(errors)//2],'top20_group_recall':len(top_actual&top_pred)/max(1,len(top_actual))})
    _write(args.output/'cross_validation.csv',audit)
    summary={'raw_rows':len(candidates),'unique_rows':len(rows),'cases':len(cases),'case_list':cases,'feature_count':len(FEATURE_NAMES),'model':'standardized_ridge_log_makespan','sklearn_available':False,'official_evaluator_used_in_this_step':False,'training_sources':'best_configs.json only; duplicate candidate signatures deduplicated','cache_or_oracle_features':'not available from plan-only history; omitted rather than imputed'}
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    report=['# Surrogate history audit','',f"- Raw official plan rows: {len(candidates)}; unique P/C/Pi rows: {len(rows)}; cases: {len(cases)}.",' - Features are cheap graph/partition/schedule values; no evaluator call was made while building the model.','- Model: standardized ridge on log Makespan. sklearn/XGBoost/NumPy are unavailable in this environment, so no random forest is claimed.','- Cache/Oracle values are omitted when unavailable; they are not filled with proxy values.','- Cross validation is grouped by case and is diagnostic only; it does not change frozen parameters or elite results.','', '## Limitation','', '- Existing best_configs history contains one verified plan per recorded candidate source, not every intermediate evaluator candidate. This is suitable for an initial ranking audit, not a final generalization claim.']
    (args.output/'report.md').write_text('\n'.join(report)+'\n',encoding='utf-8')

if __name__=='__main__':main()
