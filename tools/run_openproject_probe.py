"""Re-evaluate selected openproject plans with this repository's evaluator."""
import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

from scheduler.evaluation.official_runner import OfficialRunner


def main():
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[1]
    ap.add_argument('--plans', type=Path, required=True)
    ap.add_argument('--cases', required=True)
    ap.add_argument('--problem', type=int, default=1)
    ap.add_argument('--cores', type=int, default=5)
    ap.add_argument('--timeout', type=float, default=300)
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    runner = OfficialRunner(root, root / 'data/config.txt')
    cases = [x for x in args.cases.split(',') if x]

    def evaluate_one(case):
        graph_path = root / 'data' / f'{case}.json'
        plan_path = args.plans / f'{case}_p{args.problem}_k{args.cores}.json'
        row = {'case': case, 'problem': args.problem, 'cores': args.cores,
               'plan_path': str(plan_path), 'official_evaluated': False}
        try:
            from scheduler.stage2 import candidate_from_plan
            plan = json.loads(plan_path.read_text())
            candidate = candidate_from_plan(plan, 'openproject_plan', {'source': 'openproject'})
            result = runner.evaluate(graph_path, candidate, args.problem,
                                     label='openproject_plan', timeout=args.timeout)
            row.update({'makespan': result.makespan,
                        'added_copy_bytes': result.added_copy_bytes,
                        'runtime_seconds': result.runtime,
                        'official_evaluated': True})
        except Exception as exc:
            row.update({'error': str(exc)})
        return row

    rows = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(evaluate_one, case): case for case in cases}
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    rows.sort(key=lambda row: row['case'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w', newline='') as handle:
        fields = sorted({key for row in rows for key in row})
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == '__main__':
    main()
