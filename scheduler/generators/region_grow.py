"""Communication and critical-path guided region growth."""

from collections import defaultdict
import time

from ..schedulers.heft_lite import assign_cores


def _grow_once(graph, max_ops_per_sg, depth_window,
               communication_weight, critical_weight, *, reseed=False,
               edge_mode='bytes', balance='max', size_slack=0,
               diagnostics=None):
    unassigned = set(graph.ops)
    mapping = {}
    sg = 0
    diag = diagnostics if diagnostics is not None else {}
    diag.setdefault('frontier_exhaustion_count', 0)
    diag.setdefault('reseed_count', 0)
    diag.setdefault('reseed_seed_nodes', [])
    diag.setdefault('frontier_events', [])
    diag.setdefault('fallback_seed_op_count', 0)
    incident = defaultdict(set)
    edge_weight = {}
    for edge in graph.edges:
        incident[edge.src].add(edge.dst)
        incident[edge.dst].add(edge.src)
        edge_weight[(edge.src, edge.dst)] = edge.tensor_size
        edge_weight[(edge.dst, edge.src)] = edge.tensor_size
    while unassigned:
        seed = max(unassigned, key=lambda n: (
            graph.ops[n].upward_rank, graph.ops[n].depth_to_sink, -n))
        members = {seed}
        frontier = set(incident[seed]) & unassigned
        target_size = max_ops_per_sg + max(0, int(size_slack))
        while len(members) < target_size:
            # On a weakly connected or depth-windowed graph the frontier can
            # be exhausted before the target size is reached.  The reference
            # implementation reseeds from the remaining high-work nodes in
            # that case.  Keep this explicitly opt-in so the original Stage 1
            # RegionGrow semantics remain available as a control candidate.
            if not frontier:
                remaining = unassigned - members
                missing_ops = max(0, target_size - len(members))
                diag['frontier_exhaustion_count'] += 1
                event = {
                    'subgraph': sg,
                    'members': len(members),
                    'target_size': target_size,
                    'missing_ops': missing_ops,
                    'reseeded': bool(reseed and remaining),
                    'current_m_cycles': sum(graph.ops[n].pipe_m_cycles for n in members),
                    'current_v_cycles': sum(graph.ops[n].pipe_v_cycles for n in members),
                }
                if not reseed or not remaining:
                    diag['frontier_events'].append(event)
                    break
                seed = max(remaining, key=lambda n: (
                    graph.ops[n].upward_rank, graph.ops[n].depth_to_sink, -n))
                frontier.add(seed)
                diag['reseed_count'] += 1
                diag['fallback_seed_op_count'] += 1
                diag['reseed_seed_nodes'].append(seed)
                diag['frontier_events'].append(event)
            # Large official graphs can expose a very wide frontier.  Ranking
            # a bounded prefix keeps this baseline linear in graph size while
            # retaining high-rank and communication-heavy candidates.
            ranked_frontier = sorted(
                frontier,
                key=lambda node: (-graph.ops[node].upward_rank, node))[:512]

            def score(node):
                info = graph.ops[node]
                if edge_mode == 'count':
                    comm = sum(1 for m in members
                               if (node, m) in edge_weight)
                else:
                    comm = sum(edge_weight.get((node, m), 0)
                               for m in members)
                affinity = sum(1 for m in members
                               if graph.ops[m].pipe == info.pipe)
                complement = sum(1 for m in members
                                 if graph.ops[m].pipe != info.pipe)
                distance = min(abs(info.depth_from_source -
                                   graph.ops[m].depth_from_source)
                               for m in members)
                if depth_window and distance > depth_window:
                    return (-float('inf'),)
                if balance == 'mv':
                    # Favor the less represented pipe when growing a region;
                    # this is only a local preference and does not replace the
                    # M/V-aware core assignment performed afterwards.
                    m_load = sum(graph.ops[m].pipe_m_cycles for m in members)
                    v_load = sum(graph.ops[m].pipe_v_cycles for m in members)
                    pipe_load = (m_load if info.pipe == 'PIPE_M' else v_load)
                    balance_term = -0.05 * pipe_load
                else:
                    # Preserve the original RegionGrow score exactly for the
                    # default ``balance='max'`` control path.
                    balance_term = 0.0
                gain = (communication_weight * comm
                        + critical_weight * info.upward_rank
                        + 0.1 * complement + 0.01 * affinity
                        + balance_term - 0.1 * info.cycles)
                return (gain, info.upward_rank, -node)
            node = max(ranked_frontier, key=score)
            if score(node)[0] == float('-inf'):
                break
            members.add(node)
            frontier.update(incident[node] & unassigned)
            frontier.difference_update(members)
        for node in sorted(members):
            mapping[node] = sg
        unassigned -= members
        sg += 1
    diag['subgraph_count'] = sg
    diag['fallback_seed_op_ratio'] = (
        diag['fallback_seed_op_count'] / max(1, len(graph.ops)))
    return mapping


def generate(graph, num_cores, scene='A', params=None):
    params = dict(params or {})
    params.setdefault('max_ops_per_sg', max(8, len(graph.ops) // max(1, num_cores * 4)))
    params.setdefault('depth_window', 0)
    params.setdefault('communication_weight', 1.0)
    params.setdefault('critical_weight', 0.01)
    params.setdefault('reseed', False)
    params.setdefault('edge_mode', 'bytes')
    params.setdefault('balance', 'max')
    params.setdefault('size_slack', 0)
    diagnostics = {}
    started_grow = time.perf_counter()
    mapping = _grow_once(
        graph, int(params['max_ops_per_sg']), int(params['depth_window']),
        float(params['communication_weight']), float(params['critical_weight']),
        reseed=bool(params['reseed']), edge_mode=str(params['edge_mode']),
        balance=str(params['balance']), size_slack=int(params['size_slack']),
        diagnostics=diagnostics)
    diagnostics['candidate_generation_seconds'] = time.perf_counter() - started_grow
    sg_members = defaultdict(list)
    for node, group in mapping.items():
        sg_members[group].append(node)
    total_m = sum(op.pipe_m_cycles for op in graph.ops.values())
    total_v = sum(op.pipe_v_cycles for op in graph.ops.values())
    target_m = total_m / max(1, len(sg_members))
    target_v = total_v / max(1, len(sg_members))
    diagnostics['target_m_cycles_per_subgraph'] = target_m
    diagnostics['target_v_cycles_per_subgraph'] = target_v
    diagnostics['subgraph_loads'] = [
        {'subgraph': int(group),
         'm_cycles': sum(graph.ops[n].pipe_m_cycles for n in nodes),
         'v_cycles': sum(graph.ops[n].pipe_v_cycles for n in nodes),
         'ops': len(nodes)}
        for group, nodes in sorted(sg_members.items())]
    for event in diagnostics.get('frontier_events', []):
        event['target_m_gap'] = target_m - event['current_m_cycles']
        event['target_v_gap'] = target_v - event['current_v_cycles']
    name = 'region_grow_reseed' if params['reseed'] else 'region_grow'
    started_schedule = time.perf_counter()
    candidate = assign_cores(graph, mapping, num_cores, scene,
                             generator_name=name, params=params,
                             metadata={'construction_diagnostics': diagnostics})
    diagnostics['heft_schedule_seconds'] = time.perf_counter() - started_schedule
    return candidate
