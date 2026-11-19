"""Select per-case P1 plans from verified current/openproject evaluations."""
import argparse
import csv
import json
from pathlib import Path

from scheduler.candidate import validate_candidate
from scheduler.graph_model import build_graph_model
from scheduler.stage2 import candidate_from_plan


def read_csv(path):
    with path.open(newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


def main():
    ap = argparse.ArgumentParser()
    root = Path(__file__).resolve().parents[1]
    ap.add_argument('--current-summary', type=Path,
                    default=root / 'results/full5_20260927/summary.csv')
    ap.add_argument('--current-configs', type=Path,
                    default=root / 'results/full5_20260927/best_configs.json')
    ap.add_argument('--open-evaluations', type=Path,
                    default=root / 'results/stage3/openproject_p1_full100.csv')
    ap.add_argument('--open-plans', type=Path, required=True)
    ap.add_argument('--open-results', type=Path, required=True)
    ap.add_argument('--output-csv', type=Path,
                    default=root / 'results/stage3/openproject_p1_merged_summary.csv')
    ap.add_argument('--output-configs', type=Path,
                    default=root / 'results/stage3/openproject_p1_selected_configs.json')
    args = ap.parse_args()
    current_rows = {r['case']: r for r in read_csv(args.current_summary)
                    if r.get('problem') == '1' and r.get('cores') == '5'}
    open_rows = {r['case']: r for r in read_csv(args.open_evaluations)}
    open_official = {}
    for row in read_csv(args.open_results):
        if row.get('problem') == '1' and row.get('cores') == '5':
            open_official[row['case']] = row
    current_configs = json.loads(args.current_configs.read_text())
    merged_rows, selected = [], {}
    for i in range(1, 101):
        case = f'case_{i:03d}'
        current = current_rows[case]
        external = open_rows[case]
        official_ref = open_official[case]
        current_ms = int(current['final_makespan']) if current.get('final_makespan') else None
        external_ms = int(external['makespan']) if external.get('official_evaluated') == 'True' else None
        if current_ms is None and external_ms is None:
            continue
        current_plan = current_configs.get(f'{case}|p1|k5', {}).get('plan')
        external_plan_path = args.open_plans / f'{case}_p1_k5.json'
        external_plan = json.loads(external_plan_path.read_text())
        use_external = external_ms is not None and (current_ms is None or external_ms < current_ms)
        chosen_ms = external_ms if use_external else current_ms
        chosen_plan = external_plan if use_external else current_plan
        model = build_graph_model(json.loads((root / 'data' / f'{case}.json').read_text()))
        candidate = candidate_from_plan(chosen_plan, 'openproject_p1' if use_external else 'current_verified_p1')
        legal, reason = validate_candidate(model, candidate)
        if not legal:
            raise RuntimeError(f'{case}: selected verified plan became illegal: {reason}')
        single = current.get('singlecore_makespan') or official_ref.get('singlecore_makespan')
        speedup = float(single) / chosen_ms if single else None
        row = {
            'case': case, 'problem': 1, 'cores': 5,
            'current_makespan': current_ms, 'openproject_makespan': external_ms,
            'selected_makespan': chosen_ms,
            'selected_source': 'openproject_plan' if use_external else 'current_verified',
            'official_evaluated': True, 'legal': legal,
            'singlecore_makespan': single, 'speedup': speedup,
            'improved_vs_current': bool(use_external),
            'current_status': current.get('status'),
            'openproject_runtime_seconds': external.get('runtime_seconds'),
            'added_copy_bytes': external.get('added_copy_bytes') if use_external else current.get('added_copy_bytes'),
        }
        merged_rows.append(row)
        selected[f'{case}|p1|k5'] = {
            'case': case, 'problem': 1, 'cores': 5,
            'makespan': chosen_ms, 'plan': candidate.to_plan(),
            'selected_source': row['selected_source'],
            'official_evaluated': True, 'legal': True,
            'current_makespan': current_ms, 'openproject_makespan': external_ms,
            'singlecore_makespan': single, 'speedup': speedup,
        }
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.output_csv.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=sorted({k for r in merged_rows for k in r}))
        writer.writeheader()
        writer.writerows(merged_rows)
    args.output_configs.write_text(json.dumps(selected, ensure_ascii=False, indent=2))
    print(json.dumps({'cases': len(merged_rows),
                      'openproject_selected': sum(r['selected_source'] == 'openproject_plan' for r in merged_rows),
                      'current_retained': sum(r['selected_source'] == 'current_verified' for r in merged_rows),
                      'official_and_legal': all(r['official_evaluated'] and r['legal'] for r in merged_rows)},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
