"""Tensor-net macroblocks and residency-aware *candidate* scheduling.

No tensor is split or duplicated. Windows are priorities/working-set proxies,
not allocations, physical pages, actual spill, or a parallel cache simulation.
The official evaluator owns COPY insertion, Step2, FIFO and acceptance.
"""
from collections import defaultdict
from dataclasses import dataclass
import heapq

from .candidate import Candidate, validate_candidate
from .schedulers.heft_lite import _subgraph_view, assign_cores


@dataclass(frozen=True)
class TensorPage:
    tensor_id: int
    size: int
    bank: str
    producers: frozenset
    consumers: frozenset
    raw_consumers: frozenset
    input_only: bool
    logical_key: int


def tensor_pages(raw):
    """One net per original tensor, without lossy op-pair contraction.

    Direct compute pins match official Task construction. Raw COPY consumers
    are retained separately: a DDR source's COPY_IN fanout is not evidence of
    equal cache keys. Official COPY_IN keys use the destination logical_tid.
    """
    ops = {o['id']: o for o in raw['ops']}
    compute = {n for n, o in ops.items()
               if o['op'] not in ('COPY_IN', 'COPY_OUT')}
    tensors = {t['id']: t for t in raw['tensors']}
    producers, consumers = defaultdict(set), defaultdict(set)
    for e in raw['edges']:
        a, b = e['source'], e['target']
        if a in ops and b in tensors:
            producers[b].add(a)
        elif a in tensors and b in ops:
            consumers[a].add(b)
    return {tid: TensorPage(
        tid, int(t['size']), 'L1' if t['pos'] == 'L1' else 'UB',
        frozenset(producers[tid] & compute),
        frozenset(consumers[tid] & compute), frozenset(consumers[tid]),
        bool(consumers[tid] & compute) and not bool(producers[tid] & compute),
        t.get('logical_tid', tid)) for tid, t in tensors.items()}


def op_pages(model, pages):
    inputs = {n: set() for n in model.ops}
    outputs = {n: set() for n in model.ops}
    for tid, page in pages.items():
        for n in page.consumers:
            inputs[n].add(tid)
        for n in page.producers:
            outputs[n].add(tid)
    return inputs, outputs


def footprint(tids, pages):
    result = {'L1': 0, 'UB': 0}
    for tid in set(tids):
        result[pages[tid].bank] += pages[tid].size
    return result


def macro_partition(model, pages, capacity, max_ops=16, ready_limit=128):
    """Greedy tensor-net coarsening along an affinity-biased topological order.

    Only globally ready compute ops join a block. Blocks are consecutive in
    that order, so all dependencies (including zero-byte ones) remain acyclic.
    Working-set limits close blocks; an oversized singleton is retained and
    explicitly left for official Step2. Pins use sets, not expanded edges.
    """
    if max_ops < 1 or ready_limit < 1:
        raise ValueError('positive macro and frontier limits required')
    inputs, outputs = op_pages(model, pages)
    degree = {n: len(o.predecessors) for n, o in model.ops.items()}
    ready = [(-model.ops[n].upward_rank, n) for n, d in degree.items() if d == 0]
    heapq.heapify(ready)
    blocks, current, resident, mapping = [], [], set(), {}
    while ready:
        pool = [heapq.heappop(ready) for _ in range(min(ready_limit, len(ready)))]
        def key(item):
            n = item[1]
            shared = sum(pages[t].size for t in inputs[n] & resident)
            return (-shared, item[0], n)
        chosen = min(pool, key=key)
        n = chosen[1]
        touched = inputs[n] | outputs[n]
        workset = footprint(resident | touched, pages)
        if current and (len(current) >= max_ops or any(
                workset[b] > capacity[b] for b in capacity)):
            blocks.append(current)
            current, resident = [], set()
        mapping[n] = len(blocks)
        current.append(n)
        resident |= touched
        for item in pool:
            if item != chosen:
                heapq.heappush(ready, item)
        for dst in sorted(model.ops[n].successors):
            degree[dst] -= 1
            if degree[dst] == 0:
                heapq.heappush(ready, (-model.ops[dst].upward_rank, dst))
    if current:
        blocks.append(current)
    if set(mapping) != set(model.ops):
        raise ValueError('incomplete macro partition')
    return mapping


def macro_graph(model, mapping, pages):
    """Explicit macro records + tensor hyperedges; zero-byte DAG kept separate."""
    members, preds, succs, _, stats, rank, topo = _subgraph_view(model, mapping)
    inputs, outputs = defaultdict(set), defaultdict(set)
    nets = []
    for tid, p in pages.items():
        src = {mapping[n] for n in p.producers}
        dst = {mapping[n] for n in p.consumers}
        if not src and not dst:
            continue
        nets.append({'tensor_id': tid, 'size': p.size,
                     'producer_blocks': sorted(src), 'consumer_blocks': sorted(dst)})
        for sg in dst:
            inputs[sg].add(tid)
        for sg in src:
            outputs[sg].add(tid)
    records = {sg: {'op_ids': members[sg], 'input_tensors': sorted(inputs[sg]),
                    'output_tensors': sorted(outputs[sg]), 'm': stats[sg]['m'],
                    'v': stats[sg]['v'], 'working_set_bytes': footprint(
                        inputs[sg] | outputs[sg], pages),
                    'cross_block_edges': len(preds[sg]) + len(succs[sg])}
               for sg in members}
    return records, nets, preds, succs, rank, topo


