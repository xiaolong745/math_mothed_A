"""Same-plan P2/P3 diagnostic; no candidate search or repeated-min sampling."""
import argparse
from pathlib import Path
from scheduler.candidate import Candidate
from scheduler.evaluation.official_runner import OfficialRunner
from tools.run_residency import ROOT, read, dump, csvwrite, result_dict


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, default=ROOT / 'results/residency_20260927')
    ap.add_argument('--case', default='case_005')
    ap.add_argument('--cores', type=int, default=5)
    args = ap.parse_args()
    combo = args.output / 'runs' / f'{args.case}_p2_k{args.cores}'
    rows = []
    runner = OfficialRunner()
    for method in ('stage2', 'fixed_pc_window', 'macro_32_heft'):
        plan = read(combo / f'{method}_plan.json')
        c = Candidate({int(n): g for n, g in plan['node_to_subgraph'].items()},
                       plan['core_schedules'], method)
        for problem in (2, 3):
            res = runner.evaluate(ROOT / 'data' / f'{args.case}.json', c, problem,
                                   timeout=120)
            dump(args.output / 'paired' / f'{method}_p{problem}.json', res.raw)
            rows.append({'case': args.case, 'cores': args.cores, 'problem': problem,
                         'method': method, 'plan_source': str(combo / f'{method}_plan.json'),
                         **result_dict(res)})
            csvwrite(args.output / 'cache_pairs.csv', rows)
    print(rows)


if __name__ == '__main__':
    main()
