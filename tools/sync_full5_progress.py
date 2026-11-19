"""Read-only remote report synchronization; never starts an evaluation."""
import argparse
import io
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
REMOTE_EXPORT = r'''
import io, pathlib, sys, tarfile
root = pathlib.Path(sys.argv[1])
names = ['manifest.json', 'state.json', 'manager.json', 'pilot_gate.json',
         'summary.csv', 'group_metrics.csv', 'report.md', 'evaluator_calls.csv',
         'worst_completed.csv', 'best_configs.json', 'manager.log']
paths = [root / n for n in names]
if sys.argv[2] == '1':
    paths += list(root.glob('baselines/*.json'))
    paths += list(root.glob('baseline_status/*/process.json'))
    paths += list(root.glob('baseline_status/*/process.log'))
    for name in ('process.json', 'process.log', 'events.jsonl', 'summary.csv',
                 'verified_incumbent.json', 'verified_incumbent_official.json'):
        paths += list(root.glob('cells/*/' + name))
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as tar:
    for path in paths:
        if not path.is_file():
            continue
        data = path.read_bytes()
        item = tarfile.TarInfo(str(path.relative_to(root)))
        item.size = len(data)
        tar.addfile(item, io.BytesIO(data))
'''


def sync(host, port, remote_dir, out, *, detail=False, timeout=90):
    if host.startswith('-') or not 1 <= port <= 65535:
        raise ValueError('Invalid SSH host or port')
    out.mkdir(parents=True, exist_ok=True)
    command = ('python3 -c ' + shlex.quote(REMOTE_EXPORT) + ' '
               + shlex.quote(remote_dir) + (' 1' if detail else ' 0'))
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                             '-p', str(port), host, command],
                            capture_output=True, timeout=timeout, check=True)
    count = 0
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode='r:gz') as tar:
        for member in tar:
            relative = Path(member.name)
            if not member.isfile() or relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Unexpected archive entry')
            destination = out / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = destination.with_name(destination.name + '.sync_tmp')
            temp.write_bytes(tar.extractfile(member).read())
            temp.replace(destination)
            count += 1
    state = json.loads((out / 'state.json').read_text())
    status = {'synced_at': time.time(), 'files': count, 'remote': remote_dir,
              'includes_detail': detail,
              'phase': state.get('phase'), 'snapshot_atomic_across_files': False}
    (out / 'sync_status.json').write_text(json.dumps(status, indent=2))
    if __package__:
        from .audit_full5_results import audit
    else:
        from audit_full5_results import audit
    if detail:
        audit(out)
    print(json.dumps(status), flush=True)
    return status['phase']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, help='SSH destination, e.g. user@host')
    parser.add_argument('--port', type=int, default=22)
    parser.add_argument('--remote-dir', required=True, help='Remote result directory')
    parser.add_argument('--output', type=Path, default=ROOT / 'results/remote_copy')
    parser.add_argument('--detail', action='store_true', help='Also download per-cell evidence')
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    while True:
        try:
            phase = sync(args.host, args.port, args.remote_dir, args.output,
                         detail=args.detail, timeout=args.timeout)
            if not args.watch or phase in ('complete', 'stopped_at_resource_gate'):
                break
        except Exception as exc:
            print(json.dumps({'sync_error': str(exc), 'at': time.time()}), flush=True)
            if not args.watch:
                raise
        time.sleep(60)
