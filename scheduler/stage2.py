"""Stage 2 budgeted refinement orchestration."""

from __future__ import annotations

import ast
import csv
import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from .candidate import Candidate, candidate_signature, validate_candidate
from .evaluation.cache import EvaluationCache
from .evaluation.official_runner import EvaluationResult, OfficialRunner
from .evaluation.oracle import SpillOracle
from .evaluation.trace_diagnoser import diagnose
from .refinement import partition_metrics, refine_candidate
from .search.portfolio import build_candidates


def _key(case, problem, cores):
    return f'{case}|p{problem}|k{cores}'


def _parse_params(value):
    try:
        result = ast.literal_eval(value)
        return result if isinstance(result, dict) else {}
    except (ValueError, SyntaxError):
        return {}


def candidate_from_plan(plan, name, params=None):
    return Candidate(
        {int(node): int(group) for node, group in plan['node_to_subgraph'].items()},
        [[int(group) for group in order] for order in plan['core_schedules']],
        name, dict(params or {}), {})


class Stage2Context:
    def __init__(self, graph_json, graph_path, model, problem, cores,
                 output_dir, config_path=None, timeout=None):
        self.graph_json = graph_json
        self.graph_path = Path(graph_path)
        self.model = model
        self.problem = problem
        self.cores = cores
        self.output_dir = Path(output_dir)
        self.config_path = Path(config_path or self.graph_path.parent / 'config.txt')
        self.timeout = timeout
        project_root = Path(__file__).resolve().parents[1]
        self.runner = OfficialRunner(project_root, self.config_path)
        self.oracle = SpillOracle(self.runner.root, self.config_path)
        evaluator_files = [self.runner.root / 'code' / f'multicore_cut_evaluate_problem_{problem}.py',
                           self.runner.root / 'code' / 'schedule_step1.py',
                           self.runner.root / 'code' / 'schedule_step2.py',
                           self.runner.root / 'code' / 'schedule_step3.py']
        version_hash = hashlib.sha256()
        for path in evaluator_files:
            version_hash.update(path.read_bytes())
        self.cache = EvaluationCache(
            self.output_dir / 'cache' / f'{self.graph_path.stem}_p{problem}_k{cores}.json',
            graph_json, self.config_path, version=f'stage2-v1-{version_hash.hexdigest()}')
        self.evaluator_calls = []
        self.spill_metrics = []

    def _result_from_cache(self, item, candidate):
        return EvaluationResult(
            makespan=int(item['makespan']),
            added_copy_bytes=int(item['added_copy_bytes']),
            cache_hit_rate=item.get('cache_hit_rate'),
            runtime=float(item.get('runtime', 0.0)),
            candidate_name=candidate.generator_name,
            params=dict(candidate.params), raw=item.get('raw', {}))

    def evaluate(self, candidate, phase='refinement'):
        legal, reason = validate_candidate(self.model, candidate)
        if not legal:
            raise ValueError(f'illegal candidate: {reason}')
        key = self.cache.key(self.model, candidate, self.problem, self.cores)
        cached = self.cache.get(key)
        if cached is not None:
            result = self._result_from_cache(cached, candidate)
            diagnosis = diagnose(result.raw)
            self.evaluator_calls.append({
                'phase': phase, 'cache_hit': True, 'status': 'cached',
                'candidate_signature': repr(candidate_signature(self.model, candidate)),
                'makespan': result.makespan, 'added_copy_bytes': result.added_copy_bytes,
                'runtime': 0.0, 'trace_diagnosis': diagnosis,
            })
            return result
        started = time.perf_counter()
        try:
            result = self.runner.evaluate(
                self.graph_path, candidate, self.problem,
                label=candidate.generator_name, timeout=self.timeout)
            status = 'ok'
            error = ''
        except Exception as exc:
            self.evaluator_calls.append({
                'phase': phase, 'cache_hit': False, 'status': 'failed',
                'candidate_signature': repr(candidate_signature(self.model, candidate)),
                'makespan': None, 'added_copy_bytes': None,
                'runtime': time.perf_counter() - started, 'error': str(exc),
            })
            raise
        self.cache.put(key, {
            'makespan': result.makespan,
            'added_copy_bytes': result.added_copy_bytes,
            'cache_hit_rate': result.cache_hit_rate,
            'runtime': result.runtime,
            'raw': result.raw,
        })
        self.evaluator_calls.append({
            'phase': phase, 'cache_hit': False, 'status': status,
            'candidate_signature': repr(candidate_signature(self.model, candidate)),
            'makespan': result.makespan, 'added_copy_bytes': result.added_copy_bytes,
            'runtime': time.perf_counter() - started,
            'trace_diagnosis': diagnose(result.raw),
        })
        return result

    def inspect_oracle(self, candidate, phase='oracle'):
        key = self.cache.key(self.model, candidate, self.problem, self.cores, 'oracle')
        cached = self.cache.get(key)
        if cached is not None:
            self.spill_metrics.append({**cached, 'phase': phase, 'cache_hit': True})
            return cached
        result = self.oracle.inspect(self.graph_json, candidate, self.problem)
        payload = dict(result)
        self.cache.put(key, payload)
        self.spill_metrics.append({**payload, 'phase': phase, 'cache_hit': False})
        return payload

    def save_cache(self):
        self.cache.save()


