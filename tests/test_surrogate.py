import json
import unittest
from pathlib import Path

from scheduler.evaluation.surrogate import FEATURE_NAMES, HistorySurrogate, feature_dict
from scheduler.graph_model import build_graph_model
from scheduler.search.portfolio import build_candidates


class SurrogateTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        graph = json.loads((root / 'data/case_001.json').read_text())
        self.model = build_graph_model(graph)
        self.candidates = build_candidates(self.model, 4, 'A', max_candidates=8)

    def test_feature_schema_and_finite_values(self):
        values = feature_dict(self.model, self.candidates[0], 1, 4)
        self.assertEqual(tuple(values), FEATURE_NAMES)
        self.assertTrue(all(value == value for value in values.values()))

    def test_ridge_fit_predict_and_diverse_rank(self):
        rows = []
        for index, candidate in enumerate(self.candidates):
            rows.append({'features': feature_dict(self.model, candidate, 1, 4),
                         'makespan': 100000 + index * 1000})
        model = HistorySurrogate().fit(rows)
        ranked = model.rank(self.candidates, self.model, 1, 4, top_k=3,
                            exploration=1)
        self.assertEqual(len(ranked), 4)
        self.assertTrue(all(item[0] > 0 for item in ranked))


if __name__ == '__main__':
    unittest.main()
