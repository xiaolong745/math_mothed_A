"""Official Step1/Step2 spill oracle adapter.

This module deliberately calls the same private Task builders used by the
official evaluator.  It reports only metrics that those builders expose; it
does not infer global parallel peaks from a serial task trace.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path


class SpillOracle:
    def __init__(self, project_root=None, config_path=None):
        self.root = Path(project_root or Path(__file__).resolve().parents[2])
        self.config_path = Path(config_path or self.root / 'data' / 'config.txt')
        code = str(self.root / 'code')
        if code not in sys.path:
            sys.path.insert(0, code)
        from evaluation_validation import read_evaluation_config
        self.settings = read_evaluation_config(str(self.config_path))
        from multicore_cut_evaluate_problem_1 import read_scene_a_config
        from multicore_cut_evaluate_problem_2 import read_scene_b_config
        self.scene_a = read_scene_a_config(str(self.config_path))
        self.scene_b = read_scene_b_config(str(self.config_path))

    def _build(self, graph, plan, problem):
        capacity = self.settings['capacity']
        bandwidth = self.settings['bandwidth']
        if problem == 1:
            from multicore_cut_evaluate_problem_1 import _build_scene_a_tasks
            return _build_scene_a_tasks(graph, plan, bandwidth, capacity)
        if problem in (2, 3):
            if problem == 2:
                from multicore_cut_evaluate_problem_2 import _build_scene_b_tasks
            else:
                from multicore_cut_evaluate_problem_3 import _build_scene_b_tasks
            return _build_scene_b_tasks(graph, plan, bandwidth, capacity)
        raise ValueError('problem must be 1, 2, or 3')

    def inspect(self, graph, candidate, problem):
        started = time.perf_counter()
        try:
            built = self._build(graph, candidate.to_plan(), problem)
            # P1: tasks, cross traffic, movement, plan view.
            # P2/P3: tasks, cross links, cross traffic, movement, plan view.
            tasks = built[0]
            movement = built[2] if problem == 1 else built[3]
            per_task = []
            peak_l1 = []
            peak_ub = []
            for task_id, task in sorted(tasks.items()):
                step3 = task.get('step3', {})
                memory_peak = step3.get('memory_peak', {})
                peak_l1.append(memory_peak.get('L1'))
                peak_ub.append(memory_peak.get('UB'))
                per_task.append({
                    'task_id': task_id,
                    'spill_in_bytes': None,
                    'spill_out_bytes': None,
                    'total_spill_bytes': None,
                    'step2_spill_bytes': None,
                    'memory_peak': memory_peak or None,
                })
            # The official builders expose a reliable aggregate distinction:
            # spill_added_copy_bytes is capacity spill, while partition_added
            # is boundary/original COPY traffic.  Per-task spill records are
            # consumed before this return and are therefore intentionally null.
            spill = int(movement.get('spill_added_copy_bytes', 0))
            return {
                'valid': True,
                'spill_in_bytes': None,
                'spill_out_bytes': None,
                'total_spill_bytes': spill,
                'partition_copy_bytes': movement.get('partition_added_copy_bytes'),
                'per_task_metrics': per_task,
                'failed_tasks': [],
                'oracle_runtime': time.perf_counter() - started,
                'source': 'official_task_builder_step1_step2',
                'peak_l1_bytes': None,
                'peak_ub_bytes': None,
                'peak_note': 'per-task Step3 peaks are available; global parallel peak is not inferred',
                'task_count': len(tasks),
            }
        except Exception as exc:  # official errors are candidate-local
            return {
                'valid': False,
                'spill_in_bytes': None,
                'spill_out_bytes': None,
                'total_spill_bytes': None,
                'partition_copy_bytes': None,
                'per_task_metrics': [],
                'failed_tasks': [str(exc)],
                'oracle_runtime': time.perf_counter() - started,
                'source': 'official_task_builder_step1_step2',
                'peak_l1_bytes': None,
                'peak_ub_bytes': None,
                'peak_note': None,
                'task_count': None,
            }