def residency_schedule(model, mapping, pages, cores, problem, capacity,
                       bandwidth, fixed_core=None, reuse_weight=1.0):
    """Ready-list macro placement with two-dimensional loads and tensor owners.

    fixed_core constrains C for a pure Pi experiment. Otherwise owner affinity,
    fresh working-set bytes/bandwidth and projected M/V loads choose C. A global
    topological order is projected onto cores to avoid added task-order cycles.
    No predicted cache hits or spill bytes are asserted.
    """
    if cores < 1 or bandwidth <= 0 or problem not in (1, 2, 3):
        raise ValueError('invalid residency scheduling parameters')
    records, nets, preds, succs, rank, topo = macro_graph(model, mapping, pages)
    degree = {sg: len(preds[sg]) for sg in records}
    ready = {sg for sg, d in degree.items() if d == 0}
    loads = [[0, 0] for _ in range(cores)]
    windows = [set() for _ in range(cores)]
    sequences = [[] for _ in range(cores)]
    owners = defaultdict(set)
    core_of, history = {}, []
    recent_inputs = set()
    while ready:
        options = []
        for sg in sorted(ready, key=lambda g: (-rank[g], g))[:128]:
            r = records[sg]
            inp, out = set(r['input_tensors']), set(r['output_tensors'])
            for core in ([fixed_core[sg]] if fixed_core is not None else range(cores)):
                projected = max(loads[core][0] + r['m'], loads[core][1] + r['v'])
                fresh = sum(pages[t].size for t in inp - windows[core])
                migrate = sum(pages[t].size for t in inp
                              if owners[t] and core not in owners[t])
                ws = footprint(windows[core] | inp | out, pages)
                overflow = sum(max(0, ws[b] - capacity[b]) for b in capacity)
                # All terms are cycles proxies. Transfer terms assume no
                # overlap/contention and do not equal elapsed time or spill.
                cost = projected + reuse_weight * (fresh + migrate + overflow) / bandwidth
                if fixed_core is not None:
                    # Pure Pi: compare marginal reuse rather than accumulated
                    # work on unrelated cores; retain critical-rank tie break.
                    cost = reuse_weight * (fresh + migrate + overflow) / bandwidth
                if problem == 3:
                    # Cluster equal input keys across consecutive ready
                    # macros. This is an access-window preference only:
                    # actual parallel COPY completion order is unknown here.
                    shared = sum(pages[t].size for t in inp & recent_inputs
                                 if pages[t].input_only)
                    cost -= reuse_weight * shared / bandwidth
                options.append((cost, -rank[sg], sg, core))
        _, _, sg, core = min(options)
        r = records[sg]
        touched = set(r['input_tensors']) | set(r['output_tensors'])
        ws = footprint(windows[core] | touched, pages)
        reset = any(ws[b] > capacity[b] for b in capacity)
        if reset:
            windows[core] = set()
        reuse = sum(pages[t].size for t in set(r['input_tensors']) & windows[core])
        windows[core] |= touched
        recent_inputs = {t for t in r['input_tensors'] if pages[t].input_only}
        history.append({'block': sg, 'core': core, 'new_window': reset,
                        'working_set_proxy': footprint(windows[core], pages),
                        'reuse_bytes_proxy': reuse, 'tensor_ids': sorted(windows[core])})
        for tid in r['output_tensors']:
            owners[tid].add(core)  # multiple producers are retained, never copied
        loads[core][0] += r['m']
        loads[core][1] += r['v']
        core_of[sg] = core
        sequences[core].append(sg)
        ready.remove(sg)
        for dst in succs[sg]:
            degree[dst] -= 1
            if not degree[dst]:
                ready.add(dst)
    candidate = Candidate(dict(mapping), sequences, 'tensor_residency',
                          {'problem': problem, 'reuse_weight': reuse_weight,
                           'fixed_core': fixed_core is not None},
                          {'windows_proxy': history, 'macro_count': len(records),
                           'core_load_m': [x[0] for x in loads],
                           'core_load_v': [x[1] for x in loads],
                           'owner_cores_proxy': {t: sorted(c) for t, c in owners.items()}})
    legal, reason = validate_candidate(model, candidate)
    if not legal:
        raise ValueError(reason)
    return candidate


def build_residency_candidates(model, pages, base, problem, capacity, bandwidth):
    """Bounded opt-in pool; original portfolio/elite is supplied by the caller."""
    cores = len(base.core_schedules)
    fixed = {sg: c for c, seq in enumerate(base.core_schedules) for sg in seq}
    yield 'fixed_pc_window', residency_schedule(
        model, base.node_to_subgraph, pages, cores, problem, capacity, bandwidth,
        fixed_core=fixed)
    yield 'fixed_p_residency', residency_schedule(
        model, base.node_to_subgraph, pages, cores, problem, capacity, bandwidth)
    for size in (8, 32):
        mapping = macro_partition(model, pages, capacity, max_ops=size)
        candidate = assign_cores(model, mapping, cores, 'A' if problem == 1 else 'B',
                                 generator_name='tensor_macro_heft', params={'size': size})
        yield f'macro_{size}_heft', candidate
        candidate = residency_schedule(model, mapping, pages, cores, problem,
                                        capacity, bandwidth)
        candidate.params['size'] = size
        yield f'macro_{size}_joint', candidate
