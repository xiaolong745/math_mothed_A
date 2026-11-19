"""A compact, evaluator-independent representation of an Op-Tensor graph.

The official input is a bipartite graph with explicit COPY operators.  Stage 1
partitions only compute operators, so COPY operators are contracted while the
tensor on the contracted path is retained as dependency metadata.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

COPY_TYPES = {'COPY_IN', 'COPY_OUT'}


@dataclass(frozen=True)
class DependencyEdge:
    src: int
    dst: int
    tensor_id: int | None = None
    tensor_size: int = 0
    tensor_pos: str | None = None


@dataclass
class OpInfo:
    op_id: int
    pipe: str
    cycles: int
    predecessors: set[int] = field(default_factory=set)
    successors: set[int] = field(default_factory=set)
    depth_from_source: int = 0
    depth_to_sink: int = 0
    upward_rank: float = 0.0
    input_bytes: int = 0
    output_bytes: int = 0
    pipe_m_cycles: int = 0
    pipe_v_cycles: int = 0


@dataclass
class GraphModel:
    ops: dict[int, OpInfo]
    edges: list[DependencyEdge]
    tensor_by_id: dict[int, dict[str, Any]]
    topo_order: list[int]
    tensor_producers: dict[int, set[int]] = field(default_factory=dict)
    tensor_consumers: dict[int, set[int]] = field(default_factory=dict)

    @property
    def op_ids(self) -> list[int]:
        return list(self.ops)

    @property
    def edge_by_pair(self) -> dict[tuple[int, int], DependencyEdge]:
        return {(e.src, e.dst): e for e in self.edges}


def _topological(nodes: set[int], succs: dict[int, set[int]],
                 preds: dict[int, set[int]]) -> list[int]:
    indegree = {node: len(preds[node] & nodes) for node in nodes}
    ready = deque(sorted(node for node, count in indegree.items() if count == 0))
    result: list[int] = []
    while ready:
        node = ready.popleft()
        result.append(node)
        for nxt in sorted(succs[node]):
            if nxt not in nodes:
                continue
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
    if len(result) != len(nodes):
        raise ValueError('compute Op-DAG contains a cycle')
    return result


def build_graph_model(graph: dict[str, Any]) -> GraphModel:
    """Build the contracted compute Op-DAG without mutating ``graph``.

    Every original graph edge is first converted to an operation adjacency
    graph.  A path through COPY operators is then contracted.  The nearest
    tensor edge on that path is attached to the resulting dependency; direct
    op-to-op edges are retained with their optional ``data_size``.
    """
    raw_ops = {int(op['id']): op for op in graph.get('ops', [])}
    tensors = {int(t['id']): dict(t) for t in graph.get('tensors', [])}
    compute_ids = {
        op_id for op_id, op in raw_ops.items()
        if op.get('op') not in COPY_TYPES
    }
    op_succ: dict[int, set[int]] = {op_id: set() for op_id in raw_ops}
    op_pred: dict[int, set[int]] = {op_id: set() for op_id in raw_ops}
    edge_tensor: dict[tuple[int, int], tuple[int | None, int, str | None]] = {}
    tensor_out: dict[int, set[int]] = defaultdict(set)
    tensor_in: dict[int, set[int]] = defaultdict(set)

    op_ids = set(raw_ops)
    for edge in graph.get('edges', []):
        src, dst = int(edge['source']), int(edge['target'])
        if src in op_ids and dst in op_ids:
            op_succ[src].add(dst)
            op_pred[dst].add(src)
            edge_tensor[(src, dst)] = (None, int(edge.get('data_size', 0)), None)
        elif src in op_ids and dst in tensors:
            tensor_out[dst].add(src)
        elif src in tensors and dst in op_ids:
            tensor_in[src].add(dst)

    # Tensor-mediated op edges.  A tensor can have multiple producers in the
    # input contract; retain all producer/consumer pairs deterministically.
    for tid in sorted(set(tensor_out) | set(tensor_in)):
        t = tensors.get(tid, {})
        meta = (tid, int(t.get('size', 0)), t.get('pos'))
        for src in sorted(tensor_out[tid]):
            for dst in sorted(tensor_in[tid]):
                if src != dst:
                    op_succ[src].add(dst)
                    op_pred[dst].add(src)
                    edge_tensor[(src, dst)] = meta

    # Contract COPY nodes by searching from each compute source until the next
    # compute node.  This also handles COPY_IN/OUT chains with intermediate
    # tensors, which are common in the official cases.
    contracted: dict[tuple[int, int], tuple[int | None, int, str | None]] = {}
    for src in sorted(compute_ids):
        queue = deque((nxt, None) for nxt in sorted(op_succ[src]))
        visited: set[int] = set()
        while queue:
            node, carried = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            carried = carried or edge_tensor.get((src, node))
            if node in compute_ids:
                if node != src:
                    old = contracted.get((src, node))
                    if old is None or carried and carried[1] > old[1]:
                        contracted[(src, node)] = carried or (None, 0, None)
                continue
            for nxt in sorted(op_succ[node]):
                next_carried = carried
                if next_carried is None:
                    next_carried = edge_tensor.get((node, nxt))
                queue.append((nxt, next_carried))

    # Direct compute-to-compute edges may not be reachable through the COPY
    # search's initial metadata; merge them without replacing tensor edges.
    for (src, dst), meta in edge_tensor.items():
        if src in compute_ids and dst in compute_ids:
            contracted[(src, dst)] = meta

    preds = {op_id: set() for op_id in compute_ids}
    succs = {op_id: set() for op_id in compute_ids}
    for (src, dst) in contracted:
        preds[dst].add(src)
        succs[src].add(dst)
    topo = _topological(compute_ids, succs, preds)

    infos = {
        op_id: OpInfo(
            op_id=op_id,
            pipe=str(raw_ops[op_id].get('pipe')),
            cycles=max(0, int(raw_ops[op_id].get('cycles', 0))),
            predecessors=set(preds[op_id]),
            successors=set(succs[op_id]),
            pipe_m_cycles=(max(0, int(raw_ops[op_id].get('cycles', 0)))
                           if raw_ops[op_id].get('pipe') == 'PIPE_M' else 0),
            pipe_v_cycles=(max(0, int(raw_ops[op_id].get('cycles', 0)))
                           if raw_ops[op_id].get('pipe') == 'PIPE_V' else 0),
        ) for op_id in sorted(compute_ids)
    }
    edges = [
        DependencyEdge(src, dst, *contracted[(src, dst)])
        for src, dst in sorted(contracted)
    ]
    edge_lookup = {(edge.src, edge.dst): edge for edge in edges}
    # Include graph inputs/outputs and tensors whose only adjacent operation is
    # a COPY node.  These bytes are features, not extra DAG edges.
    for tid, producer_ids in tensor_out.items():
        size = int(tensors.get(tid, {}).get('size', 0))
        for op_id in producer_ids & compute_ids:
            infos[op_id].output_bytes += size
    for tid, consumer_ids in tensor_in.items():
        size = int(tensors.get(tid, {}).get('size', 0))
        for op_id in consumer_ids & compute_ids:
            infos[op_id].input_bytes += size

    for op_id in topo:
        if infos[op_id].predecessors:
            infos[op_id].depth_from_source = 1 + max(
                infos[p].depth_from_source for p in infos[op_id].predecessors)
    for op_id in reversed(topo):
        if infos[op_id].successors:
            infos[op_id].depth_to_sink = 1 + max(
                infos[s].depth_to_sink for s in infos[op_id].successors)
        infos[op_id].upward_rank = infos[op_id].cycles + max(
            (edge_lookup[(op_id, s)].tensor_size + infos[s].upward_rank
             for s in infos[op_id].successors),
            default=0.0)
    return GraphModel(infos, edges, tensors, topo,
                      {tid: set(values) for tid, values in tensor_out.items()},
                      {tid: set(values) for tid, values in tensor_in.items()})
