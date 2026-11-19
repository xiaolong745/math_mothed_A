"""Rank an existing Stage 1 portfolio with the history surrogate.

This command does not call the official evaluator.  It is a wiring and
feature audit before a same-budget remote comparison is scheduled.
"""
from __future__ import annotations
import argparse, csv, json
from pathlib import Path
from scheduler.evaluation.surrogate import HistorySurrogate
from scheduler.graph_model import build_graph_model
from scheduler.search.portfolio import build_candidates

def main(argv=None):
    root=Path(__file__).resolve().parents[1]
    ap=argparse.ArgumentParser();ap.add_argument('--case',default='case_001');ap.add_argument('--problem',type=int,default=1);ap.add_argument('--cores',type=int,default=4);ap.add_argument('--model',type=Path,default=root/'results/surrogate_audit_20260927/model.json');ap.add_argument('--output',type=Path,default=root/'results/surrogate_audit_20260927/ranking.csv');args=ap.parse_args(argv)
    graph=json.loads((root/'data'/f'{args.case}.json').read_text(encoding='utf-8')); model=build_graph_model(graph)
    candidates=build_candidates(model,args.cores,'A' if args.problem==1 else 'B',max_candidates=12)
    surrogate=HistorySurrogate.from_dict(json.loads(args.model.read_text(encoding='utf-8')))
    ranked=surrogate.rank(candidates,model,args.problem,args.cores,top_k=min(10,len(candidates)),exploration=min(2,len(candidates)))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('w',newline='',encoding='utf-8') as f:
        fields=['rank','candidate_name','params','predicted_makespan','lower_bound','lb_over_prediction'];w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
        for rank,(pred,lb,candidate,features) in enumerate(ranked,1):
            w.writerow({'rank':rank,'candidate_name':candidate.generator_name,'params':json.dumps(candidate.params,sort_keys=True),'predicted_makespan':pred,'lower_bound':lb,'lb_over_prediction':lb/max(1.0,pred)})
    print(f'pool={len(candidates)} selected={len(ranked)} output={args.output}')
if __name__=='__main__':main()
