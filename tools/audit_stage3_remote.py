"""Audit Stage 1/Stage 2 remote results and classify likely bottlenecks."""

from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
csv.field_size_limit(1024 * 1024 * 1024)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler.candidate import Candidate, candidate_signature
from scheduler.graph_model import build_graph_model
from scheduler.refinement import partition_metrics


def _rows(path):
    try:
        with path.open(newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def _critical_path(model):
    finish = {}
    for node in model.topo_order:
        finish[node] = model.ops[node].cycles + max(
            (finish[p] for p in model.ops[node].predecessors), default=0)
    return max(finish.values(), default=0)


def _raw_for(cache_path, target):
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    best = None
    for item in data.values():
        if int(item.get('makespan', -1)) == int(target):
            best = item.get('raw') or best
    return best


def _find_raw(root, case, problem, cores, target):
    candidates = [
        root / 'no_oracle' / 'cache' / f'{case}_p{problem}_k{cores}.json',
        root / 'full' / 'cache' / f'{case}_p{problem}_k{cores}.json',
    ]
    for path in candidates:
        raw = _raw_for(path, target)
        if raw is not None:
            return raw, path
    return None, None


def _candidate_metrics(graph, raw, plan=None):
    if not raw and not plan:
        return {}
    model = build_graph_model(graph)
    if plan is None:
        return {}
    mapping = {int(node): int(group)
               for node, group in plan['node_to_subgraph'].items()}
    schedules = [[int(group) for group in order]
                 for order in plan['core_schedules']]
    candidate = Candidate(mapping, schedules, 'audit')
    metrics = partition_metrics(model, mapping, candidate)
    core_of = {sg: core for core, order in enumerate(schedules)
               for sg in order}
    loads = defaultdict(lambda: [0, 0])
    for node, group in mapping.items():
        core = core_of.get(group)
        if core is None:
            continue
        loads[core][0] += model.ops[node].pipe_m_cycles
        loads[core][1] += model.ops[node].pipe_v_cycles
    mvals = [loads[i][0] for i in range(len(schedules))]
    vvals = [loads[i][1] for i in range(len(schedules))]
    metrics.update({
        'load_m_max': max(mvals, default=0),
        'load_v_max': max(vvals, default=0),
        'load_m_min': min(mvals, default=0),
        'load_v_min': min(vvals, default=0),
        'load_m_values': mvals,
        'load_v_values': vvals,
        'critical_path_cycles': _critical_path(model),
        'total_m_cycles': sum(mvals),
        'total_v_cycles': sum(vvals),
        'subgraph_count': len(set(mapping.values())),
    })
    return metrics


def _classify(row):
    if row['p2_p3_makespan_delta'] == 0 and row['problem'] == 3:
        return 'cache'
    if row['spill_added_copy_bytes'] > 0:
        return 'spill'
    k = row['cores']
    lower = max(row['total_m_cycles'] / k if k else 0,
                row['total_v_cycles'] / k if k else 0,
                row['critical_path_cycles'])
    gap = row['makespan'] / lower if lower else 0
    mv_gap = max(row['load_m_max'] / (row['total_m_cycles'] / k or 1),
                 row['load_v_max'] / (row['total_v_cycles'] / k or 1))
    if mv_gap >= 1.35:
        return 'mv_imbalance'
    if row['cross_core_transfers'] >= max(4, row['subgraph_count'] // 3):
        return 'cross_core_sync'
    if row['added_copy_bytes'] > max(4096, row['total_tensor_bytes'] * 0.02):
        return 'communication'
    if gap >= 1.8:
        return 'critical_path'
    if row['distinct_candidate_signatures'] <= 2:
        return 'search_space_insufficient'
    return 'unknown'


def _signature_layers(value):
    try:
        parsed = ast.literal_eval(value)
        partition, schedules = parsed
        return repr(partition), repr((partition, schedules))
    except (ValueError, SyntaxError, TypeError):
        return None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    parser.add_argument('--stage1-dir', type=Path, default=Path('results/stage1'))
    parser.add_argument('--remote-root', type=Path,
                        default=Path('results/stage2_remote_parallel_20260926'))
    parser.add_argument('--output-dir', type=Path, default=Path('results/stage3'))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stage1 = {(r['case'], int(r['problem']), int(r['num_cores'])): r
              for r in _rows(args.stage1_dir / 'summary.csv')}
    stage2_rows = []
    for summary in [args.remote_root / case / 'summary.csv'
                    for case in ('case_001', 'case_005', 'case_003')]:
        stage2_rows.extend(_rows(summary))
    # Also accept the single-case remote result layout.
    if not stage2_rows:
        stage2_rows = _rows(Path('results/stage2_remote_20260926/summary.csv'))
    by_combo = {}
    for row in stage2_rows:
        if row.get('status') != 'ok':
            continue
        key = (row['case'], int(row['problem']), int(row['num_cores']))
        old = by_combo.get(key)
        if old is None or float(row.get('search_runtime') or 0) >= float(old.get('search_runtime') or 0):
            by_combo[key] = row
    audit, diversity = [], []
    for (case, problem, cores), row in sorted(by_combo.items()):
        graph_path = args.data_dir / f'{case}.json'
        if not graph_path.exists():
            continue
        graph = json.loads(graph_path.read_text(encoding='utf-8'))
        model = build_graph_model(graph)
        target = int(row['makespan'])
        raw, cache_path = _find_raw(args.remote_root / case, case, problem, cores, target)
        if raw is None and case == 'case_001':
            raw, cache_path = _find_raw(Path('results/stage2_remote_20260926'), case, problem, cores, target)
        plan = None
        for config_path in (args.remote_root / case / 'best_configs.json',
                            Path('results/stage2_remote_20260926/best_configs.json')):
            if config_path.exists():
                try:
                    configs = json.loads(config_path.read_text(encoding='utf-8'))
                    item = configs.get(f'{case}|p{problem}|k{cores}')
                    if item:
                        plan = item.get('plan')
                        break
                except (OSError, ValueError):
                    pass
        metrics = _candidate_metrics(graph, raw, plan)
        calls_path = args.remote_root / case / 'evaluator_calls.csv'
        calls = [r for r in _rows(calls_path)
                 if r.get('problem') == str(problem) and r.get('num_cores') == str(cores)]
        sigs = {r.get('candidate_signature') for r in calls if r.get('candidate_signature')}
        partition_sigs = {_signature_layers(sig)[0] for sig in sigs
                          if _signature_layers(sig)[0] is not None}
        p2 = by_combo.get((case, 2, cores), {})
        p3 = by_combo.get((case, 3, cores), {})
        p2p3 = int(p3.get('makespan', target)) - int(p2.get('makespan', target))
        movement = (raw or {}).get('data_movement_bytes', {})
        transfers = len((raw or {}).get('cross_core_transfers', []))
        total_tensor_bytes = sum(int(t.get('size', 0)) for t in graph.get('tensors', []))
        out = {
            'case': case, 'problem': problem, 'cores': cores,
            'stage1_makespan': int(stage1[(case, problem, cores)]['makespan']) if (case, problem, cores) in stage1 else None,
            'stage1_baseline_source': 'official_stage1_summary' if (case, problem, cores) in stage1 else 'missing_stage1_filled_seed',
            'stage2_makespan': target, 'makespan': target,
            'stage2_improvement_vs_stage1': row.get('improvement_vs_stage1'),
            'added_copy_bytes': int(row.get('added_copy_bytes') or 0),
            'partition_added_copy_bytes': int(movement.get('partition_added_copy_bytes') or 0),
            'spill_added_copy_bytes': int(movement.get('spill_added_copy_bytes') or 0),
            'total_tensor_bytes': total_tensor_bytes,
            'cross_task_traffic': int((raw or {}).get('cross_task_traffic') or 0),
            'cross_core_transfers': transfers,
            'evaluator_calls': int(row.get('evaluator_calls') or 0),
            'evaluator_cache_hits': int(row.get('evaluator_cache_hits') or 0),
            'oracle_calls': int(row.get('oracle_calls') or 0),
            'search_runtime': row.get('search_runtime'),
            'distinct_candidate_signatures': len(sigs),
            'distinct_partition_signatures': len(partition_sigs),
            'p2_p3_makespan_delta': p2p3,
            'raw_available': bool(raw),
            **{key: value for key, value in metrics.items()
               if key not in ('load_m_values', 'load_v_values')},
            'mv_load_m': json.dumps(metrics.get('load_m_values', [])),
            'mv_load_v': json.dumps(metrics.get('load_v_values', [])),
        }
        out['diagnosis'] = _classify(out)
        audit.append(out)
        diversity.append({
            'case': case, 'problem': problem, 'cores': cores,
            'candidate_count': row.get('candidate_count'),
            'selected_count': row.get('selected_count'),
            'evaluator_signatures': len(sigs),
            'partition_signatures': len(partition_sigs),
            'same_makespan_count': sum(1 for r in calls if r.get('makespan') == str(target)),
        })
    fields = sorted({key for row in audit for key in row})
    with (args.output_dir / 'remote_audit.csv').open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(audit)
    with (args.output_dir / 'candidate_diversity.csv').open('w', newline='', encoding='utf-8') as fh:
        fields = sorted({key for row in diversity for key in row}); writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(diversity)
    counts = Counter(row['diagnosis'] for row in audit)
    report = ['# Stage 3 远端结果审计', '',
              f'审计组合数：{len(audit)}；诊断分类：{dict(counts)}', '',
              '正式比较基线优先使用 Stage 1 summary；Stage 1 缺失的组合单独标记为 `missing_stage1_filled_seed`，不冒充已有 Stage 1 成绩。',
              '', '## 主要观察', '']
    if audit:
        p23 = [row for row in audit if row['problem'] == 3]
        same = sum(row['p2_p3_makespan_delta'] == 0 for row in p23)
        report.append(f'- P3 与 P2 Makespan 相同：{same}/{len(p23)} 个已审计 P3 组合。')
        report.append(f'- spill_added_copy_bytes > 0：{sum(row["spill_added_copy_bytes"] > 0 for row in audit)} 个组合。')
        report.append(f'- 可读取官方 raw trace：{sum(row["raw_available"] for row in audit)}/{len(audit)} 个组合。')
        report.append(f'- Stage 2 严格不退化记录：{sum(float(row.get("stage2_improvement_vs_stage1") or 0) >= 0 for row in audit)}/{len(audit)}。')
    report += ['', '## 分类计数', '']
    for key, value in sorted(counts.items()): report.append(f'- `{key}`：{value}')
    report += ['', '下界诊断使用 `max(W_M/k, W_V/k, critical_path_cycles)`，只用于定位，不替代官方 Makespan。', '']
    (args.output_dir / 'bottleneck_report.md').write_text('\n'.join(report), encoding='utf-8')
    print(f'wrote {len(audit)} audit rows to {args.output_dir}')


if __name__ == '__main__':
    main()
