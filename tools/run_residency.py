"""Opt-in official residency experiment, parallel over independent combinations.

python3 -m tools.run_residency --cases case_001,case_005 --cores 2,3,4,5
Each worker checkpoints isolated results; no writable evaluator state is shared.
Historical plans are re-evaluated before they become incumbents.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from scheduler.candidate import Candidate, candidate_signature, validate_candidate
from scheduler.evaluation.official_runner import OfficialRunner
from scheduler.graph_model import build_graph_model
from scheduler.residency import (tensor_pages, macro_graph,
                                  build_residency_candidates)


ROOT = Path(__file__).resolve().parents[1]


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def csvwrite(path, rows):
    fields = sorted({k for r in rows for k in r}) or ['status']
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def rank(result, problem):
    return (result['makespan'], result['added_copy_bytes'],
            -(result.get('cache_hit_rate') or 0) if problem == 3 else 0)


def result_dict(result):
    movement = result.raw.get('data_movement_bytes', {})
    cache = result.raw.get('cache_stats', {})
    return {'makespan': result.makespan, 'added_copy_bytes': result.added_copy_bytes,
            'spill_added_copy_bytes': movement.get('spill_added_copy_bytes'),
            'cache_hit_rate': result.cache_hit_rate,
            'cache_hit_bytes': cache.get('hit_bytes'),
            'cache_accesses': cache.get('accesses'),
            'runtime': result.runtime, 'official_evaluated': True,
            'prediction_only': False}


def baseline_items(case, key):
    sources = [
        ('stage1', ROOT / 'results/stage1/best_configs.json'),
        ('stage2', ROOT / f'results/stage2_remote_parallel_20260926/{case}/best_configs.json'),
        ('stage2', ROOT / 'results/stage2_remote_20260926/best_configs.json'),
        ('elite', ROOT / 'results/final_frozen_20260927/best_configs.json'),
        ('elite', ROOT / 'results/reseed_pair_20260927/elite_merged.json'),
        ('elite', ROOT / 'results/stage3/openproject_best_configs.json'),
        ('elite', ROOT / 'results/stage3/elite_merged.json')]
    items = {}
    for category, path in sources:
        item = read(path).get(key)
        if item and item.get('plan') and item.get('makespan') is not None:
            if category not in items or rank(item, int(key.split('|')[1][1:])) < rank(
                    items[category], int(key.split('|')[1][1:])):
                items[category] = {**item, 'provenance_path': str(path.relative_to(ROOT))}
    return items


def run_combo(task):
    case, cores, problem, out, timeout, manifest_id, singlecore = task
    key = f'{case}|p{problem}|k{cores}'
    combo = out / 'runs' / key.replace('|', '_')
    combo.mkdir(parents=True, exist_ok=True)
    old = read(combo / 'checkpoint.json')
    if old.get('complete') and old.get('manifest_id') == manifest_id:
        return old
    started = time.perf_counter()
    cpu_start = os.times()
    graph_path = ROOT / 'data' / f'{case}.json'
    raw = read(graph_path)
    model = build_graph_model(raw)
    pages = tensor_pages(raw)
    # Read exact official config via its existing read-only interface.
    sys.path.insert(0, str(ROOT / 'code'))
    from evaluation_validation import read_required_settings
    config = ROOT / 'data/config.txt'
    capacity = read_required_settings(config, 'capacity', ('L1', 'UB'))
    bandwidth = read_required_settings(config, 'bandwidth', ('bandwidth',))['bandwidth']
    runner = OfficialRunner(ROOT, config)
    calls, candidates, memo = [], [], {}
    state = {'key': key, 'manifest_id': manifest_id, 'complete': False,
             'calls': calls, 'ablation': candidates}

    def checkpoint():
        dump(combo / 'checkpoint.json', state)

    def evaluate(label, candidate, historical=None):
        sig = hashlib.sha256(repr(candidate_signature(model, candidate)).encode()).hexdigest()
        begin = time.perf_counter()
        row = {'case': case, 'cores': cores, 'problem': problem, 'method': label,
               'signature': sig, 'cache_hit': sig in memo, 'oracle_calls': 0,
               'official_called_this_row': False}
        dump(combo / f'{label}_plan.json', candidate.to_plan())
        try:
            legal, reason = validate_candidate(model, candidate)
            if not legal:
                raise ValueError(reason)
            if sig not in memo:
                row['official_called_this_row'] = True
                res = runner.evaluate(graph_path, candidate, problem, timeout=timeout)
                memo[sig] = result_dict(res)
                dump(combo / f'{label}_official.json', res.raw)
            row.update(memo[sig])
            row['status'] = 'ok'
            row['official_called_this_row'] = not row['cache_hit']
            if historical:
                row['historical_makespan'] = historical['makespan']
                row['historical_matches'] = historical['makespan'] == row['makespan']
                row['provenance_path'] = historical['provenance_path']
            candidates.append(dict(row))
        except Exception as exc:
            row.update(status='failed', error=str(exc), official_evaluated=False,
                       prediction_only=False)
        row['elapsed_s'] = time.perf_counter() - begin
        calls.append(row)
        checkpoint()
        return row if row['status'] == 'ok' else None

    pool = []
    baseline = baseline_items(case, key)
    if not baseline:
        state.update(complete=False, error='missing historical elite plan')
        checkpoint()
        return state
    for category, item in baseline.items():
        p = item['plan']
        c = Candidate({int(n): int(g) for n, g in p['node_to_subgraph'].items()},
                      p['core_schedules'], item.get('generator_name', category),
                      item.get('params', {}))
        row = evaluate(category, c, item)
        if row:
            pool.append((row, c))
    if len(pool) != len(baseline) or any(r.get('historical_matches') is False for r, c in pool):
        state.update(complete=False, error='baseline failed or changed; diagnose before promotion')
        checkpoint()
        return state
    base_row, base = min(pool, key=lambda pair: rank(pair[0], problem))
    elapsed_baselines = time.perf_counter() - started
    generated_at = time.perf_counter()
    variants = list(build_residency_candidates(model, pages, base, problem, capacity, bandwidth))
    generation_s = time.perf_counter() - generated_at
    for label, candidate in variants:
        records, nets, *_ = macro_graph(model, candidate.node_to_subgraph, pages)
        dump(combo / f'{label}_features.json', {
            'macro_records': records, 'tensor_nets': nets, 'metadata': candidate.metadata,
            'proxy_only': True, 'actual_spill_peak_and_cache_hits': None})
        row = evaluate(label, candidate)
        if row:
            pool.append((row, candidate))
    best_row, best = min(pool, key=lambda pair: rank(pair[0], problem))
    new_rows = [row for row in candidates if row['method'] not in baseline]
    cpu_end = os.times()
    summary = {'case': case, 'cores': cores, 'problem': problem,
               'stage1_makespan': next((r['makespan'] for r, _ in pool
                                        if r['method'] == 'stage1'), None),
               'stage2_makespan': next((r['makespan'] for r, _ in pool
                                        if r['method'] == 'stage2'), None),
               'previous_elite_makespan': base_row['makespan'],
               'new_method_makespan': min((r['makespan'] for r in new_rows), default=None),
               'final_makespan': best_row['makespan'], 'method': best_row['method'],
               'improvement_vs_elite': 1 - best_row['makespan'] / base_row['makespan'],
               'singlecore_makespan': singlecore,
               'official_singlecore_speedup': singlecore / best_row['makespan'] if singlecore else None,
               'added_copy_bytes': best_row['added_copy_bytes'],
               'spill_added_copy_bytes': best_row['spill_added_copy_bytes'],
               'cache_hit_rate': best_row['cache_hit_rate'],
               'evaluator_calls': sum(r.get('official_called_this_row',
                                        r['status'] == 'failed') for r in calls),
               'cache_hits': sum(r['cache_hit'] for r in calls), 'oracle_calls': 0,
               'failed_candidates': sum(r['status'] != 'ok' for r in calls),
               'baseline_seconds': elapsed_baselines, 'generation_seconds': generation_s,
               'wall_seconds': time.perf_counter() - started,
               'cpu_seconds': sum(cpu_end[:4]) - sum(cpu_start[:4]),
               'official_evaluated': True, 'prediction_only': False, 'new_run': True}
    assert summary['final_makespan'] <= summary['previous_elite_makespan']
    state.update(complete=True, summary=summary, best={
        'plan': best.to_plan(), **{k: v for k, v in best_row.items() if k != 'signature'},
        'generator_name': best.generator_name, 'params': best.params})
    checkpoint()
    return state


def audit(cases, out):
    overview = []
    for case in cases:
        raw = read(ROOT / 'data' / f'{case}.json')
        pages = tensor_pages(raw)
        rows = [{'tensor_id': t, 'size': p.size, 'bank': p.bank,
                 'compute_consumers': len(p.consumers), 'raw_consumers': len(p.raw_consumers),
                 'producer_ids': sorted(p.producers), 'consumer_ids': sorted(p.consumers),
                 'input_only': p.input_only, 'logical_key': p.logical_key}
                for t, p in pages.items()]
        csvwrite(out / 'reuse_tables' / f'{case}.csv', rows)
        shared = [p for p in pages.values() if len(p.consumers) > 1]
        raw_shared = [p for p in pages.values() if len(p.raw_consumers) > 1]
        overview.append({'case': case, 'shared_compute_tensors': len(shared),
                         'shared_compute_references': sum(len(p.consumers) for p in shared),
                         'shared_raw_tensors': len(raw_shared),
                         'shared_raw_references': sum(len(p.raw_consumers) for p in raw_shared),
                         'shared_input_tensors': sum(p.input_only for p in shared)})
    csvwrite(out / 'reuse_audit.csv', overview)
    return overview


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cases', default='case_001,case_005')
    ap.add_argument('--cores', default='2,3,4,5')
    ap.add_argument('--problems', default='1,2,3')
    ap.add_argument('--workers', type=int, default=12)
    ap.add_argument('--timeout', type=float, default=180)
    ap.add_argument('--output', type=Path, default=ROOT / 'results/residency_20260927')
    ap.add_argument('--audit-only', action='store_true')
    args = ap.parse_args()
    cases = args.cases.split(',')
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    audit(cases, out)
    if args.audit_only:
        return
    inputs = [*ROOT.joinpath('code').glob('*.py'), ROOT / 'data/config.txt',
              *ROOT.joinpath('scheduler').rglob('*.py'), Path(__file__),
              *[ROOT / 'data' / f'{c}.json' for c in cases],
              *[p for pattern in ('best_configs.json', 'elite_merged.json',
                                   'openproject_best_configs.json')
                for p in ROOT.joinpath('results').rglob(pattern)
                if 'runs' not in p.parts and not p.is_relative_to(out)]]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in inputs}
    manifest_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    dump(out / 'manifest.json', {'id': manifest_id, 'hashes': hashes,
                                'python': sys.version, 'workers': args.workers})
    baseline_runner = OfficialRunner()
    singles = {}
    for case in cases:
        try:
            res = baseline_runner.evaluate_singlecore(ROOT / 'data' / f'{case}.json',
                                                       timeout=args.timeout)
            singles[case] = res.makespan
            dump(out / f'{case}_singlecore_official.json', res.raw)
        except Exception as exc:
            raise RuntimeError(f'singlecore failed for {case}: {exc}') from exc
    tasks = [(c, int(k), int(p), out, args.timeout, manifest_id, singles[c])
             for c in cases for k in args.cores.split(',') for p in args.problems.split(',')]
    results = []
    start = time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_combo, task): task for task in tasks}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                task = futures[future]
                result = {'key': f'{task[0]}|p{task[2]}|k{task[1]}',
                          'complete': False, 'error': str(exc)}
            results.append(result)
            print(result.get('summary', {'key': result['key'], 'error': result.get('error')}), flush=True)
            csvwrite(out / 'summary.csv', [r['summary'] for r in results if r.get('complete')])
            csvwrite(out / 'evaluator_calls.csv', [c for r in results for c in r.get('calls', [])])
            csvwrite(out / 'pcpi_ablation.csv', [c for r in results for c in r.get('ablation', [])])
            dump(out / 'best_configs.json', {r['key']: r['best'] for r in results if r.get('complete')})
            dump(out / 'failures.json', [r for r in results if not r.get('complete')])
    dump(out / 'timing.json', {'parallel_wall_seconds': time.perf_counter() - start,
                               'complete': sum(r.get('complete', False) for r in results),
                               'requested': len(tasks), 'singlecore_calls': len(singles)})


if __name__ == '__main__':
    main()
