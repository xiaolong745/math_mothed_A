import json
import unittest
from pathlib import Path

from scheduler.candidate import validate_candidate
from scheduler.generators.band_comp import generate as band_generate
from scheduler.generators.region_grow import generate as region_generate
from scheduler.generators.region_grow import _grow_once
from scheduler.generators.topological_segment import generate as segment_generate
from scheduler.graph_model import build_graph_model
from scheduler.search.portfolio import build_candidates
from scheduler.candidate import candidate_signature, partition_signature
from scheduler.refinement import (_quotient_acyclic, _same_core_reorders,
                                  partition_metrics, pi_reorder_candidates,
                                  refine_candidate)
from scheduler.evaluation.pi_attribution import attribute
from scheduler.audit_stage2 import candidate_record, controlled_variants


def tiny_graph():
    return {
        'tensors': [
            {'id': 100, 'pos': 'DDR', 'size': 32},
            {'id': 101, 'pos': 'UB', 'size': 64},
            {'id': 102, 'pos': 'UB', 'size': 16},
            {'id': 103, 'pos': 'DDR', 'size': 16},
        ],
        'ops': [
            {'id': 1, 'op': 'COPY_IN', 'pipe': 'PIPE_MTE2', 'cycles': 1},
            {'id': 2, 'op': 'MATMUL', 'pipe': 'PIPE_M', 'cycles': 10},
            {'id': 3, 'op': 'RELU', 'pipe': 'PIPE_V', 'cycles': 5},
            {'id': 4, 'op': 'COPY_OUT', 'pipe': 'PIPE_MTE3', 'cycles': 1},
        ],
        'edges': [
            {'source': 100, 'target': 1},
            {'source': 1, 'target': 101},
            {'source': 101, 'target': 2},
            {'source': 2, 'target': 102},
            {'source': 102, 'target': 3},
            {'source': 3, 'target': 103},
            {'source': 3, 'target': 4},
        ],
    }


