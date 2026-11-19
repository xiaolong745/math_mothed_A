"""Continuous topological-segment partition candidate."""

from ..schedulers.heft_lite import assign_cores


def generate(graph, num_cores, scene='A', params=None):
    """Keep consecutive topological work together, then schedule the segments."""
    params = dict(params or {})
    default_size = max(2, len(graph.ops) // max(1, num_cores * 3))
    segment_size = max(1, int(params.get('segment_size', default_size)))
    mapping = {}
    segment = 0
    for offset in range(0, len(graph.topo_order), segment_size):
        for node in graph.topo_order[offset:offset + segment_size]:
            mapping[node] = segment
        segment += 1
    params['segment_size'] = segment_size
    return assign_cores(graph, mapping, num_cores, scene,
                        generator_name='topological_segment', params=params)
