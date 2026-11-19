"""Controlled evaluator comparison for a fixed candidate pool.

Both arms generate exactly the same deterministic Stage 1 pool.  The baseline
evaluates the whole pool; the surrogate arm evaluates only its ranked top-K
plus structural exploration reserves.  This is a small ablation driver, not a
replacement for the frozen Final Portfolio run.
"""
from __future__ import annotations
import argparse, csv, json, time
from pathlib import Path
from scheduler.candidate import candidate_signature, validate_candidate
from scheduler.evaluation.official_runner import OfficialRunner
from scheduler.evaluation.surrogate import HistorySurrogate
from scheduler.graph_model import build_graph_model
from scheduler.search.portfolio import build_candidates

def write(path, rows):
    path.parent.mkdir(parents=True,exist_ok=True)
    fields=sorted({k for r in rows for k in r}) if rows else ['case']
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)

def main(argv=None):
    root=Path(__file__).resolve().parents[1]
    ap=argparse.ArgumentParser();ap.add_argument('--cases',default='case_001,case_005');ap.add_argument('--problem',type=int,default=1);ap.add_argument('--cores',type=int,default=4);ap.add_argument('--pool-size',type=int,default=12);ap.add_argument('--top-k',type=int,default=4);ap.add_argument('--exploration',type=int,default=2);ap.add_argument('--timeout',type=float,default=180);ap.add_argument('--model',type=Path,default=root/'results/surrogate_audit_20260927/model.json');ap.add_argument('--output',type=Path,default=root/'results/surrogate_ablation_20260927');args=ap.parse_args(argv)
    surrogate=HistorySurrogate.from_dict(json.loads(args.model.read_text(encoding='utf-8')))
    runner=OfficialRunner(root,root/'data/config.txt');summary=[];calls=[]
    for case in [x for x in args.cases.split(',') if x]:
        graph_path=root/'data'/f'{case}.json'; graph=json.loads(graph_path.read_text(encoding='utf-8')); model=build_graph_model(graph)
        pool=build_candidates(model,args.cores,'A' if args.problem==1 else 'B',max_candidates=args.pool_size)
        ranked=surrogate.rank(pool,model,args.problem,args.cores,top_k=args.top_k,exploration=args.exploration)
        selected={repr(candidate_signature(model,item[2])):item for item in ranked}
        arms=[('baseline',pool),('surrogate',[item[2] for item in ranked])]
        outcomes={}
        for arm, candidates in arms:
            best=None; started_arm=time.perf_counter()
            for index,candidate in enumerate(candidates):
                legal,reason=validate_candidate(model,candidate)
                row={'case':case,'problem':args.problem,'cores':args.cores,'arm':arm,'index':index,'candidate_name':candidate.generator_name,'legal':legal,'reason':reason if not legal else '','predicted_makespan':selected.get(repr(candidate_signature(model,candidate)),(None,None,None,{}))[0] if arm=='surrogate' else None}
                if legal:
                    started=time.perf_counter()
                    try:
                        result=runner.evaluate(graph_path,candidate,args.problem,label=f'{arm}_{index}',timeout=args.timeout)
                        row.update({'status':'ok','makespan':result.makespan,'added_copy_bytes':result.added_copy_bytes,'runtime':time.perf_counter()-started})
                        if best is None or (result.makespan,result.added_copy_bytes)<(best[0],best[1]):best=(result.makespan,result.added_copy_bytes,candidate)
                    except Exception as exc:
                        row.update({'status':'failed','error':str(exc),'runtime':time.perf_counter()-started})
                else:row.update({'status':'illegal','makespan':None,'added_copy_bytes':None,'runtime':0.0})
                calls.append(row)
            outcomes[arm]={'best_makespan':best[0] if best else None,'best_added_copy_bytes':best[1] if best else None,'evaluated':sum(r['case']==case and r['arm']==arm and r['status']=='ok' for r in calls),'wall_seconds':time.perf_counter()-started_arm}
        base=outcomes['baseline'];sur=outcomes['surrogate']
        summary.append({'case':case,'problem':args.problem,'cores':args.cores,'pool_size':len(pool),'surrogate_selected':len(ranked),'baseline_evaluator_calls':base['evaluated'],'surrogate_evaluator_calls':sur['evaluated'],'baseline_best_makespan':base['best_makespan'],'surrogate_best_makespan':sur['best_makespan'],'surrogate_minus_baseline':(sur['best_makespan']-base['best_makespan']) if sur['best_makespan'] is not None and base['best_makespan'] is not None else None,'baseline_wall_seconds':base['wall_seconds'],'surrogate_wall_seconds':sur['wall_seconds'],'same_candidate_pool':True,'official_evaluated':True})
        write(args.output/'summary.csv',summary);write(args.output/'calls.csv',calls)
    report=['# Surrogate ablation','', '- Both arms used the same deterministic candidate pool and the unmodified official evaluator.','- `baseline` evaluates the whole pool; `surrogate` evaluates ranked top-K plus exploration reserves.','- This is a controlled small experiment; it does not modify frozen results or claim generalization to all large graphs.']
    (args.output/'report.md').write_text('\n'.join(report)+'\n',encoding='utf-8')

if __name__=='__main__':main()
