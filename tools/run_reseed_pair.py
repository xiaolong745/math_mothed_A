"""Run a reseed-only RegionGrow ablation with resumable official evaluation.

The two candidates in a pair share every construction and scheduling
parameter.  Only ``reseed`` changes.  Results are written after every
combination so a timeout or interrupted batch cannot erase an incumbent.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from scheduler.candidate import candidate_signature, validate_candidate
from scheduler.evaluation.official_runner import OfficialRunner
from scheduler.generators.region_grow import generate
from scheduler.graph_model import build_graph_model
from scheduler.refinement import partition_metrics


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for row in rows for k in row}) if rows else ['case']
    with path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def _params(model, cores, reseed):
    target = min(64, max(4, len(model.ops) // (cores * 4)))
    return {
        'max_ops_per_sg': target,
        'depth_window': 0,
        'communication_weight': 1.0,
        'critical_weight': 0.01,
        'edge_mode': 'bytes',
        'balance': 'max',
        'size_slack': 0,
        'reseed': bool(reseed),
    }


def _metrics(model, candidate):
    metrics = partition_metrics(model, candidate.node_to_subgraph, candidate)
    core_of = {sg: core for core, order in enumerate(candidate.core_schedules)
               for sg in order}
    loads = [[0, 0] for _ in candidate.core_schedules]
    for node, sg in candidate.node_to_subgraph.items():
        core = core_of.get(sg)
        if core is None:
            continue
        loads[core][0] += model.ops[node].pipe_m_cycles
        loads[core][1] += model.ops[node].pipe_v_cycles
    metrics.update({
        'core_load_m': [x[0] for x in loads],
        'core_load_v': [x[1] for x in loads],
        'subgraph_count': len(set(candidate.node_to_subgraph.values())),
    })
    return metrics


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser()
    ap.add_argument('--data-dir', type=Path, default=root / 'data')
    ap.add_argument('--output', type=Path,
                    default=root / 'results/reseed_pair_20260927')
    ap.add_argument('--cases', default=','.join(f'case_{i:03d}' for i in range(1, 11)))
    ap.add_argument('--cores', default='2,3,4,5')
    ap.add_argument('--problems', default='1')
    ap.add_argument('--timeout', type=float, default=300.0)
    ap.add_argument('--limit', type=int, default=None)
    args = ap.parse_args(argv)
    cases = [x for x in args.cases.split(',') if x]
    cores_list = [int(x) for x in args.cores.split(',') if x]
    problems = [int(x) for x in args.problems.split(',') if x]
    args.output.mkdir(parents=True, exist_ok=True)
    pair_rows, diag_rows, calls, failures = [], [], [], []
    best_configs = {}
    runner = OfficialRunner(root, args.data_dir / 'config.txt')

    def checkpoint():
        _write(args.output / 'pair_summary.csv', pair_rows)
        _write(args.output / 'construction_diagnostics.csv', diag_rows)
        _write(args.output / 'evaluator_calls.csv', calls)
        _write(args.output / 'failures.csv', failures)
        _json(args.output / 'best_configs.json', best_configs)

    count = 0
    for case in cases:
        graph_path = args.data_dir / f'{case}.json'
        if not graph_path.exists():
            failures.append({'case': case, 'status': 'missing_graph'})
            checkpoint()
            continue
        try:
            graph_json = json.loads(graph_path.read_text(encoding='utf-8'))
            model = build_graph_model(graph_json)
        except Exception as exc:
            failures.append({'case': case, 'status': 'model_failed', 'error': str(exc)})
            checkpoint()
            continue
        for cores in cores_list:
            for problem in problems:
                if args.limit is not None and count >= args.limit:
                    checkpoint()
                    return
                combo = f'{case}|p{problem}|k{cores}'
                combo_dir = args.output / 'runs' / f'{case}_p{problem}_k{cores}'
                combo_dir.mkdir(parents=True, exist_ok=True)
                results = {}
                candidates = {}
                for reseed in (False, True):
                    label = 'reseed_true' if reseed else 'reseed_false'
                    started = time.perf_counter()
                    try:
                        candidate = generate(model, cores, 'A' if problem == 1 else 'B', _params(model, cores, reseed))
                        gen_time = time.perf_counter() - started
                        construction = dict(candidate.metadata.get('construction_diagnostics', {}))
                        generated_time = construction.get('candidate_generation_seconds', gen_time)
                        heft_time = construction.get('heft_schedule_seconds', None)
                        legal_started = time.perf_counter()
                        legal, reason = validate_candidate(model, candidate)
                        if not legal:
                            raise ValueError(f'illegal generated candidate: {reason}')
                        legal_time = time.perf_counter() - legal_started
                        candidates[label] = candidate
                        candidate_path = combo_dir / f'{label}_plan.json'
                        candidate_path.write_text(json.dumps(candidate.to_plan(), indent=2), encoding='utf-8')
                        incumbent_path = combo_dir / 'incumbent.json'
                        eval_started = time.perf_counter()
                        result = runner.evaluate(graph_path, candidate, problem,
                                                 label=label, timeout=args.timeout)
                        eval_time = time.perf_counter() - eval_started
                        results[label] = result
                        calls.append({
                            'case': case, 'problem': problem, 'cores': cores,
                            'variant': label, 'status': 'ok', 'cache_hit': False,
                            'generation_runtime': gen_time, 'evaluator_runtime': eval_time,
                            'makespan': result.makespan,
                            'added_copy_bytes': result.added_copy_bytes,
                        })
                        # Persist the first verified incumbent before evaluating the
                        # second member of the pair.
                        current = json.loads(incumbent_path.read_text(encoding='utf-8')) if incumbent_path.exists() else None
                        if current is None or (result.makespan, result.added_copy_bytes) < (current['makespan'], current['added_copy_bytes']):
                            _json(incumbent_path, {
                                'case': case, 'problem': problem, 'cores': cores,
                                'source': label, 'official_evaluated': True,
                                'makespan': result.makespan,
                                'added_copy_bytes': result.added_copy_bytes,
                                'plan': candidate.to_plan(), 'params': candidate.params,
                            })
                        dm = _metrics(model, candidate)
                        diag_rows.append({
                            'case': case, 'problem': problem, 'cores': cores,
                            'variant': label, 'status': 'ok',
                            'frontier_exhaustion_count': construction.get('frontier_exhaustion_count'),
                            'reseed_count': construction.get('reseed_count'),
                            'fallback_seed_op_count': construction.get('fallback_seed_op_count'),
                            'fallback_seed_op_ratio': construction.get('fallback_seed_op_ratio'),
                            'frontier_events': json.dumps(construction.get('frontier_events', []), ensure_ascii=False),
                            'subgraph_count': dm.get('subgraph_count'),
                            'subgraph_loads': json.dumps(construction.get('subgraph_loads', []), ensure_ascii=False),
                            'core_load_m': json.dumps(dm.get('core_load_m', [])),
                            'core_load_v': json.dumps(dm.get('core_load_v', [])),
                            'estimated_copy_bytes': dm.get('estimated_copy_bytes'),
                            'cross_core_pairs': dm.get('cross_core_pairs'),
                            'official_added_copy_bytes': result.added_copy_bytes,
                            'generation_runtime': gen_time,
                            'candidate_generation_seconds': generated_time,
                            'heft_schedule_seconds': heft_time,
                            'quotient_legal_check_seconds': legal_time,
                            'evaluator_runtime': eval_time,
                            'candidate_signature': repr(candidate_signature(model, candidate)),
                        })
                    except Exception as exc:
                        failures.append({'case': case, 'problem': problem, 'cores': cores,
                                         'variant': label, 'status': 'failed',
                                         'error': str(exc),
                                         'runtime': time.perf_counter() - started})
                        calls.append({'case': case, 'problem': problem, 'cores': cores,
                                      'variant': label, 'status': 'failed',
                                      'cache_hit': False,
                                      'generation_runtime': None,
                                      'evaluator_runtime': time.perf_counter() - started,
                                      'makespan': None, 'added_copy_bytes': None,
                                      'error': str(exc)})
                    checkpoint()
                false = results.get('reseed_false')
                true = results.get('reseed_true')
                row = {
                    'case': case, 'problem': problem, 'cores': cores,
                    'status': 'complete' if false and true else 'incomplete',
                    'reseed_false_makespan': false.makespan if false else None,
                    'reseed_true_makespan': true.makespan if true else None,
                    'reseed_false_added_copy_bytes': false.added_copy_bytes if false else None,
                    'reseed_true_added_copy_bytes': true.added_copy_bytes if true else None,
                    'makespan_delta_false_minus_true': (false.makespan - true.makespan) if false and true else None,
                    'official_evaluated': bool(false and true),
                    'search_truncated': False,
                    'no_verified_solution': not bool(false or true),
                }
                pair_rows.append(row)
                if results:
                    best_label, best_result = min(results.items(), key=lambda item: (item[1].makespan, item[1].added_copy_bytes))
                    best_configs[combo] = {
                        'source': best_label, 'official_evaluated': True,
                        'makespan': best_result.makespan,
                        'added_copy_bytes': best_result.added_copy_bytes,
                        'plan': candidates[best_label].to_plan(),
                        'params': candidates[best_label].params,
                    }
                count += 1
                checkpoint()
    
    
if __name__ == '__main__':
    main()
