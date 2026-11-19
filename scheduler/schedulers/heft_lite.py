"""A deliberately small HEFT-style scheduler for Stage 1."""

from __future__ import annotations

from collections import defaultdict

from ..candidate import Candidate


def _subgraph_view(model, mapping):
    members = defaultdict(list)
    for op_id, sg in mapping.items():
        members[sg].append(op_id)
    for node_ids in members.values():
        node_ids.sort()
    preds = {sg: set() for sg in members}
    succs = {sg: set() for sg in members}
    edge_size = {}
    for edge in model.edges:
        a, b = mapping[edge.src], mapping[edge.dst]
        if a != b:
            preds[b].add(a)
            succs[a].add(b)
            edge_size[(a, b)] = max(edge_size.get((a, b), 0), edge.tensor_size)
    stats = {}
    for sg, node_ids in members.items():
        stats[sg] = {
            'm': sum(model.ops[n].pipe_m_cycles for n in node_ids),
            'v': sum(model.ops[n].pipe_v_cycles for n in node_ids),
            'cycles': sum(model.ops[n].cycles for n in node_ids),
            'work': sum(max(model.ops[n].pipe_m_cycles,
                            model.ops[n].pipe_v_cycles)
                        for n in node_ids),
            'bytes': sum(model.ops[n].input_bytes + model.ops[n].output_bytes
                         for n in node_ids),
        }
    topo = []
    indegree = {sg: len(preds[sg]) for sg in members}
    ready = sorted(sg for sg, d in indegree.items() if d == 0)
    while ready:
        sg = ready.pop(0)
        topo.append(sg)
        for nxt in sorted(succs[sg]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    if len(topo) != len(members):
        raise ValueError('subgraph quotient contains a cycle')
    rank = {sg: float(stats[sg]['work']) for sg in members}
    for sg in reversed(topo):
        rank[sg] = stats[sg]['work'] + max(
            (edge_size.get((sg, nxt), 0) + rank[nxt]
             for nxt in succs[sg]), default=0.0)
    return members, preds, succs, edge_size, stats, rank, topo


def assign_cores(model, mapping, num_cores, scene='A',
                 generator_name='candidate', params=None,
                 metadata=None) -> Candidate:
    """Assign quotient tasks with M/V-aware EFT and derive legal core orders."""
    if num_cores < 1:
        raise ValueError('num_cores must be positive')
    members, preds, succs, edge_size, stats, rank, topo = _subgraph_view(model, mapping)
    order = sorted(members, key=lambda sg: (-rank[sg], sg))
    core_of = {}
    finish = {}
    core_available = [0.0] * num_cores
    loads_m = [0] * num_cores
    loads_v = [0] * num_cores
    same_wait = 100 if scene == 'A' else 0
    cross_wait = 1000 if scene == 'A' else 500
    for sg in order:
        best = None
        for core in range(num_cores):
            pred_ready = 0.0
            for pred in preds[sg]:
                if pred not in finish:
                    continue
                delay = same_wait if core_of[pred] == core else cross_wait
                pred_ready = max(pred_ready, finish[pred] + delay)
            start = max(core_available[core], pred_ready)
            # Keep the two pipes explicit.  The max is an estimate of overlap,
            # while the small load term prevents pathological M/V imbalance.
            duration = max(1, stats[sg]['work'])
            finish_time = start + duration
            score = (finish_time, max(loads_m[core] + stats[sg]['m'],
                                     loads_v[core] + stats[sg]['v']), core)
            sticky = float((params or {}).get('sticky', 0.0))
            if sticky:
                # A predecessor-sticky candidate gives a small, explicit
                # preference to keeping depth-band components beside their
                # already placed predecessors.  The normal EFT terms remain
                # primary; this only breaks near ties and reduces avoidable
                # cross-core synchronization.
                same_pred = sum(1 for pred in preds[sg]
                                if pred in core_of and core_of[pred] == core)
                cross_pred = sum(1 for pred in preds[sg]
                                 if pred in core_of and core_of[pred] != core)
                score = (finish_time + sticky * 1000.0 * cross_pred
                         - sticky * 500.0 * same_pred,
                         score[1], core)
            if best is None or score < best[0]:
                best = (score, core, finish_time)
        _, core, finish_time = best
        core_of[sg] = core
        finish[sg] = finish_time
        core_available[core] = finish_time
        loads_m[core] += stats[sg]['m']
        loads_v[core] += stats[sg]['v']

    # Stable Kahn order per core; assignment order is only a priority and may
    # have placed a descendant before a predecessor on the same core.
    core_orders = []
    priority = {sg: i for i, sg in enumerate(order)}
    for core in range(num_cores):
        local = {sg for sg, assigned in core_of.items() if assigned == core}
        local_indegree = {sg: len(preds[sg] & local) for sg in local}
        ready = sorted((sg for sg, d in local_indegree.items() if d == 0),
                       key=lambda sg: (priority[sg], sg))
        sequence = []
        while ready:
            sg = ready.pop(0)
            sequence.append(sg)
            for nxt in sorted(succs[sg] & local):
                local_indegree[nxt] -= 1
                if local_indegree[nxt] == 0:
                    ready.append(nxt)
                    ready.sort(key=lambda item: (priority[item], item))
        if len(sequence) != len(local):
            raise ValueError('same-core subgraph order contains a cycle')
        core_orders.append(sequence)
    metadata = dict(metadata or {})
    metadata.update({'subgraph_stats': stats, 'core_load_m': loads_m,
                     'core_load_v': loads_v})
    return Candidate(dict(mapping), core_orders, generator_name,
                     dict(params or {}), metadata)
