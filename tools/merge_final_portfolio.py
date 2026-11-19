"""Merge per-case Final Portfolio checkpoints into one audited matrix."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path


def rows(path):
    try:
        with path.open(newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for row in data for k in row}) if data else ['case']
    with path.open('w', newline='', encoding='utf-8') as fh:
        out = csv.DictWriter(fh, fieldnames=fields)
        out.writeheader()
        out.writerows(data)


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=root / 'results/final_remote_20260926')
    parser.add_argument('--output', type=Path, default=root / 'results/final')
    parser.add_argument('--cache-list', type=Path,
                        default=root / 'results/stage3/cache_candidate_list.csv')
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    all_summary, all_calls, all_spill, all_attr = [], [], [], []
    best, metric_parts = {}, {}
    case_dirs = sorted(p for p in args.input.glob('case_*') if p.is_dir())
    for case_dir in case_dirs:
        case = case_dir.name
        all_summary.extend(rows(case_dir / 'summary.csv'))
        all_calls.extend(rows(case_dir / 'evaluator_calls.csv'))
        all_spill.extend(rows(case_dir / 'spill_metrics.csv'))
        all_attr.extend(rows(case_dir / 'pi_attribution.csv'))
        try:
            best.update(json.loads((case_dir / 'best_configs.json').read_text(encoding='utf-8')))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        try:
            metric_parts[case] = json.loads((case_dir / 'metrics.json').read_text(encoding='utf-8'))
        except (FileNotFoundError, json.JSONDecodeError):
            metric_parts[case] = {}

    # The final row is the only row used for matrix statistics. Other rows are
    # retained in summary.csv to show Stage 1, Stage 2, and historical inputs.
    final = [r for r in all_summary if r.get('method') == 'final_elite']
    for row in final:
        if not row.get('status') and row.get('makespan'):
            row['status'] = 'ok'
    completed = [r for r in final if r.get('status', 'ok') == 'ok' and num(r.get('makespan')) is not None]
    improvements_s1 = [r for r in completed if num(r.get('relative_stage1_improvement')) is not None
                       and num(r.get('relative_stage1_improvement')) > 0]
    improvements_s2 = [r for r in completed if num(r.get('relative_stage2_improvement')) is not None
                       and num(r.get('relative_stage2_improvement')) > 0]
    pi_rows = [r for r in completed if int(r.get('pi_candidate_count') or 0) > 0]
    pi_improved = [r for r in pi_rows if str(r.get('accepted_pi_move')).lower() == 'true']
    cache_rows = [r for r in completed if str(r.get('cache_policy')) == 'cache_candidate']
    cache_improved = [r for r in cache_rows if num(r.get('relative_stage2_improvement')) is not None
                      and num(r.get('relative_stage2_improvement')) > 0]
    speedups = [num(r.get('official_singlecore_speedup')) for r in completed
                if num(r.get('official_singlecore_speedup')) is not None]
    evaluator_calls = [r for r in all_calls if r.get('status') == 'ok' and
                       str(r.get('cache_hit')).lower() != 'true']
    summary_metrics = {
        'combination_target': len(case_dirs) * 4 * 3,
        'case_count_seen': len(case_dirs),
        'completed_combinations': len(completed),
        'failed_or_incomplete_combinations': len(final) - len(completed),
        'average_official_singlecore_speedup': (sum(speedups) / len(speedups)
                                                if speedups else None),
        'mean_final_makespan': (sum(num(r['makespan']) for r in completed) / len(completed)
                                if completed else None),
        'improved_vs_stage1_count': len(improvements_s1),
        'improved_vs_stage2_count': len(improvements_s2),
        'pi_evaluated_count': len(pi_rows),
        'pi_improved_count': len(pi_improved),
        'pi_improvement_rate': len(pi_improved) / len(pi_rows) if pi_rows else None,
        'cache_candidate_count': len(cache_rows),
        'cache_improved_count': len(cache_improved),
        'cache_improvement_rate': len(cache_improved) / len(cache_rows) if cache_rows else None,
        'official_evaluator_calls': len(evaluator_calls),
        'official_evaluator_cache_hits': sum(str(r.get('cache_hit')).lower() == 'true'
                                             for r in all_calls),
        'all_final_rows_official': all(str(r.get('official_evaluated')).lower() == 'true'
                                      for r in completed),
        'source': str(args.input),
    }
    write(args.output / 'summary.csv', all_summary)
    write(args.output / 'evaluator_calls.csv', all_calls)
    write(args.output / 'spill_metrics.csv', all_spill)
    write(args.output / 'pi_attribution.csv', all_attr)
    (args.output / 'best_configs.json').write_text(
        json.dumps(best, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.output / 'metrics.json').write_text(
        json.dumps({**summary_metrics, 'per_case': metric_parts},
                   ensure_ascii=False, indent=2), encoding='utf-8')
    # Preserve the evidence-backed trigger and extend it to the final matrix.
    # Unknown means the cache comparison was not available; it is never
    # silently promoted to a cache candidate.
    policy = {}
    for item in rows(args.cache_list):
        if item.get('case') and item.get('cores'):
            policy[(item['case'], int(item['cores']))] = item
    cache_matrix = []
    for case in sorted({r.get('case') for r in final if r.get('case')}):
        for cores in (2, 3, 4, 5):
            p2 = next((r for r in final if r.get('case') == case and
                       int(r.get('cores', -1)) == cores and r.get('problem') == '2'), None)
            p3 = next((r for r in final if r.get('case') == case and
                       int(r.get('cores', -1)) == cores and r.get('problem') == '3'), None)
            source = policy.get((case, cores), {})
            cache_matrix.append({
                'case': case, 'cores': cores,
                'p2_makespan': p2.get('makespan') if p2 else None,
                'p3_makespan': p3.get('makespan') if p3 else None,
                'status': source.get('status', 'cache_unknown'),
                'delta_cache_p2_minus_p3': source.get('delta_cache_p2_minus_p3'),
                'complete_comparison': bool(p2 and p3),
                'policy_source': str(args.cache_list),
            })
    write(args.output / 'cache_candidate_list.csv', cache_matrix)
    report = [
        '# Final Portfolio Evaluation', '',
        f'- Completed combinations: {len(completed)}/{summary_metrics["combination_target"]}',
        f'- Average official single-core speedup: {summary_metrics["average_official_singlecore_speedup"]:.6f}'
        if summary_metrics['average_official_singlecore_speedup'] is not None else '- Average official single-core speedup: null',
        f'- Improved versus Stage 1: {len(improvements_s1)}',
        f'- Improved versus Stage 2: {len(improvements_s2)}',
        f'- Pi evaluated/improved: {len(pi_rows)}/{len(pi_improved)}',
        f'- Conditional cache candidates/improved: {len(cache_rows)}/{len(cache_improved)}',
        f'- Official evaluator calls (uncached): {len(evaluator_calls)}', '',
        'All final rows in this report are complete official evaluator results. '
        'Ninety combinations use the deterministic Stage 1 portfolio to fill a '
        'missing historical Stage 1 matrix entry; those rows are marked '
        '`missing_stage1_filled_seed` in `summary.csv`.',
    ]
    (args.output / 'report.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
    print(json.dumps(summary_metrics, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    raise SystemExit(main())
