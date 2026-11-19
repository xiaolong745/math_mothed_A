"""Candidate data and legality checks."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Candidate:
    node_to_subgraph: dict[int, int]
    core_schedules: list[list[int]]
    generator_name: str
    params: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_plan(self) -> dict[str, Any]:
        return {
            'node_to_subgraph': {str(k): int(v)
                                 for k, v in self.node_to_subgraph.items()},
            'core_schedules': [list(map(int, order))
                               for order in self.core_schedules],
        }


def canonicalize_mapping(model, mapping):
    """Rename partition ids by first topological occurrence and drop empties."""
    order = {node: i for i, node in enumerate(model.topo_order)}
    groups = {}
    for node, group in mapping.items():
        groups.setdefault(group, []).append(node)
    old_order = sorted(groups, key=lambda group: min(order[n] for n in groups[group]))
    remap = {old: new for new, old in enumerate(old_order)}
    return {node: remap[group] for node, group in mapping.items()}


def partition_signature(model, mapping):
    """Unlabelled partition signature; core assignment/order is excluded."""
    normalized = canonicalize_mapping(model, mapping)
    groups = {}
    for node, group in normalized.items():
        groups.setdefault(group, []).append(node)
    return tuple(tuple(sorted(nodes)) for _, nodes in sorted(groups.items()))


def candidate_signature(model, candidate):
    """Full P,C,Pi signature after canonical subgraph renumbering."""
    normalized = canonicalize_mapping(model, candidate.node_to_subgraph)
    old_to_new = {
        old: normalized[next(node for node, group in candidate.node_to_subgraph.items()
                            if group == old)]
        for old in set(candidate.node_to_subgraph.values())
    }
    schedules = tuple(tuple(old_to_new[group] for group in order)
                      for order in candidate.core_schedules)
    return (tuple(sorted(normalized.items())), schedules)


def validate_candidate(model, candidate: Candidate) -> tuple[bool, str]:
    """Validate coverage, quotient acyclicity and per-core order."""
    ids = set(model.ops)
    mapping = candidate.node_to_subgraph
    if set(mapping) != ids:
        return False, 'node coverage mismatch'
    if not candidate.core_schedules:
        return False, 'empty core schedules'
    if any(type(sg) is not int or sg < 0 for sg in mapping.values()):
        return False, 'invalid subgraph id'
    subgraphs = set(mapping.values())
    scheduled = [sg for order in candidate.core_schedules for sg in order]
    if set(scheduled) != subgraphs or len(scheduled) != len(set(scheduled)):
        return False, 'subgraph schedules do not cover exactly once'
    quotient = {sg: set() for sg in subgraphs}
    for edge in model.edges:
        a, b = mapping[edge.src], mapping[edge.dst]
        if a != b:
            quotient[a].add(b)
    indegree = {sg: 0 for sg in subgraphs}
    for a in quotient:
        for b in quotient[a]:
            indegree[b] += 1
    ready = sorted(sg for sg, d in indegree.items() if d == 0)
    seen = []
    while ready:
        sg = ready.pop(0)
        seen.append(sg)
        for nxt in sorted(quotient[sg]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    if len(seen) != len(subgraphs):
        return False, 'subgraph dependency cycle'
    core_of = {sg: core for core, order in enumerate(candidate.core_schedules)
               for sg in order}
    positions = {core: {sg: i for i, sg in enumerate(order)}
                 for core, order in enumerate(candidate.core_schedules)}
    for src, targets in quotient.items():
        for dst in targets:
            if (core_of[src] == core_of[dst]
                    and positions[core_of[src]][src] >= positions[core_of[dst]][dst]):
                return False, 'same-core dependency order violation'
    return True, ''
