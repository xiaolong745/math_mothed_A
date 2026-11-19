"""Stage 2 candidate diversity and controlled P/C/Pi audit.

This module deliberately does not change the refinement or official evaluator
semantics.  It materializes the candidates produced by the existing bounded
search, records the three layers of a plan separately, and creates a small
controlled set where only one layer changes at a time.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from .candidate import Candidate, candidate_signature, partition_signature, validate_candidate
from .graph_model import build_graph_model
from .refinement import (_same_core_reorders, _quotient_acyclic,
                         _rebuild_candidates, partition_metrics,
                         refine_candidate)
from .schedulers.heft_lite import assign_cores
from .stage2 import Stage2Context, load_stage1_seeds


def _core_assignment(model, candidate):
    normalized = {}
    groups = {}
    for node, group in candidate.node_to_subgraph.items():
        groups.setdefault(group, []).append(node)
    # Use the canonical partition id so a relabeling is not reported as a
    # different core assignment.
    p = partition_signature(model, candidate.node_to_subgraph)
    group_for_nodes = {tuple(sorted(nodes)): i for i, nodes in enumerate(p)}
    for core, order in enumerate(candidate.core_schedules):
        for group in order:
            members = tuple(sorted(groups.get(group, ())))
            normalized[group_for_nodes[members]] = core
    return tuple(sorted(normalized.items()))


def _schedule_signature(model, candidate):
    groups = {}
    for node, group in candidate.node_to_subgraph.items():
        groups.setdefault(group, []).append(node)
    p = partition_signature(model, candidate.node_to_subgraph)
    group_for_nodes = {tuple(sorted(nodes)): i for i, nodes in enumerate(p)}
    result = []
    for order in candidate.core_schedules:
        result.append(tuple(group_for_nodes[tuple(sorted(groups[g]))] for g in order))
    return tuple(result)


def _mv_loads(model, candidate):
    core_of = {group: core for core, order in enumerate(candidate.core_schedules)
               for group in order}
    loads = [[0, 0] for _ in candidate.core_schedules]
    for node, group in candidate.node_to_subgraph.items():
        core = core_of[group]
        loads[core][0] += model.ops[node].pipe_m_cycles
        loads[core][1] += model.ops[node].pipe_v_cycles
    return loads


def candidate_record(model, candidate, source='refinement'):
    legal, reason = validate_candidate(model, candidate)
    metrics = partition_metrics(model, candidate.node_to_subgraph, candidate)
    p_sig = partition_signature(model, candidate.node_to_subgraph)
    c_sig = _core_assignment(model, candidate)
    pi_sig = _schedule_signature(model, candidate)
    return {
        'source': source,
        'generator_name': candidate.generator_name,
        'params': json.dumps(candidate.params, sort_keys=True),
        'legal': legal,
        'illegal_reason': reason,
        'partition_signature': repr(p_sig),
        'core_assignment_signature': repr(c_sig),
        'core_schedule_signature': repr(pi_sig),
        'candidate_signature': repr(candidate_signature(model, candidate)),
        'subgraph_count': len(set(candidate.node_to_subgraph.values())),
        'unique_cut_bytes': metrics['unique_cut_bytes'],
        'estimated_copy_bytes': metrics['estimated_copy_bytes'],
        'cut_pairs': metrics['cut_pairs'],
        'cross_core_pairs': metrics['cross_core_pairs'],
        'sync_events_proxy': metrics['sync_events_proxy'],
        'mv_balance': metrics['mv_balance'],
        'mv_loads': json.dumps(_mv_loads(model, candidate)),
        'candidate': candidate,
    }


def _change_one_op(model, base, node, target):
    mapping = dict(base.node_to_subgraph)
    mapping[node] = target
    if not _quotient_acyclic(model, mapping):
        return None
    trial = Candidate(mapping, [list(x) for x in base.core_schedules],
                      'audit_p_only', {'node': node, 'target': target}, {})
    return trial if validate_candidate(model, trial)[0] else None


def _change_one_core(model, base, group, target_core):
    """Move one whole subgraph while preserving all other order sequences."""
    schedules = [list(x) for x in base.core_schedules]
    source = next((core for core, order in enumerate(schedules) if group in order), None)
    if source is None or source == target_core:
        return None
    schedules[source].remove(group)
    # Insert at the earliest position that preserves local quotient edges.  A
    # later insertion is tried as well so this remains a small C-only probe.
    for index in range(len(schedules[target_core]) + 1):
        trial_schedules = [list(x) for x in schedules]
        trial_schedules[target_core].insert(index, group)
        trial = Candidate(dict(base.node_to_subgraph), trial_schedules,
                          'audit_c_only', {'group': group, 'target_core': target_core}, {})
        if validate_candidate(model, trial)[0]:
            return trial
    return None


def controlled_variants(model, base, limit=12):
    """Return baseline plus fixed-P, fixed-C and fixed-Pi probes.

    The probes are intentionally small.  They are diagnostic candidates, not
    an additional search operator.
    """
    result = [candidate_record(model, base, 'controlled_baseline')]
    seen = {result[0]['candidate_signature']}
    per_kind = max(1, (limit - 1) // 3)
    # P only: move a boundary op between existing groups; schedules stay byte
    # for byte identical, so only partition P changes.
    for node in model.topo_order:
        source = base.node_to_subgraph[node]
        # If this move empties the source group, the schedule would also need
        # relabeling; that is a P+C/Pi change and is intentionally excluded
        # from the P-only probe.
        if sum(group == source for group in base.node_to_subgraph.values()) < 2:
            continue
        targets = {base.node_to_subgraph[p] for p in model.ops[node].predecessors}
        targets.update(base.node_to_subgraph[s] for s in model.ops[node].successors)
        for target in sorted(targets):
            if target == source:
                continue
            trial = _change_one_op(model, base, node, target)
            if trial is not None:
                record = candidate_record(model, trial, 'controlled_P_only')
                if record['candidate_signature'] not in seen:
                    seen.add(record['candidate_signature'])
                    result.append(record)
                    break
        if len([r for r in result if r['source'] == 'controlled_P_only']) >= per_kind:
            return result
    # Pi only: legal adjacent same-core orders with fixed P and C.
    for trial in _same_core_reorders(model, base, limit=limit):
        record = candidate_record(model, trial, 'controlled_Pi_only')
        if record['candidate_signature'] not in seen:
            seen.add(record['candidate_signature'])
            result.append(record)
        if len([r for r in result if r['source'] == 'controlled_Pi_only']) >= per_kind:
            break
    # C only: move a whole group and preserve each core's existing order.
    groups = sorted(set(base.node_to_subgraph.values()))
    for group in groups:
        for core in range(len(base.core_schedules)):
            trial = _change_one_core(model, base, group, core)
            if trial is not None:
                record = candidate_record(model, trial, 'controlled_C_only')
                if record['candidate_signature'] not in seen:
                    seen.add(record['candidate_signature'])
                    result.append(record)
                break
        if len([r for r in result if r['source'] == 'controlled_C_only']) >= per_kind:
            break
    return result


def audit_one(graph_path, problem, cores, stage1_dir, output_dir,
              max_passes=3, max_moves=12, checkpoint_limit=12,
              chain_limit=4, max_candidates=40, evaluate=False,
              evaluate_controlled=False):
    graph_path = Path(graph_path)
    graph = json.loads(graph_path.read_text(encoding='utf-8'))
    model = build_graph_model(graph)
    context = Stage2Context(graph, graph_path, model, problem, cores,
                            Path(output_dir))
    seeds = load_stage1_seeds(model, graph_path, problem, cores, stage1_dir, context)
    records = []
    seen = set()
    for seed in seeds:
        refined = refine_candidate(model, seed, cores,
                                   scene='A' if problem == 1 else 'B',
                                   max_passes=max_passes, max_moves=max_moves,
                                   max_negative_moves=2,
                                   checkpoint_limit=checkpoint_limit,
                                   preference='communication', enable_chains=True,
                                   chain_limit=chain_limit, problem=problem)
        for candidate in [seed] + refined['candidates']:
            record = candidate_record(model, candidate, 'seed' if candidate is seed else 'refinement')
            if record['candidate_signature'] not in seen:
                seen.add(record['candidate_signature'])
                records.append(record)
            if len(records) >= max_candidates:
                break
        if len(records) >= max_candidates:
            break
    # Probe every retained seed.  RegionGrow can have singleton boundary
    # groups while BandComp often exposes the useful P-only moves, so using
    # only the first seed would hide that distinction.
    for seed in seeds:
        records.extend(controlled_variants(model, seed, limit=max_candidates))
    unique = {}
    for record in records:
        unique.setdefault(record['candidate_signature'], record)
    records = list(unique.values())
    for record in records:
        should_evaluate = (evaluate and record['legal'] and
                           (not evaluate_controlled or
                            record['source'].startswith('controlled_') or
                            record['source'] == 'seed'))
        if should_evaluate:
            candidate = record.pop('candidate')
            try:
                result = context.evaluate(candidate, phase='audit')
                record.update({'makespan': result.makespan,
                               'added_copy_bytes': result.added_copy_bytes,
                               'cache_hit_rate': result.cache_hit_rate,
                               'evaluation_status': 'ok'})
            except Exception as exc:
                record.update({'makespan': None, 'added_copy_bytes': None,
                               'cache_hit_rate': None,
                               'evaluation_status': f'failed: {exc}'})
        else:
            record.pop('candidate', None)
            record.update({'makespan': None, 'added_copy_bytes': None,
                           'cache_hit_rate': None, 'evaluation_status': 'not_run'})
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f'{graph_path.stem}_p{problem}_k{cores}.csv'
    keys = sorted({key for row in records for key in row})
    with path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=keys, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(records)
    context.save_cache()
    return path, records


def main(argv=None):
    parser = argparse.ArgumentParser(description='Audit Stage 2 candidate diversity')
    parser.add_argument('--cases', default='data/case_001.json')
    parser.add_argument('--stage1-dir', default='results/stage1')
    parser.add_argument('--output', default='results/stage2/audit')
    parser.add_argument('--problem', type=int, default=2)
    parser.add_argument('--cores', type=int, default=4)
    parser.add_argument('--max-candidates', type=int, default=40)
    parser.add_argument('--evaluate', action='store_true')
    parser.add_argument('--evaluate-controlled', action='store_true',
                        help='evaluate only baseline and one-variable probes')
    args = parser.parse_args(argv)
    summaries = []
    for item in args.cases.split(','):
        path, records = audit_one(item, args.problem, args.cores,
                                   args.stage1_dir, args.output,
                                   max_candidates=args.max_candidates,
                                   evaluate=args.evaluate,
                                   evaluate_controlled=args.evaluate_controlled)
        p = len({row['partition_signature'] for row in records})
        c = len({row['core_assignment_signature'] for row in records})
        pi = len({row['core_schedule_signature'] for row in records})
        summaries.append({'case': Path(item).stem, 'problem': args.problem,
                          'num_cores': args.cores, 'candidate_count': len(records),
                          'partition_count': p, 'core_assignment_count': c,
                          'core_schedule_count': pi,
                          'evaluated_count': sum(row['evaluation_status'] == 'ok'
                                                 for row in records)})
        print(f'{path}: candidates={len(records)} P={p} C={c} Pi={pi}')
    if summaries:
        output = Path(args.output) / 'summary.csv'
        with output.open('w', newline='', encoding='utf-8') as fh:
            writer = csv.DictWriter(fh, fieldnames=list(summaries[0]))
            writer.writeheader()
            writer.writerows(summaries)


if __name__ == '__main__':
    main()
