"""Depth-band / weak-component candidate generator."""

from collections import defaultdict

from ..schedulers.heft_lite import assign_cores


def _components(nodes, graph):
    nodes = set(nodes)
    adj = {node: set() for node in nodes}
    for edge in graph.edges:
        if edge.src in nodes and edge.dst in nodes:
            adj[edge.src].add(edge.dst)
            adj[edge.dst].add(edge.src)
    result = []
    while nodes:
        root = min(nodes)
        stack = [root]
        nodes.remove(root)
        component = []
        while stack:
            node = stack.pop()
            component.append(node)
            for nxt in sorted(adj[node] & nodes, reverse=True):
                nodes.remove(nxt)
                stack.append(nxt)
        result.append(sorted(component))
    return result


def generate(graph, num_cores, scene='A', params=None):
    params = dict(params or {})
    width = max(1, int(params.get('band_width', 4)))
    max_ops = int(params.get('max_ops_per_sg', 0))
    max_depth = max((graph.ops[n].depth_from_source for n in graph.ops), default=0)
    bands = defaultdict(list)
    for node in graph.topo_order:
        band = graph.ops[node].depth_from_source // width
        bands[band].append(node)
    groups = []
    for band in sorted(bands):
        for component in _components(bands[band], graph):
            if max_ops > 0 and len(component) > max_ops:
                for start in range(0, len(component), max_ops):
                    groups.append(component[start:start + max_ops])
            else:
                groups.append(component)
    mapping = {}
    for sg, group in enumerate(groups):
        for node in group:
            mapping[node] = sg
    # Defensive fallback for unusual isolated nodes or empty input.
    for node in graph.topo_order:
        if node not in mapping:
            mapping[node] = len(groups)
            groups.append([node])
    params['max_depth'] = max_depth
    sticky = float(params.get('sticky', 0.0))
    if params.get('wavefront', False):
        name = 'wavefront_sticky' if sticky else 'wavefront'
    else:
        name = 'bandcomp_sticky' if sticky else 'bandcomp'
    return assign_cores(graph, mapping, num_cores, scene,
                        generator_name=name, params=params)
