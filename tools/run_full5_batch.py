"""Frozen-algorithm five-core sweep. Orchestration/checkpointing only.

Run from an isolated extracted paper snapshot plus all 100 input graphs.
Unit subprocesses isolate state; parent timeouts kill the entire owned group.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'results/full5_20260927'
PILOT = ['case_019', 'case_029', 'case_048', 'case_070',
         'case_018', 'case_036', 'case_043', 'case_075',
         'case_030', 'case_041', 'case_085', 'case_091']
POLICY = {'cores': 5, 'max_seconds': 60, 'fast_top_k': 20, 'oracle_top_k': 6,
          'max_evaluations': 20, 'pi_limit': 6, 'pi_radius': 2,
          'evaluator_timeout': 300, 'cell_timeout': 900,
          'baseline_timeout': 330, 'workers': 12, 'process_memory_gib': 16}


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def write(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cell_path(case, problem):
    return OUT / 'cells' / f'{case}_p{problem}_k5'


def event(path, obj):
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(obj, ensure_ascii=False) + '\n')


def unit(kind, case, problem):
    limit = POLICY['process_memory_gib'] * 1024**3
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    from scheduler.evaluation.official_runner import OfficialRunner, EvaluationResult
    if kind == 'baseline':
        result = OfficialRunner().evaluate_singlecore(ROOT / 'data' / f'{case}.json',
                                                       timeout=POLICY['evaluator_timeout'])
        write(OUT / 'baselines' / f'{case}.json', asdict(result))
        return
    import tools.run_final_portfolio as frozen
    from scheduler.stage2 import Stage2Context
    from scheduler.candidate import candidate_signature
    directory = cell_path(case, problem)
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    incumbent = None

    class BaselineRunner(OfficialRunner):
        def evaluate_singlecore(self, graph_path, timeout=None):
            saved = read(OUT / 'baselines' / f'{case}.json')
            if not saved:
                raise RuntimeError('shared official singlecore unavailable; see baseline status')
            return EvaluationResult(**saved)

    class CheckpointContext(Stage2Context):
        def evaluate(self, candidate, phase='refinement'):
            nonlocal incumbent
            signature = hashlib.sha256(repr(candidate_signature(
                self.model, candidate)).encode()).hexdigest()
            begin = time.monotonic()
            event(directory / 'events.jsonl', {'event': 'start', 'phase': phase,
                  'signature': signature, 't': time.monotonic()-started})
            try:
                result = super().evaluate(candidate, phase)
            except Exception as exc:
                event(directory / 'events.jsonl', {'event': 'failed', 'phase': phase,
                    'signature': signature, 'runtime': time.monotonic()-begin,
                    'error': str(exc), 'exception_type': type(exc).__name__})
                raise
            cached = self.evaluator_calls[-1].get('cache_hit', False)
            event(directory / 'events.jsonl', {'event': 'ok', 'phase': phase,
                'signature': signature, 'runtime': time.monotonic()-begin,
                'cache_hit': cached, 'makespan': result.makespan,
                'added_copy_bytes': result.added_copy_bytes})
            value = (result.makespan, result.added_copy_bytes,
                     -(result.cache_hit_rate or 0) if problem == 3 else 0)
            if incumbent is None or value < incumbent:
                incumbent = value
                saved = {'case': case, 'problem': problem, 'cores': 5,
                    'makespan': result.makespan, 'added_copy_bytes': result.added_copy_bytes,
                    'cache_hit_rate': result.cache_hit_rate,
                    'spill_added_copy_bytes': result.raw.get('data_movement_bytes', {}).get('spill_added_copy_bytes'),
                    'plan': candidate.to_plan(), 'generator_name': candidate.generator_name,
                    'params': candidate.params, 'phase': phase,
                    'time_to_best': time.monotonic()-started,
                    'official_evaluated': True, 'prediction_only': False}
                write(directory / 'verified_incumbent.json', saved)
                # Only best results retained here; the frozen context still
                # owns its existing cache and end-of-combination persistence.
                write(directory / 'verified_incumbent_official.json', result.raw)
            return result

        def inspect_oracle(self, candidate, phase='oracle'):
            begin = time.monotonic()
            try:
                result = super().inspect_oracle(candidate, phase)
                event(directory / 'events.jsonl', {'event': 'oracle',
                    'runtime': time.monotonic()-begin, 'valid': result.get('valid'),
                    'cache_hit': self.spill_metrics[-1].get('cache_hit', False)})
                return result
            except Exception as exc:
                event(directory / 'events.jsonl', {'event': 'oracle_failed', 'error': str(exc)})
                raise

    # Instrument the frozen integration boundary without changing any search
    # operation, candidate pool, score, order, or acceptance rule.
    frozen.OfficialRunner = BaselineRunner
    frozen.Stage2Context = CheckpointContext
    argv = ['--cases', case, '--cores', '5', '--problems', str(problem),
            '--output', str(directory), '--historical-stage3', str(OUT / 'historical_best.json'),
            '--cache-list', str(ROOT / 'results/stage3/cache_candidate_list.csv')]
    for flag, key in [('--max-seconds','max_seconds'),('--fast-top-k','fast_top_k'),
                       ('--oracle-top-k','oracle_top_k'),('--max-evaluations','max_evaluations'),
                       ('--pi-limit','pi_limit'),('--pi-radius','pi_radius'),
                       ('--timeout','evaluator_timeout')]:
        argv += [flag, str(POLICY[key])]
    frozen.main(argv)


def launch(kind, case, problem=0):
    directory = OUT / 'baseline_status' / case if kind == 'baseline' else cell_path(case, problem)
    directory.mkdir(parents=True, exist_ok=True)
    status_file = directory / 'process.json'
    old = read(status_file)
    if old.get('status') in ('exited', 'timeout', 'launch_failed'):
        return old  # resume does not resample completed/failed experiments
    started = time.time()
    command = [sys.executable, '-m', 'tools.run_full5_batch', '--unit', kind,
               '--case', case, '--problem', str(problem)]
    record = {'case': case, 'problem': problem, 'unit': kind, 'status': 'running',
              'started_at': started, 'command': command}
    write(status_file, record)
    timeout = POLICY['baseline_timeout' if kind == 'baseline' else 'cell_timeout']
    with (directory / 'process.log').open('w') as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log,
                                   start_new_session=True)
        record['pid'] = process.pid
        write(status_file, record)
        try:
            _, code, usage = os.wait4(process.pid, os.WNOHANG)
            deadline = time.monotonic() + timeout
            while _ == 0 and time.monotonic() < deadline:
                time.sleep(0.5)
                _, code, usage = os.wait4(process.pid, os.WNOHANG)
            if _ == 0:
                os.killpg(process.pid, signal.SIGTERM)
                time.sleep(0.2)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _, code, usage = os.wait4(process.pid, 0)
                record['status'] = 'timeout'
            else:
                record['status'] = 'exited'
            process.returncode = os.waitstatus_to_exitcode(code)
            record.update(exit_code=process.returncode, wall_seconds=time.time()-started,
                          cpu_seconds=usage.ru_utime+usage.ru_stime, max_rss_kib=usage.ru_maxrss)
        except Exception as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            record.update(status='launch_failed', error=str(exc))
    write(status_file, record)
    return record


def prepare():
    OUT.mkdir(parents=True, exist_ok=True)
    hashes = {str(p.relative_to(ROOT)): digest(p) for folder in ('scheduler', 'code')
              for p in (ROOT / folder).rglob('*.py')}
    for p in [ROOT/'tools/run_final_portfolio.py', Path(__file__),
              ROOT/'tools/report_full5_batch.py', ROOT/'data/config.txt',
              ROOT/'results/stage3/cache_candidate_list.csv',
              *sorted((ROOT/'data').glob('case_[0-9][0-9][0-9].json'))]:
        hashes[str(p.relative_to(ROOT))] = digest(p)
    # Historical plans are supplied as independent incumbents, re-evaluated
    # by the frozen driver before final selection, never accepted by prediction.
    historical = {}
    source_paths = [ROOT/'results/final_frozen_20260927/best_configs.json',
                    ROOT/'results/reseed_pair_20260927/elite_merged.json',
                    ROOT/'results/residency_20260927/best_configs.json',
                    ROOT/'results/research_summary_20260927/best_configs_5core.json']
    for path in source_paths:
        hashes[str(path.relative_to(ROOT))] = digest(path)
        for key, item in read(path).items():
            if not key.endswith('k5') or not item.get('plan'):
                continue
            if key not in historical or item['makespan'] < historical[key]['makespan']:
                historical[key] = {**item, 'historical_source': str(path.relative_to(ROOT))}
    manifest = {'policy': POLICY, 'hashes': hashes, 'pilot': PILOT,
                'pilot_selection': 'four per size class, shallow/deep DAGs; no result-based selection',
                'requested_cases': 100, 'requested_cells': 300,
                'python': sys.version, 'no_algorithm_changes': True}
    previous = read(OUT/'manifest.json')
    if previous and previous != manifest:
        raise RuntimeError('Manifest changed: new output version required')
    write(OUT/'manifest.json', manifest)
    write(OUT/'historical_best.json', historical)


def batch(cases, phase):
    from tools.report_full5_batch import report
    write(OUT/'state.json', {'phase': phase, 'cases': cases, 'policy': POLICY,
                            'updated_at': time.time()})
    with ThreadPoolExecutor(max_workers=POLICY['workers']) as pool:
        futures = [pool.submit(launch, 'baseline', c) for c in cases]
        for future in as_completed(futures):
            print(json.dumps(future.result()), flush=True)
            report(ROOT, OUT)
        futures = [pool.submit(launch, 'cell', c, p) for c in cases for p in (1,2,3)]
        for future in as_completed(futures):
            print(json.dumps(future.result()), flush=True)
            report(ROOT, OUT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--unit', choices=('baseline', 'cell'))
    ap.add_argument('--case')
    ap.add_argument('--problem', type=int, default=0)
    args = ap.parse_args()
    if args.unit:
        unit(args.unit, args.case, args.problem)
        return
    prepare()
    batch(PILOT, 'pilot')
    processes = [read(cell_path(c,p)/'process.json') for c in PILOT for p in (1,2,3)]
    resource_failures = [r for r in processes if r.get('exit_code') not in (0, -15, -9)
                         and r.get('status') != 'timeout']
    # Timing out an individual graph is a measured limit, not resource failure.
    # Do not gate on speedup or tune parameters from pilot Makespans.
    gate = {'resource_failures': resource_failures,
            'timed_out_cells': sum(r.get('status')=='timeout' for r in processes),
            'max_rss_kib': max((r.get('max_rss_kib',0) for r in processes), default=0),
            'passed': not resource_failures and any(
                (cell_path(c,p)/'verified_incumbent.json').exists() for c in PILOT for p in (1,2,3))}
    write(OUT/'pilot_gate.json', gate)
    if not gate['passed']:
        write(OUT/'state.json', {'phase': 'stopped_at_resource_gate', 'gate': gate})
        return
    remaining = [f'case_{i:03d}' for i in range(1,101) if f'case_{i:03d}' not in PILOT]
    batch(remaining, 'full')
    write(OUT/'state.json', {'phase': 'complete', 'ended_at': time.time(), 'requested_cells':300})
    from tools.report_full5_batch import report
    report(ROOT, OUT)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
