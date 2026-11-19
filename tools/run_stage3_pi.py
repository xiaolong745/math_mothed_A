"""Evaluate the evidence-backed fixed-P/C same-core Pi refinement."""

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

from scheduler.candidate import Candidate, candidate_signature, validate_candidate
from scheduler.evaluation.official_runner import OfficialRunner
from scheduler.evaluation.pi_attribution import attribute
from scheduler.refinement import pi_reorder_candidates


def _candidate(item):
    plan = item['plan']
    return Candidate({int(k): int(v) for k, v in plan['node_to_subgraph'].items()},
                     [[int(x) for x in row] for row in plan['core_schedules']],
                     item.get('generator_name', 'stage2_best'), item.get('params', {}))


def _read_rows(path):
    try:
        with path.open(newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--remote-root', type=Path,
                        default=ROOT / 'results/stage2_remote_parallel_20260926')
    parser.add_argument('--output', type=Path, default=ROOT / 'results/stage3')
    parser.add_argument('--cases', default='case_001,case_005')
    parser.add_argument('--cores', default='2,3,4,5')
    parser.add_argument('--problems', default='1,2,3')
    parser.add_argument('--limit', type=int, default=24)
    parser.add_argument('--radius', type=int, default=2)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    runner = OfficialRunner(ROOT, args.data_dir / 'config.txt')
    rows, calls, attributions, best_configs = [], [], [], {}
    for case in [x for x in args.cases.split(',') if x]:
        graph_path = args.data_dir / f'{case}.json'
        graph = json.loads(graph_path.read_text(encoding='utf-8'))
        from scheduler.graph_model import build_graph_model
        model = build_graph_model(graph)
        config_path = args.remote_root / case / 'best_configs.json'
        if not config_path.exists():
            continue
        configs = json.loads(config_path.read_text(encoding='utf-8'))
        stage1_rows = _read_rows(ROOT / 'results/stage1/summary.csv')
        stage1_index = {(r['case'], int(r['problem']), int(r['num_cores'])): r
                        for r in stage1_rows}
        for cores in [int(x) for x in args.cores.split(',') if x]:
            for problem in [int(x) for x in args.problems.split(',') if x]:
                key = f'{case}|p{problem}|k{cores}'
                item = configs.get(key)
                if not item:
                    continue
                incumbent = _candidate(item)
                legal, reason = validate_candidate(model, incumbent)
                if not legal:
                    rows.append({'case': case, 'problem': problem, 'cores': cores,
                                 'method': 'stage2', 'status': 'invalid',
                                 'makespan': None, 'error': reason})
                    continue
                base_started = time.perf_counter()
                try:
                    base_result = runner.evaluate(graph_path, incumbent, problem,
                                                  label='stage2_elite', timeout=120)
                    base_status, base_error = 'ok', ''
                except Exception as exc:
                    base_result, base_status, base_error = None, 'failed', str(exc)
                rows.append({
                    'case': case, 'problem': problem, 'cores': cores,
                    'method': 'stage2', 'status': base_status,
                    'makespan': base_result.makespan if base_result else None,
                    'added_copy_bytes': base_result.added_copy_bytes if base_result else None,
                    'runtime': time.perf_counter() - base_started,
                    'official_evaluated': True, 'used_historical_elite': True,
                    'pi_candidate_count': 0, 'accepted_pi_move': False,
                    'error': base_error,
                })
                if base_result is None:
                    continue
                best = (base_result.makespan, base_result.added_copy_bytes,
                        -(base_result.cache_hit_rate or 0.0) if problem == 3 else 0.0,
                        incumbent)
                variants = pi_reorder_candidates(model, incumbent,
                                                 limit=args.limit,
                                                 radius=args.radius)
                accepted_count = 0
                for index, candidate in enumerate(variants):
                    started = time.perf_counter()
                    signature = repr(candidate_signature(model, candidate))
                    try:
                        result = runner.evaluate(graph_path, candidate, problem,
                                                 label=f'pi_reorder_{index}', timeout=120)
                        status, error = 'ok', ''
                        key_result = (result.makespan, result.added_copy_bytes,
                                      -(result.cache_hit_rate or 0.0) if problem == 3 else 0.0,
                                      candidate)
                        if key_result[:3] < best[:3]:
                            attributions.append({
                                'case': case, 'problem': problem, 'cores': cores,
                                'variant': index, 'accepted': True,
                                **attribute(base_result.raw, result.raw,
                                            'stage2_elite', f'pi_reorder_{index}')})
                            best = key_result
                            accepted_count += 1
                    except Exception as exc:
                        result, status, error = None, 'failed', str(exc)
                    calls.append({
                        'case': case, 'problem': problem, 'cores': cores,
                        'method': 'pi_reorder', 'variant': index,
                        'status': status, 'runtime': time.perf_counter() - started,
                        'candidate_signature': signature,
                        'makespan': result.makespan if result else None,
                        'error': error,
                    })
                    rows.append({
                        'case': case, 'problem': problem, 'cores': cores,
                        'method': 'pi_reorder', 'variant': index,
                        'status': status,
                        'makespan': result.makespan if result else None,
                        'added_copy_bytes': result.added_copy_bytes if result else None,
                        'runtime': time.perf_counter() - started,
                        'official_evaluated': True, 'used_historical_elite': True,
                        'pi_candidate_count': len(variants),
                        'accepted_pi_move': False, 'error': error,
                    })
                best_candidate = best[3]
                best_configs[key] = {
                    'generator_name': 'stage3_pi_elite',
                    'params': {'radius': args.radius, 'limit': args.limit},
                    'plan': best_candidate.to_plan(),
                    'makespan': best[0], 'added_copy_bytes': best[1],
                }
                for row in rows:
                    if row.get('case') == case and row.get('problem') == problem \
                            and row.get('cores') == cores:
                        row['pi_candidate_count'] = len(variants)
                        row['accepted_pi_move'] = bool(accepted_count)
    fields = sorted({key for row in rows for key in row})
    with (args.output / 'summary.csv').open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    fields = sorted({key for row in calls for key in row}) if calls else ['case']
    with (args.output / 'evaluator_calls.csv').open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(calls)
    with (args.output / 'pi_attribution.csv').open('w', newline='', encoding='utf-8') as fh:
        fields = sorted({key for row in attributions for key in row}) if attributions else ['case']
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(attributions)
    (args.output / 'best_configs.json').write_text(
        json.dumps(best_configs, ensure_ascii=False, indent=2), encoding='utf-8')
    # Keep required audit files present when this focused method does not add
    # Oracle or spill observations.
    for filename in ('spill_metrics.csv',):
        path = args.output / filename
        if not path.exists(): path.write_text('', encoding='utf-8')
    print(f'wrote {len(rows)} summary rows and {len(calls)} Pi evaluator calls')


if __name__ == '__main__':
    main()
