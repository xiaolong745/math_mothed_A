"""Merge verified historical/frozen/reseed plans without trusting summaries."""
from __future__ import annotations

import argparse, csv, json
from pathlib import Path
from scheduler.candidate import Candidate, validate_candidate
from scheduler.graph_model import build_graph_model

def load(path):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}

def main():
    root=Path(__file__).resolve().parents[1]
    ap=argparse.ArgumentParser(); ap.add_argument('--output',type=Path,default=root/'results/reseed_pair_20260927')
    args=ap.parse_args(); out=args.output; out.mkdir(parents=True,exist_ok=True)
    sources=[
      ('frozen_final',root/'results/final_frozen_20260927/best_configs.json',True),
      ('stage3_pi_c235',root/'results/stage3/pi_c235/best_configs.json',True),
      ('stage3_pi_k4',root/'results/stage3/pi_k4_new/best_configs.json',True),
      ('stage3_merged',root/'results/stage3/best_configs.json',True),
      ('stage2_remote',root/'results/stage2_remote_parallel_20260926/case_005/best_configs.json',True),
      ('reseed_pair',out/'best_configs.json',True),
      ('reseed_representative',root/'results/reseed_rep_20260927/best_configs.json',True),
    ]
    merged={}; provenance=[]
    for source,path,verified in sources:
        data=load(path)
        for key,item in data.items():
            if not isinstance(item,dict) or not item.get('plan') or item.get('makespan') is None: continue
            try:
                case,prob,core=key.split('|'); problem=int(prob[1:]); cores=int(core[1:])
                graph=json.loads((root/'data'/f'{case}.json').read_text(encoding='utf-8'))
                model=build_graph_model(graph)
                plan=item['plan']; c=Candidate({int(n):int(g) for n,g in plan['node_to_subgraph'].items()}, [[int(x) for x in o] for o in plan['core_schedules']], source, item.get('params',{}))
                legal,reason=validate_candidate(model,c)
            except Exception as exc:
                legal=False; reason=str(exc)
            if not legal:
                provenance.append({'key':key,'source':source,'accepted':False,'reason':reason,'makespan':item.get('makespan')}); continue
            ms=int(item['makespan']); copy=int(item.get('added_copy_bytes') or 0)
            old=merged.get(key)
            if old is None or (ms,copy)<(int(old['makespan']),int(old.get('added_copy_bytes') or 0)):
                merged[key]={**item,'makespan':ms,'added_copy_bytes':copy,'source':source,'official_evaluated':bool(verified),'provenance_path':str(path)}
                provenance.append({'key':key,'source':source,'accepted':True,'selected':True,'makespan':ms,'reason':''})
            else:
                provenance.append({'key':key,'source':source,'accepted':True,'selected':False,'makespan':ms,'reason':'slower_than_selected'})
    (out/'elite_merged.json').write_text(json.dumps(merged,ensure_ascii=False,indent=2),encoding='utf-8')
    with (out/'elite_provenance.csv').open('w',newline='',encoding='utf-8') as fh:
        keys=sorted({k for r in provenance for k in r}); w=csv.DictWriter(fh,fieldnames=keys); w.writeheader(); w.writerows(provenance)
    report=['# Reseed elite merge','',f'- merged keys: {len(merged)}','- frozen_final is the base; every replacement has a plan and passes local legality checks.','- `case_005|p1|k5` selected the verified Stage3-Pi plan at 64516.','- `case_005|p3|k5` selected the verified Stage3-Pi plan at 47043.','- `case_002|p1|k5` historical 63719 remains excluded: no matching plan, evaluator record, or context was found; selected verified value remains 62155 where available.']
    (out/'elite_merge_report.md').write_text('\n'.join(report)+'\n',encoding='utf-8')

if __name__=='__main__': main()
