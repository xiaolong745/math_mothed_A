"""Verify official code, config and 100 graphs against historical SHA-256s."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify(root=ROOT):
    manifest = json.loads((root / 'results/full5_20260927/manifest.json').read_text())
    hashes = {name: expected for name, expected in manifest['hashes'].items()
              if name.startswith('code/') or name == 'data/config.txt'
              or (name.startswith('data/case_') and name.endswith('.json'))}
    graphs = [name for name in hashes if name.startswith('data/case_')]
    if len(graphs) != 100:
        raise ValueError('Expected 100 input graph hashes')
    for name, expected in hashes.items():
        actual = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError('Historical hash mismatch: ' + name)
    return {'verified_files': len(hashes), 'graphs': len(graphs),
            'source': 'results/full5_20260927/manifest.json',
            'official_files_changed': False}


if __name__ == '__main__':
    print(json.dumps(verify(), indent=2))
