"""Budgeted Stage 2 FM-style partition refinement.

The search state is a partition.  Scheduling is rebuilt at each checkpoint so
that an accepted result always has a complete, legal ``P,C,Pi`` candidate.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .candidate import (Candidate, canonicalize_mapping, candidate_signature,
                        partition_signature, validate_candidate)
from .schedulers.heft_lite import assign_cores


@dataclass
class Move:
    op_id: int
    source: int
    target: int
    gain: float
    preference: str
    step: int


def partition_metrics(model, mapping, candidate=None):
    """Return distinct Tensor cut and communication proxy features.

    ``unique_cut_bytes`` counts each split Tensor once.  ``estimated_copy``
    counts a Tensor once per producer-core to consumer-core pair, matching the
    evaluator's cross-core fan-out semantics more closely without claiming to
    be its exact Makespan model.
    """
    groups = defaultdict(set)
    for node, group in mapping.items():
        groups[group].add(node)
    tensor_nodes = defaultdict(lambda: {'producers': set(), 'consumers': set(),
                                         'size': 0, 'pos': None})
    for tid in set(model.tensor_producers) | set(model.tensor_consumers):
        item = tensor_nodes[tid]
        item['producers'].update(n for n in model.tensor_producers.get(tid, ())
                                 if n in model.ops)
        item['consumers'].update(n for n in model.tensor_consumers.get(tid, ())
                                 if n in model.ops)
        tensor = model.tensor_by_id.get(tid, {})
        item['size'] = int(tensor.get('size', 0))
        item['pos'] = tensor.get('pos')
    split_tensors = 0
    unique_cut_bytes = 0
    estimated_copy_bytes = 0
    target_fanout = 0
    cut_pairs = 0
    for item in tensor_nodes.values():
        producer_groups = {mapping[n] for n in item['producers']}
        consumer_groups = {mapping[n] for n in item['consumers']}
        touched = producer_groups | consumer_groups
        if len(touched) <= 1:
            continue
        split_tensors += 1
        size = item['size']
        unique_cut_bytes += size
        target_fanout += len(consumer_groups)
        cut_pairs += len({(mapping[src], mapping[dst])
                           for src in item['producers']
                           for dst in item['consumers']
                           if mapping[src] != mapping[dst]})
        estimated_copy_bytes += size * max(1, len(consumer_groups))
    if candidate is not None:
        core_of = {group: core for core, order in enumerate(candidate.core_schedules)
                   for group in order}
        loads = defaultdict(lambda: [0, 0])
        for node, group in mapping.items():
            core = core_of.get(group, 0)
            loads[core][0] += model.ops[node].pipe_m_cycles
            loads[core][1] += model.ops[node].pipe_v_cycles
        mv_balance = max((max(m, v) for m, v in loads.values()), default=0)
    else:
        mv_balance = 0
    # Cross-core/task boundaries are a separate signal from unique bytes.  A
    # tensor can be split once but feed several target cores, so both values
    # are retained for problem-specific scoring.
    cross_core_pairs = 0
    if candidate is not None:
        core_of = {group: core for core, order in enumerate(candidate.core_schedules)
                   for group in order}
        for item in tensor_nodes.values():
            producer_cores = {core_of.get(mapping[n]) for n in item['producers']}
            consumer_cores = {core_of.get(mapping[n]) for n in item['consumers']}
            producer_cores.discard(None)
            consumer_cores.discard(None)
            cross_core_pairs += sum(1 for src in producer_cores
                                    for dst in consumer_cores if src != dst)
    return {
        'split_tensors': split_tensors,
        'unique_cut_bytes': unique_cut_bytes,
        'estimated_copy_bytes': estimated_copy_bytes,
        'target_fanout': target_fanout,
        'cut_pairs': cut_pairs,
        'mv_balance': mv_balance,
        'sync_events_proxy': cut_pairs,
        'cross_core_pairs': cross_core_pairs,
    }


def boundary_ops(model, mapping):
    result = []
    for node in model.topo_order:
        group = mapping[node]
        neighbors = {mapping[p] for p in model.ops[node].predecessors}
        neighbors.update(mapping[s] for s in model.ops[node].successors)
        neighbors.discard(group)
        if neighbors:
            result.append((node, tuple(sorted(neighbors))))
    return result


def _normalized_gain(before, after, preference, problem=2):
    # P1 is dominated by Task boundaries, P2 by cross-core communication and
    # M/V balance, and P3 adds a small reuse-oriented signal.  These are proxy
    # preferences only; official Makespan decides acceptance.
    scales = {
        'communication': ('unique_cut_bytes', 'estimated_copy_bytes',
                          'cross_core_pairs'),
        'balance': ('mv_balance',),
        'hybrid': ('unique_cut_bytes', 'estimated_copy_bytes', 'mv_balance'),
    }
    if problem == 1:
        scales['communication'] = ('unique_cut_bytes', 'estimated_copy_bytes',
                                   'sync_events_proxy')
        scales['hybrid'] = ('unique_cut_bytes', 'estimated_copy_bytes',
                            'mv_balance', 'sync_events_proxy')
    elif problem == 3:
        scales['communication'] = ('estimated_copy_bytes', 'cross_core_pairs',
                                   'unique_cut_bytes')
    fields = scales[preference]
    # Lower proxy cost is better.  Normalize by a nonzero local scale so bytes
    # do not swamp the M/V term and zero-work graphs remain deterministic.
    gain = 0.0
    weights = {'unique_cut_bytes': 0.42, 'estimated_copy_bytes': 0.24,
               'mv_balance': 0.24, 'cross_core_pairs': 0.10,
               'sync_events_proxy': 0.10}
    for field in fields:
        scale = max(1.0, float(before.get(field, 0)), float(after.get(field, 0)))
        gain += weights[field] * (before.get(field, 0) - after.get(field, 0)) / scale
    return gain


def _quotient_acyclic(model, mapping):
    groups = set(mapping.values())
    succs = {group: set() for group in groups}
    indegree = {group: 0 for group in groups}
    for edge in model.edges:
        src, dst = mapping[edge.src], mapping[edge.dst]
        if src != dst and dst not in succs[src]:
            succs[src].add(dst)
            indegree[dst] += 1
    ready = sorted(group for group, degree in indegree.items() if degree == 0)
    seen = 0
    while ready:
        group = ready.pop(0)
        seen += 1
        for nxt in sorted(succs[group]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    return seen == len(groups)


def _preserve_schedule(model, mapping, base_candidate):
    """Keep core placement/order while removing empty groups and repairing IDs."""
    normalized = canonicalize_mapping(model, mapping)
    first_node = {}
    for node, group in mapping.items():
        first_node.setdefault(group, node)
    old_to_new = {old: normalized[node] for old, node in first_node.items()}
    core_of = {}
    for order in base_candidate.core_schedules:
        core = len(core_of)
        for group in order:
            if group in old_to_new:
                core_of[old_to_new[group]] = core
    groups = set(normalized.values())
    preds = {group: set() for group in groups}
    succs = {group: set() for group in groups}
    for edge in model.edges:
        src, dst = normalized[edge.src], normalized[edge.dst]
        if src != dst:
            preds[dst].add(src)
            succs[src].add(dst)
    schedules = []
    for core, old_order in enumerate(base_candidate.core_schedules):
        local = {old_to_new[group] for group in old_order if group in old_to_new}
        priority = {old_to_new[group]: i for i, group in enumerate(old_order)
                    if group in old_to_new}
        indegree = {group: len(preds[group] & local) for group in local}
        ready = sorted((group for group, degree in indegree.items() if degree == 0),
                       key=lambda group: (priority[group], group))
        sequence = []
        while ready:
            group = ready.pop(0)
            sequence.append(group)
            for nxt in sorted(succs[group] & local):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    ready.append(nxt)
                    ready.sort(key=lambda item: (priority[item], item))
        if len(sequence) != len(local):
            return Candidate(normalized, [], 'fm_preserve',
                             {'schedule_mode': 'preserve'}, {})
        schedules.append(sequence)
    while len(schedules) < len(base_candidate.core_schedules):
        schedules.append([])
    return Candidate(normalized, schedules, 'fm_preserve',
                     {'schedule_mode': 'preserve'}, {})


def _rebuild_candidates(model, mapping, base_candidate, num_cores, scene):
    candidates = []
    preserve = _preserve_schedule(model, mapping, base_candidate)
    candidates.append(preserve)
    try:
        heft = assign_cores(model, canonicalize_mapping(model, mapping), num_cores,
                            scene, generator_name='fm_heft',
                            params={'schedule_mode': 'reassign'})
        candidates.append(heft)
    except (ValueError, KeyError):
        pass
    candidates.extend(_same_core_reorders(model, preserve, limit=4))
    unique = []
    seen = set()
    for candidate in candidates:
        legal, _ = validate_candidate(model, candidate)
        if legal:
            signature = candidate_signature(model, candidate)
            if signature not in seen:
                seen.add(signature)
                unique.append(candidate)
    return unique


def _same_core_reorders(model, candidate, limit=4):
    """Try adjacent same-core subgraph swaps that preserve quotient order."""
    result = []
    for core, order in enumerate(candidate.core_schedules):
        for index in range(len(order) - 1):
            swapped = [list(items) for items in candidate.core_schedules]
            swapped[core][index], swapped[core][index + 1] = (
                swapped[core][index + 1], swapped[core][index])
            trial = Candidate(dict(candidate.node_to_subgraph), swapped,
                              'same_core_reorder',
                              {'schedule_mode': 'adjacent_swap', 'core': core,
                               'index': index}, dict(candidate.metadata))
            legal, _ = validate_candidate(model, trial)
            if legal:
                result.append(trial)
                if len(result) >= limit:
                    return result
    return result


def pi_reorder_candidates(model, candidate, limit=24, radius=2):
    """Enumerate a bounded neighbourhood while keeping P and C fixed.

    The existing refinement already emits a few adjacent swaps as part of
    partition checkpoints.  This dedicated neighbourhood is for the case
    where the partition and core placement are already good but the supplied
    same-core order is slightly wrong.  Only one or two adjacent transpositions
    are composed, and every result goes through the normal legality check.
    """
    result = []
    seen = {candidate_signature(model, candidate)}
    frontier = [candidate]
    for depth in range(max(1, int(radius))):
        next_frontier = []
        for base in frontier:
            for core, order in enumerate(base.core_schedules):
                for index in range(len(order) - 1):
                    swapped = [list(items) for items in base.core_schedules]
                    swapped[core][index], swapped[core][index + 1] = (
                        swapped[core][index + 1], swapped[core][index])
                    trial = Candidate(dict(candidate.node_to_subgraph), swapped,
                                      'pi_reorder',
                                      {'schedule_mode': 'bounded_adjacent_swap',
                                       'depth': depth + 1, 'core': core,
                                       'index': index}, dict(candidate.metadata))
                    legal, _ = validate_candidate(model, trial)
                    if not legal:
                        continue
                    signature = candidate_signature(model, trial)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    result.append(trial)
                    next_frontier.append(trial)
                    if len(result) >= limit:
                        return result
        frontier = next_frontier
        if not frontier:
            break
    return result


def make_chain_moves(model, mapping, max_length=4, limit=8):
    """Build short dependency-chain moves from one source group."""
    moves = []
    for root, neighbors in boundary_ops(model, mapping):
        source = mapping[root]
        for target in neighbors:
            chain = [root]
            cursor = root
            while len(chain) < max_length:
                nxt = [node for node in model.ops[cursor].successors
                       if mapping[node] == source and node not in chain]
                if not nxt:
                    break
                cursor = min(nxt)
                chain.append(cursor)
            if len(chain) >= 2:
                moves.append((tuple(chain), source, target))
            if len(moves) >= limit:
                return moves
    return moves


def refine_candidate(model, seed, num_cores, scene='A',
                     evaluator=None, oracle=None, max_passes=3,
                     max_moves=48, max_negative_moves=2,
                     checkpoint_limit=8, preference='hybrid',
                     enable_chains=True, chain_limit=8,
                     mode='full', deadline=None, problem=2):
    """Run bounded FM passes and return trajectory, candidates and elite.

    ``evaluator`` is a callable Candidate -> result.  It is intentionally
    external so tests can use a deterministic fake and official runs can add
    caching/budgets without coupling the neighborhood implementation.
    """
    mapping = canonicalize_mapping(model, seed.node_to_subgraph)
    current = _preserve_schedule(model, mapping, seed)
    if not validate_candidate(model, current)[0]:
        current = assign_cores(model, mapping, num_cores, scene,
                               generator_name='fm_seed', params={})
    checkpoints = [current]
    history = []
    locked = set()
    pass_count = 1 if mode == 'strict' else max_passes
    negative_limit = 0 if mode == 'strict' else max_negative_moves
    moves_used = 0
    for pass_index in range(pass_count):
        pass_start = current
        prefix_best = current
        prefix_gain = 0.0
        negative_used = 0
        pass_moves = []
        for step in range(max_moves):
            if deadline is not None:
                import time
                if time.perf_counter() >= deadline:
                    history.append({'status': 'budget_exhausted',
                                    'pass': pass_index, 'step': step})
                    break
            if moves_used >= max_moves:
                break
            candidate_moves = []
            before_metrics = partition_metrics(model, current.node_to_subgraph, current)
            boundary = boundary_ops(model, current.node_to_subgraph)
            # A bounded FM pass samples the highest-impact boundary operators;
            # this preserves a real multi-step pass without quadratic scans on
            # the largest official graphs.
            boundary = sorted(
                boundary,
                key=lambda item: (-model.ops[item[0]].upward_rank,
                                  -model.ops[item[0]].cycles, item[0]))[:128]
            for node, targets in boundary:
                if node in locked:
                    continue
                source = current.node_to_subgraph[node]
                for target in targets:
                    trial_map = dict(current.node_to_subgraph)
                    trial_map[node] = target
                    if not _quotient_acyclic(model, trial_map):
                        history.append({'pass': pass_index, 'step': step,
                                        'op_id': node, 'source': source,
                                        'target': target, 'status': 'cycle_rejected'})
                        continue
                    after = partition_metrics(model, trial_map, current)
                    gain = _normalized_gain(before_metrics, after, preference,
                                            problem=problem)
                    candidate_moves.append((gain, node, source, target, trial_map))
            if enable_chains and moves_used < max_moves:
                for chain, source, target in make_chain_moves(
                        model, current.node_to_subgraph, limit=chain_limit):
                    if any(node in locked for node in chain):
                        continue
                    trial_map = dict(current.node_to_subgraph)
                    for node in chain:
                        trial_map[node] = target
                    if not _quotient_acyclic(model, trial_map):
                        continue
                    after = partition_metrics(model, trial_map, current)
                    gain = _normalized_gain(before_metrics, after, preference,
                                            problem=problem)
                    candidate_moves.append((gain, chain, source, target, trial_map))
            if not candidate_moves:
                break
            gain, node_or_chain, source, target, trial_map = max(
                candidate_moves, key=lambda item: (item[0], str(item[1]), -item[3]))
            is_chain = isinstance(node_or_chain, tuple)
            if gain < 0:
                if negative_used >= negative_limit:
                    break
                negative_used += 1
            current = _preserve_schedule(model, trial_map, current)
            if not validate_candidate(model, current)[0]:
                break
            locked.update(node_or_chain if is_chain else (node_or_chain,))
            moves_used += 1
            prefix_gain += gain
            pass_moves.append((gain, node_or_chain, source, target))
            if prefix_gain > 0:
                prefix_best = current
            if len(checkpoints) < checkpoint_limit:
                checkpoints.append(current)
            history.append({
                'pass': pass_index, 'step': step, 'op_id': node_or_chain,
                'source': source, 'target': target, 'gain': gain,
                'cumulative_gain': prefix_gain, 'status': 'tentative',
            })
        current = prefix_best
        if current is not pass_start:
            checkpoints.append(current)
        locked.clear()
        if not pass_moves:
            break
    if enable_chains and len(checkpoints) < checkpoint_limit:
        for chain, source, target in make_chain_moves(
                model, current.node_to_subgraph, limit=chain_limit):
            if deadline is not None:
                import time
                if time.perf_counter() >= deadline:
                    break
            trial_map = dict(current.node_to_subgraph)
            for node in chain:
                trial_map[node] = target
            if _quotient_acyclic(model, trial_map):
                candidates = _rebuild_candidates(model, trial_map, current,
                                                 num_cores, scene)
                checkpoints.extend(candidates[:checkpoint_limit - len(checkpoints)])
            if len(checkpoints) >= checkpoint_limit:
                break
    # Add both scheduling adaptations for every retained partition checkpoint.
    adapted = []
    seen = set()
    for candidate in checkpoints:
        for rebuilt in _rebuild_candidates(model, candidate.node_to_subgraph,
                                           candidate, num_cores, scene):
            sig = candidate_signature(model, rebuilt)
            if sig not in seen:
                seen.add(sig)
                adapted.append(rebuilt)
    if evaluator is None:
        return {'candidates': adapted, 'history': history,
                'moves_used': moves_used, 'checkpoints': len(checkpoints)}
    # The callback owns official budget and acceptance.  Evaluate each adapted
    # checkpoint once; no exploratory result can replace the incumbent itself.
    evaluated = []
    for candidate in adapted:
        result = evaluator(candidate)
        evaluated.append((candidate, result))
        history.append({'pass': None, 'step': None,
                        'candidate_signature': repr(candidate_signature(model, candidate)),
                        'status': 'evaluated',
                        'makespan': getattr(result, 'makespan', None)})
    return {'candidates': adapted, 'evaluated': evaluated, 'history': history,
            'moves_used': moves_used, 'checkpoints': len(checkpoints)}
