"""Run Stage 2 bounded refinement experiments and write audit artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from .evaluation.official_runner import OfficialRunner
from .graph_model import build_graph_model
from .stage2 import Stage2Context, load_stage1_seeds, refine_seeds


def _paths(value, data_dir):
    if value:
        return [Path(item) for item in value.split(',')]
    return sorted(Path(data_dir).glob('case_[0-9][0-9][0-9].json'))


def _write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text('', encoding='utf-8')
        return
    keys = sorted({key for row in rows for key in row})
    with path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Stage 2 FM-style refinement')
    parser.add_argument('--cases')
    parser.add_argument('--data-dir', default='data')
    parser.add_argument('--stage1-dir', default='results/stage1')
    parser.add_argument('--output', default='results/stage2')
    parser.add_argument('--cores', default='2,3,4,5')
    parser.add_argument('--problems', default='1,2,3')
    parser.add_argument('--mode', choices=('strict', 'no_oracle', 'full'), default='full')
    parser.add_argument('--fast-top-k', type=int, default=20)
    parser.add_argument('--oracle-top-k', type=int, default=6)
    parser.add_argument('--max-passes', type=int, default=3)
    parser.add_argument('--max-moves', type=int, default=48)
    parser.add_argument('--max-negative-moves', type=int, default=2)
    parser.add_argument('--checkpoint-limit', type=int, default=8)
    parser.add_argument('--chain-limit', type=int, default=8)
    parser.add_argument('--max-evaluations', type=int, default=20)
    parser.add_argument('--timeout', type=float)
    parser.add_argument('--max-seconds', type=float, default=60.0,
                        help='per case/core/problem search wall-clock budget')
    parser.add_argument('--ablation', action='store_true',
                        help='run strict/no_oracle/full on the same combinations')
    args = parser.parse_args(argv)
    cases = _paths(args.cases, args.data_dir)
    cores_list = [int(value) for value in args.cores.split(',') if value]
    problems = [int(value) for value in args.problems.split(',') if value]
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    summary_rows, move_rows, spill_rows, call_rows, ablation_rows = [], [], [], [], []
    best_configs = {}
    singlecore_results = {}

    def checkpoint():
        _write_csv(output / 'summary.csv', summary_rows)
        _write_csv(output / 'move_history.csv', move_rows)
        _write_csv(output / 'spill_metrics.csv', spill_rows)
        _write_csv(output / 'evaluator_calls.csv', call_rows)
        _write_csv(output / 'ablation.csv', ablation_rows)
        (output / 'best_configs.json').write_text(
            json.dumps(best_configs, ensure_ascii=False, indent=2), encoding='utf-8')

    def run_mode(graph_path, graph, model, cores, problem, active_mode):
        started = time.perf_counter()
        context = Stage2Context(graph, graph_path, model, problem, cores,
                                output / active_mode, timeout=args.timeout)
        seeds = load_stage1_seeds(model, graph_path, problem, cores,
                                  args.stage1_dir, context)
        result = refine_seeds(
            context, seeds, mode=active_mode,
            fast_top_k=args.fast_top_k, oracle_top_k=args.oracle_top_k,
            max_passes=args.max_passes, max_moves=args.max_moves,
            max_negative_moves=args.max_negative_moves,
            checkpoint_limit=args.checkpoint_limit,
            chain_limit=args.chain_limit,
            max_evaluations=args.max_evaluations,
            max_seconds=args.max_seconds)
        elapsed = time.perf_counter() - started
        baseline = min(result['seed_results'],
                       key=lambda pair: (pair[1].makespan,
                                         pair[1].added_copy_bytes))[1]
        incumbent_result = result['incumbent_result']
        row = {
            'case': graph_path.stem, 'problem': problem, 'num_cores': cores,
            'mode': active_mode, 'seed_count': len(seeds),
            'candidate_count': result['candidate_count'],
            'selected_count': result['selected_count'],
            'unselected_count': result.get('unselected_count', 0),
            'evaluated_count': result['evaluated_count'],
            'makespan': incumbent_result.makespan,
            'singlecore_makespan': (
                singlecore_results[graph_path].makespan
                if not isinstance(singlecore_results[graph_path], Exception) else None),
            'speedup_vs_singlecore': (
                singlecore_results[graph_path].makespan / incumbent_result.makespan
                if not isinstance(singlecore_results[graph_path], Exception)
                and incumbent_result.makespan else None),
            'stage1_seed_best_makespan': baseline.makespan,
            'improvement_vs_stage1': ((baseline.makespan - incumbent_result.makespan)
                                      / baseline.makespan if baseline.makespan else 0),
            'added_copy_bytes': incumbent_result.added_copy_bytes,
            'cache_hit_rate': incumbent_result.cache_hit_rate,
            'evaluator_calls': len(context.evaluator_calls),
            'evaluator_cache_hits': sum(row.get('cache_hit', False)
                                       for row in context.evaluator_calls),
            'oracle_calls': len(context.spill_metrics),
            'oracle_cache_hits': sum(row.get('cache_hit', False)
                                    for row in context.spill_metrics),
            'search_runtime': elapsed, 'status': 'ok',
        }
        return context, result, row

    for graph_path in cases:
        graph = json.loads(graph_path.read_text(encoding='utf-8'))
        model = build_graph_model(graph)
        try:
            singlecore_results[graph_path] = OfficialRunner(
                Path(__file__).resolve().parents[1],
                graph_path.parent / 'config.txt').evaluate_singlecore(
                    graph_path, timeout=args.timeout)
        except Exception as exc:
            singlecore_results[graph_path] = exc
        for cores in cores_list:
            for problem in problems:
                modes = ('strict', 'no_oracle', 'full') if args.ablation else (args.mode,)
                for active_mode in modes:
                    try:
                        context, result, row = run_mode(
                            graph_path, graph, model, cores, problem, active_mode)
                        summary_rows.append(row)
                        for item in result['move_history']:
                            move_rows.append({**item, 'case': graph_path.stem,
                                              'problem': problem, 'num_cores': cores,
                                              'mode': active_mode})
                        for item in context.spill_metrics:
                            spill_rows.append({**item, 'case': graph_path.stem,
                                               'problem': problem, 'num_cores': cores,
                                               'mode': active_mode})
                        for item in context.evaluator_calls:
                            call_rows.append({**item, 'case': graph_path.stem,
                                              'problem': problem, 'num_cores': cores,
                                              'mode': active_mode})
                        key = f'{graph_path.stem}|p{problem}|k{cores}'
                        if active_mode == args.mode or not args.ablation:
                            incumbent = result['incumbent']
                            best_configs[key] = {
                                'generator_name': incumbent.generator_name,
                                'params': incumbent.params,
                                'plan': incumbent.to_plan(),
                                'makespan': result['incumbent_result'].makespan,
                                'added_copy_bytes': result['incumbent_result'].added_copy_bytes,
                                'cache_hit_rate': result['incumbent_result'].cache_hit_rate,
                            }
                        if args.ablation:
                            ablation_rows.append({
                                'case': graph_path.stem, 'problem': problem,
                                'num_cores': cores, 'mode': active_mode,
                                'makespan': result['incumbent_result'].makespan,
                                'added_copy_bytes': result['incumbent_result'].added_copy_bytes,
                                'evaluator_calls': len(context.evaluator_calls),
                                'oracle_calls': len(context.spill_metrics),
                                'search_runtime': row['search_runtime'],
                            })
                        print(f'{key}|{active_mode}: {row["makespan"]} '
                              f'(stage1={row["stage1_seed_best_makespan"]}, '
                              f'evaluated={row["evaluated_count"]})')
                        checkpoint()
                    except Exception as exc:
                        failed = {'case': graph_path.stem, 'problem': problem,
                                  'num_cores': cores, 'mode': active_mode,
                                  'status': 'failed', 'error': str(exc)}
                        summary_rows.append(failed)
                        if args.ablation:
                            ablation_rows.append(failed)
                        print(f'{graph_path.stem}|p{problem}|k{cores}|{active_mode}: FAILED {exc}')
                        checkpoint()
    _write_csv(output / 'summary.csv', summary_rows)
    _write_csv(output / 'move_history.csv', move_rows)
    _write_csv(output / 'spill_metrics.csv', spill_rows)
    _write_csv(output / 'evaluator_calls.csv', call_rows)
    _write_csv(output / 'ablation.csv', ablation_rows)
    (output / 'best_configs.json').write_text(
        json.dumps(best_configs, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
