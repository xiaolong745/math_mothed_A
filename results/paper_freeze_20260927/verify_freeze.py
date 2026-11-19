"""Read-only artifact verification; does not import scheduling code."""
import hashlib
import json
from pathlib import Path
import tarfile

root = Path(__file__).resolve().parent
manifest = json.loads((root / 'freeze_manifest.json').read_text())
def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()
for name, expected in manifest['artifacts'].items():
    if digest(root / name) != expected:
        raise SystemExit('Artifact mismatch: ' + name)
with tarfile.open(root / 'snapshot.tar.gz', 'r:gz') as archive:
    names = [item.name for item in archive if item.isfile()]
    if len(names) != len(set(names)) or set(names) != set(manifest['members']):
        raise SystemExit('Archive membership mismatch')
    for name, expected in manifest['members'].items():
        if hashlib.sha256(archive.extractfile(name).read()).hexdigest() != expected:
            raise SystemExit('Archive member mismatch: ' + name)
print('Verified frozen release: %d members, %d artifacts. No experiments run.' % (
    len(manifest['members']), len(manifest['artifacts'])))
