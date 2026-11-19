"""Persistent JSON cache for Stage 2 official evaluations and oracles."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..candidate import candidate_signature


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


class EvaluationCache:
    def __init__(self, path, graph, config_path, version='stage2-v1'):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.graph_digest = hashlib.sha256(
            json.dumps(graph, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.config_digest = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
        self.version = version
        try:
            self.data = json.loads(self.path.read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError):
            self.data = {}

    def key(self, model, candidate, problem, cores, kind='evaluator'):
        return _digest({
            'graph': self.graph_digest, 'config': self.config_digest,
            'version': self.version, 'kind': kind,
            'problem': problem, 'cores': cores,
            'candidate': repr(candidate_signature(model, candidate)),
        })

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value

    def save(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False,
                                        indent=2), encoding='utf-8')
