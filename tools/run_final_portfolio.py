"""Run the final Stage 1 -> Stage 2 -> Stage 3-Pi portfolio.

This is deliberately a bounded integration driver.  It does not add a new
search operator: Stage 2 owns P refinement and this driver applies the
validated fixed-P/C Pi post-processing.  Historical official elites can be
fed in as independent incumbents, while every candidate entering the final
pool is evaluated by the unmodified official evaluator.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from scheduler.candidate import Candidate, candidate_signature, validate_candidate
from scheduler.evaluation.pi_attribution import attribute
from scheduler.graph_model import build_graph_model
from scheduler.refinement import pi_reorder_candidates
from scheduler.stage2 import (
    Stage2Context,
    _result_key,
    candidate_from_plan,
    load_stage1_seeds,
    refine_seeds,
)
from scheduler.evaluation.official_runner import OfficialRunner


DEFAULT_CASES = (
    'case_001,case_002,case_004,case_006,case_008,case_010,'
    'case_012,case_013,case_017,case_023'
)


def _read_csv(path: Path):
    try:
        with path.open(newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def _write_csv(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for row in rows for k in row}) if rows else ['case']
    with path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _key(case, problem, cores):
    return f'{case}|p{problem}|k{cores}'


def _cache_policy(path: Path):
    return {(r.get('case'), int(r['cores'])): r.get('status', 'cache_unknown')
            for r in _read_csv(path) if r.get('case') and r.get('cores')}


def _historical_candidates(model, key, paths):
    """Load plans only; caller still performs an official evaluation."""
    result = []
    seen = set()
    for path in paths:
        configs = _load_json(path)
        item = configs.get(key)
        if not item or not item.get('plan'):
            continue
        candidate = candidate_from_plan(item['plan'],
                                        item.get('generator_name', 'historical_elite'),
                                        item.get('params', {}))
        sig = repr(candidate_signature(model, candidate))
        if sig not in seen:
            seen.add(sig)
            result.append(candidate)
    return result


def _official_row(case, problem, cores, method, result, *, source,
                  official=True, **extra):
    return {
        'case': case, 'problem': problem, 'cores': cores,
        'method': method, 'source': source,
        'makespan': result.makespan if result else None,
        'added_copy_bytes': result.added_copy_bytes if result else None,
        'cache_hit_rate': result.cache_hit_rate if result else None,
        'official_evaluated': official,
        'prediction_only': not official,
        **extra,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument('--data-dir', type=Path, default=root / 'data')
    parser.add_argument('--stage1-dir', type=Path, default=root / 'results/stage1')
    parser.add_argument('--historical-stage2-dir', type=Path,
                        default=root / 'results/stage2_remote_parallel_20260926')
    parser.add_argument('--historical-stage3', type=Path,
                        default=root / 'results/stage3/elite_merged.json')
    parser.add_argument('--cache-list', type=Path,
                        default=root / 'results/stage3/cache_candidate_list.csv')
    parser.add_argument('--output', type=Path, default=root / 'results/final')
    parser.add_argument('--cases', default=DEFAULT_CASES)
    parser.add_argument('--cores', default='2,3,4,5')
    parser.add_argument('--problems', default='1,2,3')
    parser.add_argument('--max-seconds', type=float, default=60.0)
    parser.add_argument('--fast-top-k', type=int, default=20)
    parser.add_argument('--oracle-top-k', type=int, default=6)
    parser.add_argument('--max-evaluations', type=int, default=20)
    parser.add_argument('--pi-limit', type=int, default=6)
    parser.add_argument('--pi-radius', type=int, default=2)
    parser.add_argument('--timeout', type=float, default=300.0)
    parser.add_argument('--no-historical', action='store_true')
    args = parser.parse_args(argv)

    cases = [x for x in args.cases.split(',') if x]
    cores_list = [int(x) for x in args.cores.split(',') if x]
    problems = [int(x) for x in args.problems.split(',') if x]
    args.output.mkdir(parents=True, exist_ok=True)
    policy = _cache_policy(args.cache_list)
    stage3_path = args.historical_stage3
    historical_paths = [] if args.no_historical else [stage3_path]

    summary, calls, spill, attribution_rows = [], [], [], []
    best_configs, metrics = {}, {}
    singlecore = {}

    def checkpoint():
        _write_csv(args.output / 'summary.csv', summary)
        _write_csv(args.output / 'evaluator_calls.csv', calls)
        _write_csv(args.output / 'spill_metrics.csv', spill)
        _write_csv(args.output / 'pi_attribution.csv', attribution_rows)
        (args.output / 'best_configs.json').write_text(
            json.dumps(best_configs, ensure_ascii=False, indent=2), encoding='utf-8')
        (args.output / 'metrics.json').write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')

    stage1_summary = {(r.get('case'), int(r['problem']), int(r['num_cores'])): r
                     for r in _read_csv(args.stage1_dir / 'summary.csv')
                     if r.get('problem') and r.get('num_cores')}
    stage1_configs = _load_json(args.stage1_dir / 'best_configs.json')

    for case in cases:
        graph_path = args.data_dir / f'{case}.json'
        if not graph_path.exists():
            summary.append({'case': case, 'status': 'missing_graph',
                            'official_evaluated': False, 'prediction_only': False})
            checkpoint()
            continue
        graph = json.loads(graph_path.read_text(encoding='utf-8'))
        model = build_graph_model(graph)
        runner = OfficialRunner(root, args.data_dir / 'config.txt')
        started_case = time.perf_counter()
        try:
            singlecore[case] = runner.evaluate_singlecore(graph_path, timeout=args.timeout)
        except Exception as exc:  # keep matrix resumable
            singlecore[case] = exc
        for cores in cores_list:
            for problem in problems:
                key = _key(case, problem, cores)
                combo_started = time.perf_counter()
                # Cache-aware search is enabled only for a directly observed
                # P2/P3 improvement. Unknown combinations remain conservative.
                cache_status = policy.get((case, cores), 'cache_unknown')
                oracle_enabled = problem == 3 and cache_status == 'cache_candidate'
                mode = 'full' if oracle_enabled else 'no_oracle'
                combo_dir = args.output / 'runs' / f'{case}_p{problem}_k{cores}'
                context = Stage2Context(graph, graph_path, model, problem,
                                        cores, combo_dir, timeout=args.timeout)
                try:
                    seeds = load_stage1_seeds(model, graph_path, problem, cores,
                                              args.stage1_dir, context)
                    stage2 = refine_seeds(
                        context, seeds, mode=mode,
                        fast_top_k=args.fast_top_k,
                        oracle_top_k=args.oracle_top_k,
                        max_evaluations=args.max_evaluations,
                        max_seconds=args.max_seconds)
                    baseline = min(stage2['seed_results'],
                                   key=lambda pair: _result_key(pair[1], problem))
                    candidates = [(baseline[0], baseline[1], 'stage1_seed')]
                    candidates.append((stage2['incumbent'], stage2['incumbent_result'],
                                       'stage2'))
                    # A historical Stage 2/3 plan is a separate incumbent and
                    # never silently replaces the current Stage 1 seed.
                    historical = [] if args.no_historical else _historical_candidates(
                        model, key, [args.historical_stage2_dir / case / 'best_configs.json',
                                     args.historical_stage3])
                    for candidate in historical:
                        legal, reason = validate_candidate(model, candidate)
                        if not legal:
                            continue
                        try:
                            result = context.evaluate(candidate, phase='historical_elite')
                            candidates.append((candidate, result, 'historical_elite'))
                        except Exception:
                            continue
                    # Stage 2/Stage 1/historical rows are all official.
                    for candidate, result, source in candidates:
                        method = ('stage1' if source == 'stage1_seed' else
                                  'stage2' if source == 'stage2' else 'historical_elite')
                        summary.append(_official_row(
                            case, problem, cores, method, result, source=source,
                            cache_policy=cache_status, oracle_enabled=oracle_enabled,
                            used_historical_elite=(source == 'historical_elite'),
                            pi_candidate_count=0, accepted_pi_move=False,
                            runtime=result.runtime))
                    # Pi always starts from the best verified P/C candidate.
                    best_candidate, best_result, best_source = min(
                        candidates, key=lambda row: _result_key(row[1], problem))
                    variants = pi_reorder_candidates(
                        model, best_candidate, limit=args.pi_limit,
                        radius=args.pi_radius)
                    seen = {repr(candidate_signature(model, best_candidate))}
                    accepted = 0
                    for index, candidate in enumerate(variants):
                        signature = repr(candidate_signature(model, candidate))
                        if signature in seen:
                            continue
                        seen.add(signature)
                        started = time.perf_counter()
                        try:
                            result = context.evaluate(candidate, phase='stage3_pi')
                            status, error = 'ok', ''
                            if _result_key(result, problem) < _result_key(best_result, problem):
                                attribution_rows.append({
                                    'case': case, 'problem': problem, 'cores': cores,
                                    'variant': index, 'accepted': True,
                                    **attribute(best_result.raw, result.raw,
                                                best_source, f'pi_reorder_{index}')})
                                best_candidate, best_result, best_source = candidate, result, 'stage3_pi'
                                accepted += 1
                        except Exception as exc:
                            result, status, error = None, 'failed', str(exc)
                        calls.append({
                            'case': case, 'problem': problem, 'cores': cores,
                            'phase': 'stage3_pi', 'variant': index,
                            'status': status, 'runtime': time.perf_counter() - started,
                            'candidate_signature': signature,
                            'makespan': result.makespan if result else None,
                            'error': error,
                        })
                    final = _official_row(
                        case, problem, cores, 'final_elite', best_result,
                        source=best_source, cache_policy=cache_status,
                        oracle_enabled=oracle_enabled, used_historical_elite=(
                            best_source == 'historical_elite'),
                        pi_candidate_count=len(variants),
                        accepted_pi_move=bool(accepted), runtime=best_result.runtime)
                    s1 = stage1_summary.get((case, problem, cores))
                    final['stage1_makespan'] = int(s1['makespan']) if s1 and s1.get('makespan') else baseline[1].makespan
                    final['stage1_source'] = ('formal_stage1' if s1 else
                                              'missing_stage1_filled_seed')
                    final['relative_stage1_improvement'] = (
                        (final['stage1_makespan'] - best_result.makespan) /
                        final['stage1_makespan'] if final['stage1_makespan'] else 0.0)
                    final['stage2_makespan'] = stage2['incumbent_result'].makespan
                    final['relative_stage2_improvement'] = (
                        (stage2['incumbent_result'].makespan - best_result.makespan) /
                        stage2['incumbent_result'].makespan
                        if stage2['incumbent_result'].makespan else 0.0)
                    final['singlecore_makespan'] = (
                        singlecore[case].makespan
                        if not isinstance(singlecore.get(case), Exception) else None)
                    final['official_singlecore_speedup'] = (
                        final['singlecore_makespan'] / best_result.makespan
                        if final['singlecore_makespan'] and best_result.makespan else None)
                    final['elapsed_combo'] = time.perf_counter() - combo_started
                    summary.append(final)
                    best_configs[key] = {
                        'case': case, 'problem': problem, 'num_cores': cores,
                        'generator_name': best_candidate.generator_name,
                        'params': best_candidate.params,
                        'plan': best_candidate.to_plan(),
                        'makespan': best_result.makespan,
                        'added_copy_bytes': best_result.added_copy_bytes,
                        'cache_hit_rate': best_result.cache_hit_rate,
                        'official_evaluated': True, 'source': best_source,
                        'cache_policy': cache_status,
                    }
                    for row in context.evaluator_calls:
                        calls.append({**row, 'case': case, 'problem': problem,
                                      'cores': cores})
                    for row in context.spill_metrics:
                        spill.append({**row, 'case': case, 'problem': problem,
                                      'cores': cores})
                    metrics[key] = {
                        'elapsed_combo': final['elapsed_combo'],
                        'evaluator_calls': len(context.evaluator_calls),
                        'evaluator_cache_hits': sum(bool(r.get('cache_hit')) for r in context.evaluator_calls),
                        'oracle_calls': len(context.spill_metrics),
                        'pi_candidates': len(variants), 'accepted_pi_moves': accepted,
                        'status': 'ok', 'official_evaluated': True,
                    }
                    print(f'{key}: final={best_result.makespan} stage2={stage2["incumbent_result"].makespan} '
                          f'pi={accepted} mode={mode}')
                except Exception as exc:
                    summary.append({'case': case, 'problem': problem, 'cores': cores,
                                    'method': 'final_elite', 'status': 'failed',
                                    'error': str(exc), 'official_evaluated': False,
                                    'prediction_only': False, 'cache_policy': cache_status,
                                    'oracle_enabled': oracle_enabled})
                    metrics[key] = {'status': 'failed', 'error': str(exc),
                                    'official_evaluated': False}
                    print(f'{key}: FAILED {exc}')
                finally:
                    context.save_cache()
                    checkpoint()

    # A compact matrix-level record supports later reporting without parsing
    # per-combination cache directories.
    metrics['matrix'] = {
        'combination_count': len(cases) * len(cores_list) * len(problems),
        'completed_count': sum(1 for r in summary if r.get('method') == 'final_elite'
                               and r.get('status') != 'failed'),
        'official_evaluator_calls': sum(1 for r in calls
                                        if r.get('status') == 'ok' and not r.get('cache_hit')),
        'cache_policy_source': str(args.cache_list),
    }
    checkpoint()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
