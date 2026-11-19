"""One-op-per-subgraph baseline."""

from ..schedulers.heft_lite import assign_cores


def generate(graph, num_cores, scene='A', params=None):
    mapping = {op_id: i for i, op_id in enumerate(graph.topo_order)}
    return assign_cores(graph, mapping, num_cores, scene,
                        generator_name='simple', params=params)
