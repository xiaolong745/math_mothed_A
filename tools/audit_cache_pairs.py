"""Audit P2/P3 cache differences using the same complete P/C/Pi plan."""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from scheduler.candidate import Candidate, candidate_signature, validate_candidate
from scheduler.evaluation.official_runner import OfficialRunner
from scheduler.graph_model import build_graph_model


def read_rows(path):
    try:
        with path.open(newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def candidate(item):
    plan = item['plan']
    return Candidate({int(k): int(v) for k, v in plan['node_to_subgraph'].items()},
                     [[int(x) for x in row] for row in plan['core_schedules']],
                     item.get('generator_name', 'historical_pair'), item.get('params', {}))


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for row in rows for k in row}) if rows else ['case']
    with path.open('w', newline='', encoding='utf-8') as fh:
        out = csv.DictWriter(fh, fieldnames=fields)
        out.writeheader(); out.writerows(rows)


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--data-dir', type=Path, default=root / 'data')
    ap.add_argument('--output', type=Path, default=root / 'results/stage3/final_cache_pair_audit')
    ap.add_argument('--p2-configs', type=Path, default=root / 'results/stage3/pi_c235/best_configs.json')
    ap.add_argument('--case003-p2', type=Path, default=root / 'results/stage3/pi_case003_p2/best_configs.json')
    ap.add_argument('--k4-config', type=Path, default=root / 'results/stage3/pi_k4_new/best_configs.json')
    ap.add_argument('--timeout', type=float, default=300)
    ap.add_argument('--only', help='single case:cores target, e.g. case_005:4')
    args = ap.parse_args(argv)
    configs = {}
    configs.update(json.loads(args.p2_configs.read_text(encoding='utf-8')))
    configs.update(json.loads(args.case003_p2.read_text(encoding='utf-8')))
    if args.k4_config.exists():
        configs.update(json.loads(args.k4_config.read_text(encoding='utf-8')))
    targets = [('case_005', k) for k in (2, 3, 4, 5)] + [('case_003', 2)]
    if args.only:
        case_name, core_text = args.only.split(':', 1)
        targets = [(case_name, int(core_text))]
    runner = OfficialRunner(root, args.data_dir / 'config.txt')
    rows, calls = [], []
    for case, cores in targets:
        key = f'{case}|p2|k{cores}'
        item = configs.get(key)
        if not item:
            rows.append({'case': case, 'cores': cores, 'status': 'unknown',
                         'reason': 'missing_same_plan_source', 'pair_comparable': False})
            continue
        graph_path = args.data_dir / f'{case}.json'
        model = build_graph_model(json.loads(graph_path.read_text(encoding='utf-8')))
        cand = candidate(item)
        legal, reason = validate_candidate(model, cand)
        sig = repr(candidate_signature(model, cand))
        if not legal:
            rows.append({'case': case, 'cores': cores, 'status': 'invalid_plan',
                         'reason': reason, 'candidate_signature': sig,
                         'pair_comparable': False})
            continue
        result = {}
        for problem in (2, 3):
            started = time.perf_counter()
            try:
                value = runner.evaluate(graph_path, cand, problem,
                                        label=f'paired_{case}_k{cores}_p{problem}',
                                        timeout=args.timeout)
                result[problem] = value
                calls.append({'case': case, 'cores': cores, 'problem': problem,
                              'status': 'ok', 'runtime': time.perf_counter() - started,
                              'makespan': value.makespan,
                              'added_copy_bytes': value.added_copy_bytes,
                              'cache_hit_rate': value.cache_hit_rate,
                              'candidate_signature': sig})
            except Exception as exc:
                calls.append({'case': case, 'cores': cores, 'problem': problem,
                              'status': 'failed', 'runtime': time.perf_counter() - started,
                              'error': str(exc), 'candidate_signature': sig})
        if 2 in result and 3 in result:
            p2, p3 = result[2], result[3]
            rows.append({'case': case, 'cores': cores, 'status': 'paired_official',
                         'pair_comparable': True, 'candidate_source': key,
                         'candidate_signature': sig,
                         'p2_makespan_same_plan': p2.makespan,
                         'p3_makespan_same_plan': p3.makespan,
                         'paired_delta_p2_minus_p3': p2.makespan - p3.makespan,
                         'p2_added_copy_bytes': p2.added_copy_bytes,
                         'p3_added_copy_bytes': p3.added_copy_bytes,
                         'p2_cache_hit_rate': p2.cache_hit_rate,
                         'p3_cache_hit_rate': p3.cache_hit_rate,
                         'official_evaluated': True})
        else:
            rows.append({'case': case, 'cores': cores, 'status': 'incomplete_pair',
                         'pair_comparable': False, 'candidate_source': key,
                         'candidate_signature': sig, 'official_evaluated': False})
    write(args.output / 'paired_results.csv', rows)
    write(args.output / 'evaluator_calls.csv', calls)
    (args.output / 'README.md').write_text(
        '# Paired Cache Audit\n\n'
        'Each row uses one identical complete P/C/Pi plan for both official '
        'P2 and P3 evaluators. The paired delta is descriptive evidence; it is '
        'not the difference between independently optimized P2/P3 elites. '
        'Missing inputs are marked `unknown`.\n', encoding='utf-8')
    print(f'wrote {len(rows)} paired rows and {len(calls)} evaluator calls')


if __name__ == '__main__':
    main()