def _result_key(result, problem):
    return (result.makespan, result.added_copy_bytes,
            -(result.cache_hit_rate or 0.0) if problem == 3 else 0.0)


def _proxy(model, candidate):
    metrics = partition_metrics(model, candidate.node_to_subgraph, candidate)
    loads = {}
    core_of = {sg: core for core, order in enumerate(candidate.core_schedules)
               for sg in order}
    for node, sg in candidate.node_to_subgraph.items():
        core = core_of.get(sg, 0)
        m, v = loads.setdefault(core, [0, 0])
        loads[core] = [m + model.ops[node].pipe_m_cycles,
                       v + model.ops[node].pipe_v_cycles]
    max_work = max((max(m, v) for m, v in loads.values()), default=0)
    predicted = (max(max_work, metrics['mv_balance'])
                 + metrics['estimated_copy_bytes'] / 60.0
                 + metrics['cross_core_pairs'] * 500.0)
    return predicted, metrics


def _diverse_sort(model, candidates, limit):
    chosen = []
    seen = set()
    for candidate in sorted(candidates,
                            key=lambda c: (_proxy(model, c)[0],
                                           repr(candidate_signature(model, c)))):
        signature = candidate_signature(model, candidate)
        if signature in seen:
            continue
        seen.add(signature)
        chosen.append(candidate)
        if len(chosen) >= limit:
            break
    return chosen


def refine_seeds(context, seeds, mode='full', fast_top_k=20,
                 oracle_top_k=6, max_passes=3, max_moves=48,
                 max_negative_moves=2, checkpoint_limit=8,
                 chain_limit=8, max_evaluations=20, max_seconds=None):
    """Refine up to three seeds, returning an incumbent and audit records."""
    # Stage 1 global best is the incumbent and is never replaced by a proxy.
    seed_results = []
    for seed in seeds:
        try:
            result = context.evaluate(seed, phase='stage1_seed')
            seed_results.append((seed, result))
        except Exception:
            continue
    if not seed_results:
        raise RuntimeError('no legal/evaluable Stage 1 seed')
    incumbent, incumbent_result = min(
        seed_results, key=lambda pair: _result_key(pair[1], context.problem))
    all_candidates = []
    move_history = []
    deadline = time.perf_counter() + max_seconds if max_seconds else None
    effective_max_moves = 1 if mode == 'strict' else max_moves
    for seed, seed_result in seed_results:
        for preference in ('communication', 'balance', 'hybrid'):
            refined = refine_candidate(
                context.model, seed, context.cores,
                scene='A' if context.problem == 1 else 'B',
                evaluator=None, max_passes=max_passes,
                max_moves=effective_max_moves, max_negative_moves=max_negative_moves,
                checkpoint_limit=checkpoint_limit, preference=preference,
                enable_chains=(mode != 'strict'), chain_limit=chain_limit,
                mode='strict' if mode == 'strict' else 'full', deadline=deadline,
                problem=context.problem)
            for row in refined['history']:
                row = dict(row)
                row['preference'] = preference
                row['seed'] = seed.generator_name
                move_history.append(row)
            all_candidates.extend(refined['candidates'])
            if deadline is not None and time.perf_counter() >= deadline:
                break
        if deadline is not None and time.perf_counter() >= deadline:
            break
    all_candidates = _diverse_sort(context.model, all_candidates, fast_top_k)
    if mode == 'strict':
        selected = all_candidates[:min(fast_top_k, max_evaluations)]
    elif mode == 'no_oracle':
        selected = all_candidates[:min(fast_top_k, max_evaluations)]
    else:
        oracle_rows = []
        for candidate in all_candidates[:fast_top_k]:
            oracle_result = context.inspect_oracle(candidate)
            predicted, metrics = _proxy(context.model, candidate)
            oracle_rows.append((candidate, oracle_result, predicted, metrics))
        # Keep Pareto-like extremes: best predicted, least spill, and low
        # communication.  This avoids a fixed spill threshold.
        ranked = []
        for key_index in (2, 1):
            ranked.extend(sorted(oracle_rows,
                                 key=lambda row: (row[key_index] if key_index == 2
                                                  else (row[1].get('total_spill_bytes')
                                                        if row[1].get('valid') else float('inf'))))[:oracle_top_k])
        selected = []
        seen = set()
        for row in ranked:
            candidate = row[0]
            sig = candidate_signature(context.model, candidate)
            if sig not in seen:
                seen.add(sig)
                selected.append(candidate)
            if len(selected) >= min(oracle_top_k, max_evaluations):
                break
    evaluated = []
    for candidate in selected:
        if len([row for row in context.evaluator_calls
                if row.get('phase') == 'refinement'
                and not row.get('cache_hit') and row.get('status') == 'ok']) >= max_evaluations:
            break
        try:
            result = context.evaluate(candidate, phase='refinement')
        except Exception as exc:
            move_history.append({'status': 'evaluation_failed', 'error': str(exc),
                                 'candidate_signature': repr(candidate_signature(context.model, candidate))})
            continue
        evaluated.append((candidate, result))
        move_history.append({
            'status': 'checkpoint_evaluated',
            'candidate_signature': repr(candidate_signature(context.model, candidate)),
            'makespan': result.makespan, 'added_copy_bytes': result.added_copy_bytes,
            'cache_hit_rate': result.cache_hit_rate,
        })
        if _result_key(result, context.problem) < _result_key(incumbent_result, context.problem):
            incumbent, incumbent_result = candidate, result
    context.save_cache()
    return {
        'incumbent': incumbent,
        'incumbent_result': incumbent_result,
        'seed_results': seed_results,
        'evaluated': evaluated,
        'move_history': move_history,
        'candidate_count': len(all_candidates),
        'selected_count': len(selected),
        'unselected_count': max(0, len(all_candidates) - len(selected)),
        'evaluated_count': len(evaluated),
    }