class Stage1Tests(unittest.TestCase):
    def test_region_grow_reseed_fills_exhausted_frontier(self):
        graph = tiny_graph()
        # Add a disconnected compute op.  With a small target cluster the
        # frontier is exhausted; reseeding must still cover the remaining op.
        graph['ops'].append({'id': 5, 'op': 'ADD', 'pipe': 'PIPE_V',
                             'cycles': 3})
        model = build_graph_model(graph)
        old = _grow_once(model, 3, 0, 1.0, 0.01, reseed=False)
        new = _grow_once(model, 3, 0, 1.0, 0.01, reseed=True,
                         balance='mv')
        self.assertEqual(set(old), set(model.ops))
        self.assertEqual(set(new), set(model.ops))
        self.assertLessEqual(max(new.values()) + 1, len(model.ops))
        candidate = region_generate(model, 2, 'A', {
            'max_ops_per_sg': 3, 'reseed': True, 'balance': 'mv'})
        self.assertEqual(validate_candidate(model, candidate), (True, ''))

    def test_wavefront_sticky_candidate_is_legal(self):
        model = build_graph_model(tiny_graph())
        candidate = band_generate(model, 2, 'A', {
            'band_width': 1, 'sticky': 1.0, 'wavefront': True})
        self.assertEqual(candidate.generator_name, 'wavefront_sticky')
        self.assertEqual(validate_candidate(model, candidate), (True, ''))

    def test_copy_nodes_are_contracted(self):
        model = build_graph_model(tiny_graph())
        self.assertEqual(model.op_ids, [2, 3])
        self.assertEqual(model.topo_order, [2, 3])
        self.assertEqual([(e.src, e.dst, e.tensor_size)
                          for e in model.edges], [(2, 3, 16)])
        self.assertEqual(model.ops[2].pipe_m_cycles, 10)
        self.assertEqual(model.ops[3].pipe_v_cycles, 5)

    def test_generators_return_legal_candidates(self):
        model = build_graph_model(tiny_graph())
        for generator in (region_generate, band_generate):
            candidate = generator(model, 2, 'A', {'max_ops_per_sg': 2,
                                                  'band_width': 1})
            self.assertEqual(validate_candidate(model, candidate), (True, ''))
        candidate = segment_generate(model, 2, 'A', {'segment_size': 1})
        self.assertEqual(validate_candidate(model, candidate), (True, ''))

    def test_portfolio_has_baseline_and_structural_candidates(self):
        model = build_graph_model(tiny_graph())
        candidates = build_candidates(model, 2, 'B', max_candidates=8)
        self.assertTrue(candidates)
        self.assertEqual(candidates[0].generator_name, 'simple')
        self.assertTrue(all(validate_candidate(model, item)[0]
                            for item in candidates))

    def test_official_case_model_is_buildable(self):
        root = Path(__file__).resolve().parents[1]
        graph_path = root / 'data' / 'case_001.json'
        graph = json.loads(graph_path.read_text(encoding='utf-8'))
        model = build_graph_model(graph)
        self.assertEqual(len(model.ops), 600)
        candidate = build_candidates(model, 2, 'A', max_candidates=4)[0]
        self.assertTrue(validate_candidate(model, candidate)[0])

    def test_partition_signatures_ignore_labels_but_keep_schedule(self):
        model = build_graph_model(tiny_graph())
        candidate = build_candidates(model, 2, 'A', max_candidates=2)[0]
        remapped = type(candidate)(
            {node: group + 9 for node, group in candidate.node_to_subgraph.items()},
            [[group + 9 for group in order] for order in candidate.core_schedules],
            'remapped')
        self.assertEqual(partition_signature(model, candidate.node_to_subgraph),
                         partition_signature(model, remapped.node_to_subgraph))
        self.assertEqual(candidate_signature(model, candidate),
                         candidate_signature(model, remapped))

    def test_cycle_rejection_and_refinement_lock(self):
        model = build_graph_model(tiny_graph())
        candidate = build_candidates(model, 2, 'A', max_candidates=2)[0]
        mapping = dict(candidate.node_to_subgraph)
        mapping[3] = mapping[2]
        self.assertTrue(_quotient_acyclic(model, mapping))
        refined = refine_candidate(model, candidate, 2, max_moves=4,
                                   checkpoint_limit=4, enable_chains=True)
        self.assertTrue(refined['candidates'])
        self.assertTrue(all(validate_candidate(model, item)[0]
                            for item in refined['candidates']))

    def test_tensor_metrics_distinguish_unique_bytes_and_fanout(self):
        model = build_graph_model(tiny_graph())
        candidate = type('CandidateView', (), {
            'core_schedules': [[0], [1]],
        })()
        metrics = partition_metrics(model, {2: 0, 3: 1}, candidate)
        self.assertEqual(metrics['unique_cut_bytes'], 16)
        self.assertEqual(metrics['estimated_copy_bytes'], 16)
        self.assertEqual(metrics['cross_core_pairs'], 1)

    def test_adjacent_reorder_keeps_dependencies_legal(self):
        model = build_graph_model(tiny_graph())
        candidate = type('CandidateView', (), {
            'node_to_subgraph': {2: 0, 3: 1},
            'core_schedules': [[0, 1], []],
            'generator_name': 'test', 'params': {}, 'metadata': {},
        })()
        # The only adjacent swap reverses the 2 -> 3 quotient edge and must be
        # rejected by the existing candidate validator.
        self.assertEqual(_same_core_reorders(model, candidate), [])

    def test_pi_neighbourhood_keeps_partition_and_core_assignment(self):
        model = build_graph_model(tiny_graph())
        candidate = type('CandidateView', (), {
            'node_to_subgraph': {2: 0, 3: 1},
            'core_schedules': [[0], [1]],
            'generator_name': 'test', 'params': {}, 'metadata': {},
        })()
        # No same-core pair exists, so the bounded Pi neighbourhood is empty.
        self.assertEqual(pi_reorder_candidates(model, candidate), [])

    def test_pi_attribution_leaves_unpublished_critical_path_null(self):
        before = {'makespan': 20, 'per_core_timeline': [
            {'core_id': 0, 'ops': [
                {'op_id': 1, 'pipe': 'PIPE_M', 'start': 0, 'end': 10},
                {'op_id': 2, 'pipe': 'PIPE_V', 'start': 0, 'end': 8},
            ]}]}
        after = {'makespan': 18, 'per_core_timeline': [
            {'core_id': 0, 'ops': [
                {'op_id': 1, 'pipe': 'PIPE_M', 'start': 0, 'end': 9},
                {'op_id': 2, 'pipe': 'PIPE_V', 'start': 0, 'end': 8},
            ]}]}
        result = attribute(before, after)
        self.assertEqual(result['makespan_improvement'], 2)
        self.assertIsNone(result['critical_path_length'])

    def test_candidate_audit_separates_plan_layers(self):
        model = build_graph_model(tiny_graph())
        candidate = build_candidates(model, 2, 'A', max_candidates=2)[0]
        record = candidate_record(model, candidate, 'test')
        self.assertTrue(record['legal'])
        self.assertIn('partition_signature', record)
        self.assertIn('core_assignment_signature', record)
        self.assertIn('core_schedule_signature', record)
        probes = controlled_variants(model, candidate, limit=8)
        self.assertTrue(probes)
        self.assertTrue(all(item['legal'] for item in probes))


if __name__ == '__main__':
    unittest.main()
