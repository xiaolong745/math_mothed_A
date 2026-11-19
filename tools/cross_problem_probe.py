"""Evaluate verified plans from another problem under a target evaluator."""
import argparse
import csv
import json
from pathlib import Path

from scheduler.candidate import candidate_signature, validate_candidate
from scheduler.evaluation.official_runner import OfficialRunner
from scheduler.graph_model import build_graph_model
from scheduler.stage2 import candidate_from_plan


def main():
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[1]
    ap.add_argument('--best-configs', type=Path,
                    default=root / 'results/full5_20260927/best_configs.json')
    ap.add_argument('--summary', type=Path,
                    default=root / 'results/full5_20260927/summary.csv')
    ap.add_argument('--cases', required=True)
    ap.add_argument('--targets', default='1,2,3')
    ap.add_argument('--sources', default='1,2,3')
    ap.add_argument('--cores', type=int, default=5)
    ap.add_argument('--timeout', type=float, default=300)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    configs = json.loads(args.best_configs.read_text())
    summary = {}
    with args.summary.open() as handle:
        for row in csv.DictReader(handle):
            if row.get('cores') == str(args.cores):
                summary[(row['case'], row['problem'])] = row
    runner = OfficialRunner(root, root / 'data/config.txt')
    rows = []
    for case in [x for x in args.cases.split(',') if x]:
        graph_path = root / 'data' / f'{case}.json'
        model = build_graph_model(json.loads(graph_path.read_text()))
        seen = set()
        for target in [int(x) for x in args.targets.split(',') if x]:
            base = summary.get((case, str(target)))
            if not base or base.get('included_in_completed_mean') != 'True':
                continue
            for source in [int(x) for x in args.sources.split(',') if x]:
                key = f'{case}|p{source}|k{args.cores}'
                item = configs.get(key)
                if not item or not item.get('plan'):
                    continue
                candidate = candidate_from_plan(item['plan'], f'cross_p{source}_to_p{target}',
                                                {'source_problem': source, 'target_problem': target})
                signature = repr(candidate_signature(model, candidate))
                if signature in seen:
                    continue
                seen.add(signature)
                legal, reason = validate_candidate(model, candidate)
                row = {'case': case, 'target_problem': target,
                       'source_problem': source, 'cores': args.cores,
                       'base_makespan': base.get('final_makespan'),
                       'base_speedup': base.get('speedup'), 'legal': legal,
                       'validation_error': reason, 'official_evaluated': False}
                if legal:
                    try:
                        result = runner.evaluate(graph_path, candidate, target,
                                                 label=candidate.generator_name,
                                                 timeout=args.timeout)
                        row.update({'probe_makespan': result.makespan,
                                    'delta_cycles': result.makespan - int(base['final_makespan']),
                                    'relative_change': result.makespan / int(base['final_makespan']) - 1.0,
                                    'speedup': float(base['singlecore_makespan']) / result.makespan,
                                    'added_copy_bytes': result.added_copy_bytes,
                                    'runtime_seconds': result.runtime,
                                    'official_evaluated': True})
                    except Exception as exc:
                        row['error'] = str(exc)
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', newline='') as handle:
        fields = sorted({key for row in rows for key in row})
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == '__main__':
    main()
