"""Cheap history surrogate for evaluator-in-the-loop candidate ranking.

The module deliberately has no scientific Python dependency.  It extracts
features that are already available in the contracted graph and Candidate,
fits a small standardized ridge model, and keeps a deterministic diversity
reserve.  The official evaluator remains the acceptance oracle.
"""

from __future__ import annotations

import math
from collections import defaultdict

from ..candidate import candidate_signature, partition_signature
from ..refinement import partition_metrics


FEATURE_NAMES = (
    'op_count', 'edge_count', 'tensor_count', 'dag_depth', 'max_level_width',
    'total_m_cycles', 'total_v_cycles', 'problem', 'cores',
    'subgraph_count', 'max_core_m', 'max_core_v', 'mean_core_m',
    'mean_core_v', 'mv_imbalance', 'unique_cut_bytes',
    'estimated_copy_bytes', 'target_fanout', 'cut_pairs',
    'cross_core_pairs', 'sync_events_proxy', 'pipe_conflict',
    'data_reuse_loss', 'critical_path_cycles', 'lb_comm', 'lb_sync',
    'lower_bound', 'schedule_disorder',
)


def _critical_path(model):
    finish = {}
    for node in model.topo_order:
        finish[node] = model.ops[node].cycles + max(
            (finish[p] for p in model.ops[node].predecessors), default=0)
    return max(finish.values(), default=0)


def _loads(model, candidate):
    core_of = {sg: core for core, order in enumerate(candidate.core_schedules)
               for sg in order}
    loads = [[0.0, 0.0] for _ in candidate.core_schedules]
    for node, sg in candidate.node_to_subgraph.items():
        core = core_of.get(sg, 0)
        loads[core][0] += model.ops[node].pipe_m_cycles
        loads[core][1] += model.ops[node].pipe_v_cycles
    return loads


def _schedule_disorder(model, candidate):
    """Count adjacent same-core inversions against topological positions."""
    topo_pos = {node: i for i, node in enumerate(model.topo_order)}
    members = defaultdict(list)
    for node, sg in candidate.node_to_subgraph.items():
        members[sg].append(node)
    sg_pos = {sg: min(topo_pos[n] for n in nodes)
              for sg, nodes in members.items()}
    inversions = 0
    for order in candidate.core_schedules:
        inversions += sum(sg_pos[a] > sg_pos[b]
                          for a, b in zip(order, order[1:]))
    return float(inversions)


def feature_dict(model, candidate, problem, cores, *, ddr_bytes_per_cycle=64.0,
                 sync_cycles=500.0):
    """Extract cheap P/C/Pi and graph features for one candidate."""
    metrics = partition_metrics(model, candidate.node_to_subgraph, candidate)
    loads = _loads(model, candidate)
    mvals = [x[0] for x in loads] or [0.0]
    vvals = [x[1] for x in loads] or [0.0]
    total_m = sum(mvals)
    total_v = sum(vvals)
    cp = _critical_path(model)
    lb_comm = metrics['estimated_copy_bytes'] / max(1.0, ddr_bytes_per_cycle)
    lb_sync = metrics['cross_core_pairs'] * sync_cycles
    lower_bound = max(total_m / max(1, cores), total_v / max(1, cores),
                      cp, lb_comm, lb_sync)
    # A task containing both pipe types has an intrinsic overlap/conflict
    # signal.  It is intentionally a feature, not an event simulation.
    pipe_conflict = 0.0
    members = defaultdict(list)
    for node, sg in candidate.node_to_subgraph.items():
        members[sg].append(node)
    for nodes in members.values():
        m = sum(model.ops[n].pipe_m_cycles for n in nodes)
        v = sum(model.ops[n].pipe_v_cycles for n in nodes)
        pipe_conflict += min(m, v)
    depths = defaultdict(int)
    for op in model.ops.values():
        depths[op.depth_from_source] += 1
    values = {
        'op_count': len(model.ops), 'edge_count': len(model.edges),
        'tensor_count': len(model.tensor_by_id),
        'dag_depth': max(depths, default=0),
        'max_level_width': max(depths.values(), default=0),
        'total_m_cycles': total_m, 'total_v_cycles': total_v,
        'problem': problem, 'cores': cores,
        'subgraph_count': len(set(candidate.node_to_subgraph.values())),
        'max_core_m': max(mvals), 'max_core_v': max(vvals),
        'mean_core_m': total_m / max(1, len(mvals)),
        'mean_core_v': total_v / max(1, len(vvals)),
        'mv_imbalance': max(max(mvals) / max(1.0, total_m / max(1, cores)),
                            max(vvals) / max(1.0, total_v / max(1, cores))),
        'unique_cut_bytes': metrics['unique_cut_bytes'],
        'estimated_copy_bytes': metrics['estimated_copy_bytes'],
        'target_fanout': metrics['target_fanout'],
        'cut_pairs': metrics['cut_pairs'],
        'cross_core_pairs': metrics['cross_core_pairs'],
        'sync_events_proxy': metrics['sync_events_proxy'],
        'pipe_conflict': pipe_conflict,
        'data_reuse_loss': max(0, metrics['estimated_copy_bytes'] - metrics['unique_cut_bytes']),
        'critical_path_cycles': cp, 'lb_comm': lb_comm, 'lb_sync': lb_sync,
        'lower_bound': lower_bound,
        'schedule_disorder': _schedule_disorder(model, candidate),
    }
    return {name: float(values[name]) for name in FEATURE_NAMES}