def load_stage1_seeds(model, graph_path, problem, cores, stage1_dir,
                      context):
    """Load existing global/RG/Band seeds, or fill missing combinations."""
    stage1_dir = Path(stage1_dir)
    case = Path(graph_path).stem
    key = _key(case, problem, cores)
    results_file = stage1_dir / 'candidate_results.csv'
    summary_file = stage1_dir / 'summary.csv'
    best_file = stage1_dir / 'best_configs.json'
    rows = []
    if results_file.exists():
        with results_file.open(newline='', encoding='utf-8') as fh:
            rows = [row for row in csv.DictReader(fh)
                    if row.get('case') == case and int(row.get('problem', -1)) == problem
                    and int(row.get('num_cores', -1)) == cores
                    and row.get('legal') == 'True']
    configs = {}
    if best_file.exists():
        configs = json.loads(best_file.read_text(encoding='utf-8'))
    seeds = []
    global_config = configs.get(key)
    if global_config:
        seeds.append(candidate_from_plan(global_config['plan'],
                                         global_config.get('generator_name', 'stage1_global'),
                                         global_config.get('params', {})))
    stage1_candidates = build_candidates(model, cores,
                                         'A' if problem == 1 else 'B', 12)
    for name in ('region_grow', 'bandcomp'):
        candidates = [candidate for candidate in stage1_candidates
                      if candidate.generator_name == name]
        matches = [row for row in rows if row.get('candidate_name') == name
                   and row.get('makespan')]
        if matches:
            best_row = min(matches, key=lambda row: (int(row['makespan']),
                                                       int(row.get('added_copy_bytes') or 0)))
            params = _parse_params(best_row.get('params', '{}'))
            candidates = [candidate for candidate in candidates
                          if candidate.params == params] or candidates
        if matches:
            # The candidate factory is deterministic for a parameter set; use
            # the exact recorded Stage 1 winner rather than the first member
            # of the current portfolio.
            exact = [candidate for candidate in candidates
                     if candidate.params == params]
            if exact:
                seeds.append(exact[0])
        elif candidates:
            seeds.append(candidates[0])
    # Missing prior Stage 1 combinations: evaluate the current Stage 1
    # portfolio once, then retain its global/RG/Band winners as seeds.
    if not rows or not global_config:
        evaluated = []
        for candidate in stage1_candidates:
            try:
                evaluated.append((candidate, context.evaluate(candidate, phase='stage1_fill')))
            except Exception:
                continue
        if evaluated:
            global_best = min(evaluated,
                              key=lambda pair: _result_key(pair[1], problem))
            seeds.insert(0, global_best[0])
            for name in ('region_grow', 'bandcomp'):
                same = [pair for pair in evaluated if pair[0].generator_name == name]
                if same:
                    seeds.append(min(same, key=lambda pair: _result_key(pair[1], problem))[0])
    # Deduplicate full candidate signatures while preserving global first.
    result, seen = [], set()
    for seed in seeds:
        sig = candidate_signature(model, seed)
        if sig not in seen:
            seen.add(sig)
            result.append(seed)
    return result[:3]
