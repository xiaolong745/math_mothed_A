"""Stage 1 candidate portfolio and official-evaluator selection."""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from ..candidate import Candidate, candidate_signature, validate_candidate
from ..evaluation.official_runner import OfficialRunner
from ..evaluation.validator import validate_plan
from ..generators import (generate_band_comp, generate_region_grow,
                           generate_simple, generate_topological_segment)


def _unique(candidates):
    seen = set()
    for candidate in candidates:
        signature = (
            tuple(sorted(candidate.node_to_subgraph.items())),
            tuple(tuple(order) for order in candidate.core_schedules),
        )
        if signature not in seen:
            seen.add(signature)
            yield candidate


def build_candidates(graph, num_cores, scene='A', max_candidates=12):
    """Generate a scale-adaptive, deterministic Stage 1 portfolio."""
    n = len(graph.ops)
    candidates = [generate_simple(graph, num_cores, scene)]
    def add(factory, params):
        try:
            candidates.append(factory(graph, num_cores, scene, params))
        except (ValueError, KeyError, TypeError):
            # Non-convex region growth can contract a legal Op-DAG into a
            # cyclic quotient.  The candidate is invalid by design and is
            # discarded; the evaluator must never see a repaired graph.
            return
    # Keep the local growth kernel bounded on the largest official graphs;
    # Stage 1 is a portfolio baseline, so thousands of node candidates are
    # screened by the evaluator rather than spending quadratic time in one
    # oversized region.
    target = min(64, max(4, n // max(1, num_cores * 4)))
    region_sizes = sorted({max(2, target), min(128, target * 2)})
    for size in region_sizes:
        add(generate_region_grow,
            {'max_ops_per_sg': size, 'depth_window': 0,
             'communication_weight': 1.0, 'critical_weight': 0.01})
    # Reference-inspired reseeding candidate.  It is kept alongside the
    # original RegionGrow so Stage 1's established baseline remains intact.
    if len(candidates) < max_candidates:
        add(generate_region_grow,
            {'max_ops_per_sg': max(2, target), 'depth_window': 0,
             'communication_weight': 1.0, 'critical_weight': 0.01,
             'reseed': True, 'edge_mode': 'bytes', 'balance': 'mv'})
    add(generate_topological_segment, {'segment_size': max(2, target)})
    max_depth = max((op.depth_from_source for op in graph.ops.values()), default=0)
    widths = [1, 2, 4, 8]
    if max_depth > 64:
        widths += [16, 32]
    elif max_depth < 8:
        widths = [1, 2, 4]
    for width in widths:
        if len(candidates) >= max_candidates:
            break
        add(generate_band_comp,
            {'band_width': width, 'max_ops_per_sg': max(0, target * 4)})
    # A bounded wavefront variant uses the same quotient construction but
    # adds predecessor sticky placement.  One scale is enough for the
    # portfolio; wider scans belong in an experiment budget, not the default.
    if len(candidates) < max_candidates:
        add(generate_band_comp,
            {'band_width': 4, 'max_ops_per_sg': max(0, target * 4),
             'sticky': 1.0, 'wavefront': True})
    return list(_unique(candidates))[:max_candidates]


def evaluate_portfolio(graph, graph_path, num_cores, problem,
                       max_candidates=12, runner=None, timeout=None,
                       graph_json=None, surrogate=None,
                       surrogate_top_k=None, surrogate_exploration=2,
                       surrogate_pool_size=None):
    scene = 'A' if problem == 1 else 'B'
    runner = runner or OfficialRunner()
    pool_limit = surrogate_pool_size or max_candidates
    candidates = build_candidates(graph, num_cores, scene, pool_limit)
    surrogate_scores = {}
    if surrogate is not None:
        ranked = surrogate.rank(
            candidates, graph, problem, num_cores,
            top_k=(surrogate_top_k or max_candidates),
            exploration=surrogate_exploration)
        candidates = [item[2] for item in ranked]
        surrogate_scores = {
            repr(candidate_signature(graph, item[2])): {
                'predicted_makespan': item[0],
                'lower_bound': item[1],
            } for item in ranked
        }
    records = []
    best = None
    started = time.perf_counter()
    for index, candidate in enumerate(candidates):
        legal, reason = validate_candidate(graph, candidate)
        if legal:
            legal, reason = validate_plan(
                graph_json=(graph_json if graph_json is not None else graph),
                candidate=candidate)
        record = {
            'candidate_name': candidate.generator_name,
            'params': candidate.params,
            'num_cores': num_cores,
            'problem': problem,
            'legal': bool(legal),
            'reason': reason if not legal else '',
            'makespan': None,
            'added_copy_bytes': None,
            'cache_hit_rate': None,
            'runtime': None,
            'surrogate_predicted_makespan': surrogate_scores.get(
                repr(candidate_signature(graph, candidate)), {}).get(
                    'predicted_makespan'),
            'surrogate_lower_bound': surrogate_scores.get(
                repr(candidate_signature(graph, candidate)), {}).get(
                    'lower_bound'),
        }
        if legal:
            result = runner.evaluate(
                graph_path, candidate, problem,
                label=f'{candidate.generator_name}_{index}', timeout=timeout)
            record.update({
                'makespan': result.makespan,
                'added_copy_bytes': result.added_copy_bytes,
                'cache_hit_rate': result.cache_hit_rate,
                'runtime': result.runtime,
            })
            key = (result.makespan, result.added_copy_bytes,
                   -(result.cache_hit_rate or 0.0))
            if best is None or key < best[0]:
                best = (key, candidate, result)
        records.append(record)
    if best is None:
        raise RuntimeError('no legal candidate in portfolio')
    return {
        'best_candidate': best[1],
        'best_result': best[2],
        'records': records,
        'candidate_count': len(candidates),
        'legal_candidate_count': sum(row['legal'] for row in records),
        'search_runtime': time.perf_counter() - started,
    }


def write_portfolio_outputs(path, summary_rows, candidate_rows, best_configs):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    import csv
    for filename, rows in (('summary.csv', summary_rows),
                           ('candidate_results.csv', candidate_rows)):
        if not rows:
            continue
        keys = sorted({key for row in rows for key in row})
        with (path / filename).open('w', newline='', encoding='utf-8') as fh:
            writer = csv.DictWriter(fh, fieldnames=keys)
            writer.writeheader()
            writer.writerows(rows)
    (path / 'best_configs.json').write_text(
        json.dumps(best_configs, ensure_ascii=False, indent=2), encoding='utf-8')