def _solve(matrix, vector):
    """Solve a small dense linear system with Gaussian elimination."""
    n = len(vector)
    aug = [list(row) + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            aug[pivot][col] = 1e-12
        aug[col], aug[pivot] = aug[pivot], aug[col]
        scale = aug[col][col]
        aug[col] = [x / scale for x in aug[col]]
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            if factor:
                aug[row] = [a - factor * b for a, b in zip(aug[row], aug[col])]
    return [aug[i][-1] for i in range(n)]


class HistorySurrogate:
    """Dependency-free standardized ridge model with ranking utilities."""

    def __init__(self, ridge=1.0):
        self.ridge = float(ridge)
        self.means = {}
        self.scales = {}
        self.coef = []
        self.intercept = 0.0
        self.sample_count = 0

    def fit(self, rows):
        rows = [r for r in rows if r.get('makespan') is not None]
        if not rows:
            raise ValueError('no surrogate training rows')
        self.sample_count = len(rows)
        for name in FEATURE_NAMES:
            vals = [float(r['features'][name]) for r in rows]
            mean = sum(vals) / len(vals)
            scale = math.sqrt(sum((x - mean) ** 2 for x in vals) / len(vals)) or 1.0
            self.means[name], self.scales[name] = mean, scale
        xrows = [[1.0] + [(float(r['features'][n]) - self.means[n]) / self.scales[n]
                           for n in FEATURE_NAMES] for r in rows]
        y = [math.log(max(1.0, float(r['makespan']))) for r in rows]
        dim = len(FEATURE_NAMES) + 1
        gram = [[0.0] * dim for _ in range(dim)]
        rhs = [0.0] * dim
        for x, target in zip(xrows, y):
            for i in range(dim):
                rhs[i] += x[i] * target
                for j in range(dim):
                    gram[i][j] += x[i] * x[j]
        for i in range(1, dim):
            gram[i][i] += self.ridge
        coef = _solve(gram, rhs)
        self.intercept, self.coef = coef[0], coef[1:]
        return self

    def predict(self, features):
        if not self.coef:
            raise RuntimeError('surrogate is not fitted')
        value = self.intercept + sum(weight *
            ((float(features[name]) - self.means[name]) / self.scales[name])
            for name, weight in zip(FEATURE_NAMES, self.coef))
        return math.exp(value)

    def rank(self, candidates, model, problem, cores, *, top_k=10,
             exploration=2, ddr_bytes_per_cycle=64.0, sync_cycles=500.0):
        scored = []
        for candidate in candidates:
            features = feature_dict(model, candidate, problem, cores,
                                     ddr_bytes_per_cycle=ddr_bytes_per_cycle,
                                     sync_cycles=sync_cycles)
            predicted = self.predict(features)
            lower_bound = features['lower_bound']
            scored.append((predicted, lower_bound, candidate, features))
        scored.sort(key=lambda item: (item[0], repr(candidate_signature(model, item[2]))))
        chosen = []
        seen_partitions = set()
        for item in scored:
            sig = partition_signature(model, item[2].node_to_subgraph)
            if sig in seen_partitions:
                continue
            chosen.append(item)
            seen_partitions.add(sig)
            if len(chosen) >= top_k:
                break
        # Exploration is selected from the lowest-prediction candidates whose
        # partition was not already represented, reserving structural variety.
        if exploration:
            for item in reversed(scored):
                sig = partition_signature(model, item[2].node_to_subgraph)
                if sig in seen_partitions:
                    continue
                chosen.append(item)
                seen_partitions.add(sig)
                if len(chosen) >= top_k + exploration:
                    break
        return chosen

    def to_dict(self):
        return {'type': 'standardized_ridge_log_makespan', 'ridge': self.ridge,
                'feature_names': list(FEATURE_NAMES), 'means': self.means,
                'scales': self.scales, 'intercept': self.intercept,
                'coef': self.coef, 'sample_count': self.sample_count}

    @classmethod
    def from_dict(cls, data):
        model = cls(data.get('ridge', 1.0)); model.means = data['means']
        model.scales = data['scales']; model.intercept = data['intercept']
        model.coef = data['coef']; model.sample_count = data.get('sample_count', 0)
        return model
