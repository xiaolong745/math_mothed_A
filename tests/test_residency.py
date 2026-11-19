import unittest
from copy import deepcopy

from scheduler.candidate import validate_candidate
from scheduler.graph_model import build_graph_model
from scheduler.residency import (tensor_pages, macro_partition, macro_graph,
                                  residency_schedule, footprint)
from tools.run_residency import rank


def fixture():
    return {'ops': [
        {'id': 1, 'op': 'COPY_IN', 'pipe': 'PIPE_MTE2', 'cycles': 1},
        *[{'id': n, 'op': 'COMPUTE', 'pipe': 'PIPE_M' if n % 2 else 'PIPE_V',
           'cycles': 10} for n in range(2, 7)]],
        'tensors': [{'id': 100, 'pos': 'DDR', 'size': 16},
                    {'id': 101, 'pos': 'UB', 'size': 16},
                    {'id': 102, 'pos': 'L1', 'size': 32},
                    {'id': 103, 'pos': 'UB', 'size': 8}],
        'edges': [{'source': a, 'target': b} for a, b in [
            (100, 1), (1, 101), (101, 2), (101, 3), (101, 4),
            (2, 102), (102, 5), (102, 6), (2, 103), (103, 5), (3, 6)]]}


class ResidencyTests(unittest.TestCase):
    def setUp(self):
        self.raw = fixture()
        self.model = build_graph_model(self.raw)
        self.pages = tensor_pages(self.raw)
        self.cap = {'L1': 64, 'UB': 64}

    def test_raw_copy_fanout_not_cache_key(self):
        self.assertEqual(self.pages[100].raw_consumers, {1})
        self.assertEqual(self.pages[100].consumers, set())
        self.assertEqual(self.pages[101].consumers, {2, 3, 4})
        self.assertEqual(self.pages[101].logical_key, 101)
        self.assertTrue(self.pages[101].input_only)

    def test_parallel_tensors_preserved_and_deduplicated(self):
        mapping = {n: n for n in self.model.ops}
        records, nets, *_ = macro_graph(self.model, mapping, self.pages)
        # Two tensors connect the same compute pair; neither may disappear.
        self.assertEqual(set(records[5]['input_tensors']), {102, 103})
        shared = next(n for n in nets if n['tensor_id'] == 101)
        self.assertEqual(shared['consumer_blocks'], [2, 3, 4])
        self.assertEqual(footprint([101, 101], self.pages)['UB'], 16)

    def test_macros_cover_once_preserve_zero_dependencies_and_input(self):
        before = deepcopy(self.raw)
        for size in (1, 2, 8):
            mapping = macro_partition(self.model, self.pages, self.cap, size)
            self.assertEqual(set(mapping), set(self.model.ops))
            self.assertTrue(all(mapping[e.src] <= mapping[e.dst]
                                for e in self.model.edges))
            for p in (1, 2, 3):
                c = residency_schedule(self.model, mapping, self.pages, 3, p,
                                        self.cap, 60)
                self.assertEqual(validate_candidate(self.model, c), (True, ''))
                self.assertEqual(sum(c.metadata['core_load_m']), 20)
                self.assertEqual(sum(c.metadata['core_load_v']), 30)
        self.assertEqual(self.raw, before)

    def test_window_keeps_pc_and_joint_task_order_acyclic(self):
        mapping = {n: n for n in self.model.ops}
        fixed = {n: n % 2 for n in self.model.ops}
        c = residency_schedule(self.model, mapping, self.pages, 2, 3, self.cap,
                                60, fixed_core=fixed)
        self.assertEqual(c.node_to_subgraph, mapping)
        self.assertEqual({sg: core for core, order in enumerate(c.core_schedules)
                          for sg in order}, fixed)
        # Check dependency DAG plus core-order edges, not only local edges.
        succ = {n: set(o.successors) for n, o in self.model.ops.items()}
        for seq in c.core_schedules:
            for a, b in zip(seq, seq[1:]):
                succ[a].add(b)
        remaining = set(succ)
        while remaining:
            ready = remaining - {b for a in remaining for b in succ[a]}
            self.assertTrue(ready)
            remaining -= ready

    def test_oversized_singleton_not_removed_or_reported_as_spill(self):
        cap = {'L1': 1, 'UB': 1}
        m = macro_partition(self.model, self.pages, cap, 32)
        self.assertEqual(len(set(m.values())), len(self.model.ops))
        c = residency_schedule(self.model, m, self.pages, 2, 2, cap, 60)
        self.assertNotIn('spill_bytes', c.metadata)
        self.assertEqual(set(c.node_to_subgraph), set(self.model.ops))

    def test_multi_producer_tensor_keeps_all_owners(self):
        self.raw['edges'].append({'source': 3, 'target': 102})
        pages = tensor_pages(self.raw)
        model = build_graph_model(self.raw)
        self.assertEqual(pages[102].producers, {2, 3})
        mapping = {n: n for n in model.ops}
        fixed = {n: n % 2 for n in model.ops}
        c = residency_schedule(model, mapping, pages, 2, 2, self.cap, 60,
                                fixed_core=fixed)
        self.assertEqual(c.metadata['owner_cores_proxy'][102], [0, 1])

    def test_elite_never_prefers_slower_less_spill_or_cache_gain(self):
        base = {'makespan': 10, 'added_copy_bytes': 100, 'cache_hit_rate': 0}
        slow = {'makespan': 11, 'added_copy_bytes': 0, 'cache_hit_rate': 1}
        tie = {'makespan': 10, 'added_copy_bytes': 90, 'cache_hit_rate': 0}
        for p in (1, 2, 3):
            self.assertLess(rank(base, p), rank(slow, p))
            self.assertLess(rank(tie, p), rank(base, p))


if __name__ == '__main__':
    unittest.main()
