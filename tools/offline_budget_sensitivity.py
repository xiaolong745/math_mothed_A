"""Offline K sensitivity using the saved fixed-pool evaluator records."""
from __future__ import annotations
import argparse, csv, json
from pathlib import Path
from scheduler.candidate import candidate_signature
from scheduler.evaluation.surrogate import HistorySurrogate, feature_dict
from scheduler.graph_model import build_graph_model
from scheduler.search.portfolio import build_candidates
from scheduler.stage2 import _proxy
from tools.offline_selector_ablation import _analytic_scores

def main(argv=None):
    root=Path(__file__).resolve().parents[1]
    ap=argparse.ArgumentParser(); ap.add_argument('--calls',nargs='+',required=True)
    ap.add_argument('--output',type=Path,required=True); ap.add_argument('--model',type=Path,default=root/'results/surrogate_audit_20260927/model.json')
    args=ap.parse_args(argv); model=HistorySurrogate.from_dict(json.loads(args.model.read_text()))
    rows=[]
    for p in args.calls:
        with open(p,newline='') as f: rows.extend(csv.DictReader(f))
    out=[]
    groups={}
    for r in rows: groups.setdefault((r['case'],int(r['problem']),int(r['cores'])),[]).append(r)
    for (case,problem,cores),grp in sorted(groups.items()):
        base=[r for r in grp if r['arm']=='baseline'];
        if not base: continue
        graph_path=root/'data'/f'{case}.json'; graph=json.loads(graph_path.read_text()); gm=build_graph_model(graph)
        pool=build_candidates(gm,cores,'A' if problem==1 else 'B',max_candidates=len(base)); by={int(r['index']):r for r in base}
        if set(by)!=set(range(len(pool))): continue
        feats=[feature_dict(gm,c,problem,cores) for c in pool]; analytic=[_proxy(gm,c)[0] for c in pool]
        pred=[model.predict(f) for f in feats]; pool_best=min(int(r['makespan']) for r in base if r['status']=='ok')
        for k in (3,4,5,6):
            def choose(order):
                sel=[0] if pool else []; rem=[i for i in order if i not in sel]
                sel.extend(rem[:max(0,k-1)]); return sel[:k]
            selections={'analytic':choose(sorted(range(len(pool)),key=lambda i:(analytic[i],i))), 'surrogate':choose(sorted(range(len(pool)),key=lambda i:(pred[i],i)))}
            for method,sel in selections.items():
                best=min(int(by[i]['makespan']) for i in sel if by[i]['status']=='ok')
                out.append({'case':case,'problem':problem,'cores':cores,'budget_k':k,'method':method,'pool_size':len(pool),'selected_count':len(sel),'contains_pool_optimum':best==pool_best,'regret_ratio':(best-pool_best)/pool_best,'pool_best_makespan':pool_best,'source':'saved_calls'})
    args.output.mkdir(parents=True,exist_ok=True); fields=sorted({k for r in out for k in r})
    with (args.output/'budget_detail.csv').open('w',newline='') as f: w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(out)
    summary=[]
    for k in (3,4,5,6):
      for method in ('analytic','surrogate'):
        rr=[r for r in out if int(r['budget_k'])==k and r['method']==method]; regrets=[float(r['regret_ratio']) for r in rr]
        summary.append({'budget_k':k,'method':method,'rows':len(rr),'pool_optimum_rate':sum(r['contains_pool_optimum'] for r in rr)/len(rr),'mean_regret_ratio':sum(regrets)/len(regrets),'worst_regret_ratio':max(regrets)})
    with (args.output/'budget_summary.csv').open('w',newline='') as f: w=csv.DictWriter(f,fieldnames=sorted(summary[0]));w.writeheader();w.writerows(summary)
if __name__=='__main__': main()
