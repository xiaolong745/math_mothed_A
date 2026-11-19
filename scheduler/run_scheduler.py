"""Command line entry point for Stage 1 portfolio generation/evaluation."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .evaluation.official_runner import OfficialRunner
from .graph_model import build_graph_model
from .search.portfolio import evaluate_portfolio, write_portfolio_outputs


def _parse_cases(value, data_dir):
    if value:
        return [Path(item) for item in value.split(',')]
    return sorted(
        path for path in Path(data_dir).glob('case_*.json')
        if re.fullmatch(r'case_\d+\.json', path.name)
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description='Stage 1 candidate portfolio')
    parser.add_argument('--graph', help='one graph JSON; omit for --data-dir sweep')
    parser.add_argument('--data-dir', default='data')
    parser.add_argument('--cases', help='comma-separated graph paths')
    parser.add_argument('--cores', default='2,3,4,5')
    parser.add_argument('--problems', default='1,2,3')
    parser.add_argument('--output', default='results/stage1')
    parser.add_argument('--max-candidates', type=int, default=12)
    parser.add_argument('--timeout', type=float)
    parser.add_argument('--no-evaluate', action='store_true')
    args = parser.parse_args(argv)
    graphs = [Path(args.graph)] if args.graph else _parse_cases(args.cases, args.data_dir)
    cores = [int(item) for item in args.cores.split(',') if item]
    problems = [int(item) for item in args.problems.split(',') if item]
    runner = OfficialRunner()
    summary, candidates, best_configs = [], [], {}
    for graph_path in graphs:
        graph = json.loads(graph_path.read_text(encoding='utf-8'))
        model = build_graph_model(graph)
        for num_cores in cores:
            for problem in problems:
                result = evaluate_portfolio(
                    model, graph_path, num_cores, problem,
                    max_candidates=args.max_candidates, runner=runner,
                    timeout=args.timeout, graph_json=graph) if not args.no_evaluate else None
                key = f'{graph_path.stem}|p{problem}|k{num_cores}'
                if result:
                    best = result['best_result']
                    best_candidate = result['best_candidate']
                    best_configs[key] = {
                        'generator_name': best_candidate.generator_name,
                        'params': best_candidate.params,
                        'plan': best_candidate.to_plan(),
                        'makespan': best.makespan,
                        'added_copy_bytes': best.added_copy_bytes,
                        'cache_hit_rate': best.cache_hit_rate,
                    }
                    summary.append({
                        'case': graph_path.stem, 'problem': problem,
                        'num_cores': num_cores,
                        'candidate_count': result['candidate_count'],
                        'legal_candidate_count': result['legal_candidate_count'],
                        'best_generator': best_candidate.generator_name,
                        'best_params': json.dumps(best_candidate.params,
                                                  sort_keys=True),
                        'makespan': best.makespan,
                        'added_copy_bytes': best.added_copy_bytes,
                        'cache_hit_rate': best.cache_hit_rate,
                        'search_runtime': result['search_runtime'],
                    })
                    for row in result['records']:
                        row = dict(row)
                        row['case'] = graph_path.stem
                        candidates.append(row)
                    safe_key = key.replace('|', '_')
                    out_plan = Path(args.output) / 'plans' / f'{safe_key}.json'
                    out_plan.parent.mkdir(parents=True, exist_ok=True)
                    out_plan.write_text(
                        json.dumps(best_candidate.to_plan(), indent=2),
                        encoding='utf-8')
                    print(f'{key}: {best_candidate.generator_name} '
                          f'makespan={best.makespan} candidates='
                          f'{result["legal_candidate_count"]}/{result["candidate_count"]}')
                else:
                    print(f'{key}: generated only')
    write_portfolio_outputs(args.output, summary, candidates, best_configs)
    # Comparison requested for Stage 1: simple one-op baseline versus the
    # portfolio winner, grouped over all evaluated case/problem/core rows.
    simple_by_key = {
        (row['case'], row['problem'], row['num_cores']): row
        for row in candidates if row['candidate_name'] == 'simple'
        and row['legal'] and row['makespan'] is not None
    }
    comparisons = []
    for row in summary:
        key = (row['case'], row['problem'], row['num_cores'])
        baseline = simple_by_key.get(key)
        if not baseline:
            continue
        base = float(baseline['makespan'])
        best = float(row['makespan'])
        comparisons.append({
            'case': row['case'], 'problem': row['problem'],
            'num_cores': row['num_cores'],
            'baseline_makespan': int(base), 'best_makespan': int(best),
            'speedup_vs_simple': base / best if best else None,
            'makespan_improvement': (base - best) / base if base else None,
            'best_generator': row['best_generator'],
            'search_runtime': row['search_runtime'],
        })
    if comparisons:
        avg_speedup = sum(r['speedup_vs_simple'] for r in comparisons) / len(comparisons)
        avg_improvement = sum(r['makespan_improvement'] for r in comparisons) / len(comparisons)
        metrics = {
            'rows': len(comparisons),
            'average_speedup_vs_simple': avg_speedup,
            'average_makespan_improvement': avg_improvement,
            'average_search_runtime': sum(r['search_runtime'] for r in comparisons) / len(comparisons),
            'winner_count': {name: sum(r['best_generator'] == name for r in comparisons)
                             for name in sorted({r['best_generator'] for r in comparisons})},
            'comparisons': comparisons,
        }
        (Path(args.output) / 'benchmark_metrics.json').write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
        print('benchmark: rows={} avg_speedup_vs_simple={:.4f} '
              'avg_makespan_improvement={:.2%} avg_search_runtime={:.3f}s '
              'winners={}'.format(
                  metrics['rows'], avg_speedup, avg_improvement,
                  metrics['average_search_runtime'], metrics['winner_count']))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
