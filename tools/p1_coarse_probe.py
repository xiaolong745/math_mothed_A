"""Controlled P1 probe: merge adjacent same-core subgraphs of verified plans."""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from scheduler.candidate import Candidate, validate_candidate
from scheduler.evaluation.official_runner import OfficialRunner
from scheduler.graph_model import build_graph_model


def merged_candidate(plan, factor):
    old_mapping = {int(k): int(v) for k, v in plan['node_to_subgraph'].items()}
    old_schedules = [[int(v) for v in order] for order in plan['core_schedules']]
    remap = {}
    new_schedules = []
    next_group = 0
    for order in old_schedules:
        local = []
        for start in range(0, len(order), factor):
            chunk = order[start:start + factor]
            for old in chunk:
                remap[old] = next_group
            local.append(next_group)
            next_group += 1
        new_schedules.append(local)
    mapping = {node: remap[group] for node, group in old_mapping.items()}
    return Candidate(mapping, new_schedules, f'p1_coarse_x{factor}', {'merge_factor': factor}, {})


def merged_pair(plan, core_index, position):
    """Merge one adjacent pair; retain all other groups and core order."""
    old_mapping = {int(k): int(v) for k, v in plan['node_to_subgraph'].items()}
    schedules = [[int(v) for v in order] for order in plan['core_schedules']]
    order = schedules[core_index]
    left, right = order[position], order[position + 1]
    replacement = min(left, right)
    remap = {old: (replacement if old == right else old)
             for old in set(old_mapping.values())}
    # Compact ids in first occurrence order after the merge.
    merged_schedules = []
    seen = set()
    for ci, current in enumerate(schedules):
        new_order = []
        for old in current:
            value = remap[old]
            if value not in seen:
                seen.add(value)
                new_order.append(value)
        merged_schedules.append(new_order)
    ids = {old: i for i, old in enumerate(seen)}
    mapping = {node: ids[remap[group]] for node, group in old_mapping.items()}
    new_schedules = [[ids[remap[group]] for group in current]
                     for current in merged_schedules]
    return Candidate(mapping, new_schedules, 'p1_coarse_pair',
                     {'core': core_index, 'position': position}, {})


def one(root, case, factor, timeout):
    result_dir = root / 'results/full5_20260927/cells' / f'{case}_p1_k5'
    incumbent = json.loads((result_dir / 'verified_incumbent.json').read_text())
    graph_path = root / 'data' / f'{case}.json'
    graph_json = json.loads(graph_path.read_text())
    model = build_graph_model(graph_json)
    candidate = merged_candidate(incumbent['plan'], factor)
    legal, reason = validate_candidate(model, candidate)
    row = {'case': case, 'merge_factor': factor,
           'base_makespan': incumbent['makespan'], 'base_generator': incumbent.get('generator_name'),
           'candidate_subgraphs': len(set(candidate.node_to_subgraph.values())),
           'legal': legal, 'validation_error': reason}
    if not legal:
        return row
    runner = OfficialRunner(root, root / 'data/config.txt')
    evaluated = runner.evaluate(graph_path, candidate, problem=1,
                                label=candidate.generator_name, timeout=timeout)
    row.update({'probe_makespan': evaluated.makespan,
                'delta_cycles': evaluated.makespan - incumbent['makespan'],
                'relative_change': evaluated.makespan / incumbent['makespan'] - 1.0,
                'added_copy_bytes': evaluated.added_copy_bytes,
                'runtime_seconds': evaluated.runtime,
                'official_evaluated': True})
    return row


def pair_rows(root, case, limit, timeout):
    result_dir = root / 'results/full5_20260927/cells' / f'{case}_p1_k5'
    incumbent = json.loads((result_dir / 'verified_incumbent.json').read_text())
    graph_path = root / 'data' / f'{case}.json'
    model = build_graph_model(json.loads(graph_path.read_text()))
    plan = incumbent['plan']
    legal_candidates = []
    for core, order in enumerate(plan['core_schedules']):
        for position in range(max(0, len(order) - 1)):
            candidate = merged_pair(plan, core, position)
            legal, reason = validate_candidate(model, candidate)
            if legal:
                legal_candidates.append((core, position, candidate))
    rows = []
    for core, position, candidate in legal_candidates[:limit]:
        runner = OfficialRunner(root, root / 'data/config.txt')
        evaluated = runner.evaluate(graph_path, candidate, problem=1,
                                    label=candidate.generator_name, timeout=timeout)
        rows.append({'case': case, 'merge_factor': 2, 'pair_core': core,
                     'pair_position': position, 'base_makespan': incumbent['makespan'],
                     'candidate_subgraphs': len(set(candidate.node_to_subgraph.values())),
                     'legal': True, 'probe_makespan': evaluated.makespan,
                     'delta_cycles': evaluated.makespan - incumbent['makespan'],
                     'relative_change': evaluated.makespan / incumbent['makespan'] - 1.0,
                     'added_copy_bytes': evaluated.added_copy_bytes,
                     'runtime_seconds': evaluated.runtime, 'official_evaluated': True})
    return rows, len(legal_candidates)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--cases', default='case_064,case_071,case_048,case_001')
    parser.add_argument('--factors', default='2,4')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--timeout', type=float, default=300)
    parser.add_argument('--output', type=Path,
                        default=Path('results/stage3/p1_coarse_probe.json'))
    parser.add_argument('--pairs', action='store_true')
    parser.add_argument('--pair-limit', type=int, default=12)
    args = parser.parse_args()
    cases = [x for x in args.cases.split(',') if x]
    factors = [int(x) for x in args.factors.split(',') if x]
    rows = []
    if args.pairs:
        for case in cases:
            pair_result, legal_count = pair_rows(args.root, case, args.pair_limit, args.timeout)
            rows.extend(pair_result)
            print(json.dumps({'case': case, 'legal_pair_count': legal_count,
                              'evaluated_pair_count': len(pair_result)}, ensure_ascii=False), flush=True)
        rows.sort(key=lambda row: (row['case'], row.get('pair_core', -1), row.get('pair_position', -1)))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(one, args.root, case, factor, args.timeout)
                   for case in cases for factor in factors]
        for future in as_completed(futures):
            rows.append(future.result())
            print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
    rows.sort(key=lambda row: (row['case'], row['merge_factor']))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
