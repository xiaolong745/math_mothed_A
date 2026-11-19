"""Measure Stage 2 front-end and official-evaluator costs on representative cases."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler.evaluation.official_runner import OfficialRunner
from scheduler.graph_model import build_graph_model
from scheduler.search.portfolio import build_candidates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--cases', default='case_001,case_005,case_003',
                        help='small, medium, large representative case stems')
    parser.add_argument('--problem', type=int, default=2)
    parser.add_argument('--cores', type=int, default=4)
    parser.add_argument('--timeout', type=float)
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'results/stage2/cost_audit.csv')
    args = parser.parse_args()
    runner = OfficialRunner(ROOT, args.data_dir / 'config.txt')
    rows = []
    for stem in [item.strip() for item in args.cases.split(',') if item.strip()]:
        path = args.data_dir / f'{stem}.json'
        started = time.perf_counter()
        graph = json.loads(path.read_text(encoding='utf-8'))
        parsed = time.perf_counter() - started
        started = time.perf_counter()
        model = build_graph_model(graph)
        modeled = time.perf_counter() - started
        started = time.perf_counter()
        candidates = build_candidates(model, args.cores,
                                      'A' if args.problem == 1 else 'B', 12)
        generated = time.perf_counter() - started
        started = time.perf_counter()
        status, evaluate_runtime, makespan = 'ok', None, None
        try:
            result = runner.evaluate(path, candidates[0], args.problem,
                                     timeout=args.timeout)
            evaluate_runtime = time.perf_counter() - started
            makespan = result.makespan
        except Exception as exc:
            evaluate_runtime = time.perf_counter() - started
            status = f'{type(exc).__name__}: {exc}'
        rows.append({
            'case': stem, 'ops_compute': len(model.ops),
            'candidate_count': len(candidates), 'parse_seconds': parsed,
            'model_seconds': modeled, 'candidate_seconds': generated,
            'one_official_eval_seconds': evaluate_runtime,
            'makespan': makespan, 'status': status,
        })
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    print(f'wrote {args.output}')
    for row in rows:
        print(row)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
