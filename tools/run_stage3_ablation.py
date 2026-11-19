"""Run P/C/Pi layer ablations on representative remote cases."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler.candidate import Candidate, validate_candidate
from scheduler.evaluation.official_runner import OfficialRunner
from scheduler.graph_model import build_graph_model
from scheduler.refinement import (_same_core_reorders, refine_candidate)
from scheduler.schedulers.heft_lite import assign_cores


def _candidate(item):
    plan = item['plan']
    return Candidate({int(k): int(v) for k, v in plan['node_to_subgraph'].items()},
                     [[int(x) for x in row] for row in plan['core_schedules']],
                     item.get('generator_name', 'stage2_best'), item.get('params', {}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--remote-root', type=Path,
                        default=ROOT / 'results/stage2_remote_parallel_20260926')
    parser.add_argument('--output', type=Path,
                        default=ROOT / 'results/stage3/pcpi_ablation.csv')
    parser.add_argument('--cases', default='case_001,case_005')
    parser.add_argument('--cores', type=int, default=4)
    args = parser.parse_args()
    runner = OfficialRunner(ROOT, args.data_dir / 'config.txt')
    rows = []
    for case in [x for x in args.cases.split(',') if x]:
        graph_path = args.data_dir / f'{case}.json'
        graph = json.loads(graph_path.read_text(encoding='utf-8'))
        model = build_graph_model(graph)
        configs = json.loads((args.remote_root / case / 'best_configs.json').read_text(encoding='utf-8'))
        for problem in (1, 2, 3):
            item = configs.get(f'{case}|p{problem}|k{args.cores}')
            if not item:
                continue
            base = _candidate(item)
            variants = [('stage2_best', base)]
            try:
                # P fixed, C/Pi re-built by stable HEFT-lite.
                variants.append(('fixed_P_reassign_CPi', assign_cores(
                    model, base.node_to_subgraph, args.cores,
                    'A' if problem == 1 else 'B',
                    generator_name='ablation_reassign')))
            except Exception:
                pass
            for index, reordered in enumerate(_same_core_reorders(model, base, limit=4)):
                variants.append((f'fixed_P_C_reorder_Pi_{index}', reordered))
            refined = refine_candidate(
                model, base, args.cores, scene='A' if problem == 1 else 'B',
                max_passes=1, max_moves=8, max_negative_moves=1,
                checkpoint_limit=3, chain_limit=2, enable_chains=True,
                mode='full', problem=problem)
            for index, candidate in enumerate(refined['candidates'][:4]):
                variants.append((f'P_repartition_{index}', candidate))
            seen = set()
            for name, candidate in variants:
                legal, reason = validate_candidate(model, candidate)
                if not legal:
                    rows.append({'case': case, 'problem': problem, 'cores': args.cores,
                                 'variant': name, 'legal': False, 'makespan': None,
                                 'error': reason})
                    continue
                signature = repr((tuple(sorted(candidate.node_to_subgraph.items())),
                                  tuple(tuple(x) for x in candidate.core_schedules)))
                if signature in seen:
                    continue
                seen.add(signature)
                try:
                    result = runner.evaluate(graph_path, candidate, problem,
                                             label=name, timeout=60)
                    rows.append({'case': case, 'problem': problem, 'cores': args.cores,
                                 'variant': name, 'legal': True,
                                 'makespan': result.makespan,
                                 'added_copy_bytes': result.added_copy_bytes,
                                 'cache_hit_rate': result.cache_hit_rate,
                                 'candidate_signature': signature, 'error': ''})
                except Exception as exc:
                    rows.append({'case': case, 'problem': problem, 'cores': args.cores,
                                 'variant': name, 'legal': True, 'makespan': None,
                                 'error': str(exc)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with args.output.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    print(f'wrote {len(rows)} ablation rows to {args.output}')


if __name__ == '__main__':
    main()
