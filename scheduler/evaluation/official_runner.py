"""Subprocess wrapper around the unmodified official evaluator CLIs."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory


@dataclass
class EvaluationResult:
    makespan: int
    added_copy_bytes: int
    cache_hit_rate: float | None
    runtime: float
    candidate_name: str
    params: dict
    raw: dict


class OfficialRunner:
    def __init__(self, project_root=None, config_path=None):
        self.root = Path(project_root or Path(__file__).resolve().parents[2])
        self.config = Path(config_path or self.root / 'data' / 'config.txt')

    def evaluate(self, graph_path, candidate, problem, label=None,
                 timeout=None):
        graph_path = Path(graph_path)
        if problem not in (1, 2, 3):
            raise ValueError('problem must be 1, 2, or 3')
        label = label or candidate.generator_name
        with TemporaryDirectory(prefix='stage1_eval_') as temp:
            temp = Path(temp)
            plan_path = temp / 'candidate.json'
            output_path = temp / 'result.json'
            plan_path.write_text(json.dumps(candidate.to_plan(), indent=2),
                                 encoding='utf-8')
            script = self.root / 'code' / f'multicore_cut_evaluate_problem_{problem}.py'
            command = [sys.executable, str(script), str(graph_path),
                       str(plan_path), '--config', str(self.config),
                       '--output', str(output_path), '--trace-output',
                       str(temp / 'trace.json'), '--log-output',
                       str(temp / 'log.txt')]
            started = time.perf_counter()
            completed = subprocess.run(
                command, cwd=self.root, text=True, capture_output=True,
                timeout=timeout, check=False)
            runtime = time.perf_counter() - started
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise RuntimeError(
                    f'official evaluator problem {problem} failed: {detail}')
            result = json.loads(output_path.read_text(encoding='utf-8'))
        movement = result.get('data_movement_bytes') or {}
        cache = result.get('cache_stats') or {}
        accesses = cache.get('accesses', 0)
        hits = cache.get('hits', 0)
        return EvaluationResult(
            makespan=int(result['makespan']),
            added_copy_bytes=int(movement.get('added_copy_bytes', 0)),
            cache_hit_rate=(hits / accesses if accesses else None),
            runtime=runtime,
            candidate_name=label,
            params=dict(candidate.params), raw=result)

    def evaluate_singlecore(self, graph_path, timeout=None):
        """Run the repository's official single-core evaluator once."""
        graph_path = Path(graph_path)
        with TemporaryDirectory(prefix='stage2_singlecore_') as temp:
            temp = Path(temp)
            output_path = temp / 'result.json'
            script = self.root / 'code' / 'singlecore_evaluate.py'
            command = [sys.executable, str(script), str(graph_path),
                       '--config', str(self.config), '-o', str(output_path),
                       '--trace-output', str(temp / 'trace.json'),
                       '--log-output', str(temp / 'log.txt')]
            started = time.perf_counter()
            completed = subprocess.run(command, cwd=self.root, text=True,
                                       capture_output=True, timeout=timeout,
                                       check=False)
            runtime = time.perf_counter() - started
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise RuntimeError(f'official single-core evaluator failed: {detail}')
            result = json.loads(output_path.read_text(encoding='utf-8'))
        movement = result.get('data_movement_bytes') or {}
        return EvaluationResult(
            makespan=int(result['makespan']),
            added_copy_bytes=int(movement.get('added_copy_bytes', 0)),
            cache_hit_rate=None, runtime=runtime,
            candidate_name='singlecore', params={}, raw=result)
