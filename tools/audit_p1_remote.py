"""Audit official P1 timelines from a completed full5 run.

This reads evaluator JSON only. It does not run an evaluator or inspect search
predictions, so the resulting fields remain diagnostics rather than scores.
"""
import csv
import json
import statistics
from pathlib import Path


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def audit(root: Path, output: Path):
    rows = []
    for path in sorted((root / 'cells').glob('*_p1_k5/verified_incumbent_official.json')):
        raw = json.loads(path.read_text())
        case = path.parent.name.split('_p1_k5')[0]
        timelines = raw.get('per_core_timeline') or []
        core_rows = []
        total_task_count = 0
        total_task_busy = 0
        total_task_gaps = 0
        cross_waits = 0
        same_waits = 0
        task_core = {}
        task_end = {}
        task_start = {}
        for core in timelines:
            cid = core.get('core_id')
            tasks = sorted(core.get('tasks') or [], key=lambda x: x.get('start', 0))
            task_core.update({t.get('task_id'): cid for t in tasks})
            task_start.update({t.get('task_id'): t.get('start', 0) for t in tasks})
            task_end.update({t.get('task_id'): t.get('end', 0) for t in tasks})
            busy = sum(t.get('duration', 0) for t in tasks)
            gaps = sum(max(0, tasks[i + 1].get('start', 0) - tasks[i].get('end', 0))
                       for i in range(len(tasks) - 1))
            initial = tasks[0].get('start', 0) if tasks else 0
            end = max((t.get('end', 0) for t in tasks), default=0)
            total_task_count += len(tasks)
            total_task_busy += busy
            total_task_gaps += gaps + initial
            core_rows.append((cid, len(tasks), busy, gaps + initial, end,
                              busy / end if end else None))
        for dep in raw.get('task_dependencies') or []:
            source, target = dep.get('source'), dep.get('target')
            if source not in task_core or target not in task_core:
                continue
            wait = max(0, task_start.get(target, 0) - task_end.get(source, 0))
            if task_core[source] == task_core[target]:
                same_waits += int(wait > 0)
            else:
                cross_waits += int(wait > 0)
        ends = [x[4] for x in core_rows]
        work = sum(int(op.get('duration', 0)) for core in timelines
                   for op in core.get('ops') or [])
        movement = raw.get('data_movement_bytes') or {}
        makespan = raw.get('makespan')
        active_cores = sum(end > 0 for end in ends)
        aggregate_capacity = active_cores * max(ends, default=0)
        aggregate_non_task = sum(max(0, end - busy)
                                 for _, _, busy, _, end, _ in core_rows)
        rows.append({
            'case': case, 'makespan': makespan,
            'task_count': total_task_count,
            'active_cores': active_cores,
            'core_end_max': max(ends, default=0),
            'core_end_min': min(ends, default=0),
            'core_end_mean': statistics.mean(ends) if ends else 0,
            'core_end_median': statistics.median(ends) if ends else 0,
            'tail_imbalance': (max(ends) / statistics.median(ends)
                               if ends and statistics.median(ends) else None),
            'task_busy_cycles': total_task_busy,
            'task_gap_cycles': total_task_gaps,
            'aggregate_non_task_fraction': (aggregate_non_task / aggregate_capacity
                                            if aggregate_capacity else None),
            'cross_wait_edges': cross_waits,
            'same_wait_edges': same_waits,
            'timeline_op_count': sum(len(core.get('ops') or []) for core in timelines),
            'pipe_work_cycles': work,
            'added_copy_bytes': movement.get('added_copy_bytes'),
            'scheduled_copy_bytes': movement.get('scheduled_copy_bytes'),
            'original_graph_copy_bytes': movement.get('original_graph_copy_bytes'),
            'cross_task_traffic': raw.get('cross_task_traffic'),
            'spill_added_copy_bytes': movement.get('spill_added_copy_bytes'),
            'core_utilization_mean': statistics.mean(x[5] for x in core_rows if x[5] is not None)
                                     if any(x[5] is not None for x in core_rows) else None,
            'core_utilization_min': min((x[5] for x in core_rows if x[5] is not None), default=None),
        })

    fields = sorted({key for row in rows for key in row})
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def report(rows, score_csv, output):
    scores = {}
    with score_csv.open() as handle:
        for row in csv.DictReader(handle):
            if row.get('problem') == '1' and row.get('included_in_completed_mean') == 'True':
                scores[row['case']] = row
    joined = []
    for row in rows:
        if row['case'] in scores:
            joined.append({**row, 'speedup': _num(scores[row['case']].get('speedup')),
                           'stage1_makespan': _num(scores[row['case']].get('stage1_makespan')),
                           'final_makespan': _num(scores[row['case']].get('final_makespan'))})
    joined.sort(key=lambda row: row['speedup'] if row['speedup'] is not None else float('inf'))
    low = joined[:max(1, len(joined) // 3)]
    high = joined[-max(1, len(joined) // 3):]
    def avg(group, key):
        values = [row[key] for row in group if _num(row.get(key)) is not None]
        return statistics.mean(values) if values else None
    lines = ['# P1 官方时间线瓶颈审计', '',
             '本报告只读取已完成全量评估的官方 JSON；所有结论是诊断信号，不替代 Makespan。', '',
             f'- 可计分 P1 组合：{len(joined)}',
             f'- 平均加速比：{avg(joined, "speedup"):.6f}x', '',
             '| 指标 | 低加速比三分之一 | 高加速比三分之一 |',
             '|---|---:|---:|']
    for key in ('speedup', 'makespan', 'added_copy_bytes', 'cross_task_traffic',
                'task_count', 'aggregate_non_task_fraction', 'cross_wait_edges',
                'same_wait_edges', 'tail_imbalance', 'core_utilization_mean',
                'core_utilization_min', 'pipe_work_cycles'):
        lines.append(f'| {key} | {avg(low, key)} | {avg(high, key)} |')
    lines += ['', '## 最低加速比组合', '', '| case | speedup | makespan | added copy bytes | non-Task capacity fraction | cross wait edges | tail imbalance |', '|---|---:|---:|---:|---:|---:|---:|']
    for row in joined[:15]:
        def fmt(value, digits=4):
            if value is None:
                return 'null'
            return f'{value:.{digits}f}' if isinstance(value, float) else str(value)
        lines.append('| {case} | {speedup} | {makespan} | {added_copy_bytes} | {gap} | {cross} | {tail} |'.format(
            case=row['case'], speedup=fmt(row['speedup'], 6), makespan=fmt(row['makespan'], 0),
            added_copy_bytes=fmt(row['added_copy_bytes'], 0),
            gap=fmt(row['aggregate_non_task_fraction']), cross=fmt(row['cross_wait_edges'], 0),
            tail=fmt(row['tail_imbalance'])))
    lines += ['', '## 判断', '',
              'P1 每个子图是独立 Task；官方配置为跨核 Task 等待 1000 cycles、同核等待 100 cycles。',
              '若低分组的非 Task 核容量、跨核等待和边界 COPY 同时较高，说明主要瓶颈是 P1 固有的 Task 边界与同步成本。',
              '若低分组主要表现为尾部核心不平衡而空档较低，才支持继续研究 P1 的核心分配或顺序。',
              '此审计不改变方案、不重跑 evaluator，也不把代理指标写成 Makespan。']
    output.write_text('\n'.join(lines) + '\n', encoding='utf-8')


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    rows = audit(root / 'results/full5_20260927', root / 'results/stage3/p1_timeline_audit.csv')
    report(rows, root / 'results/full5_20260927/summary.csv', root / 'results/stage3/p1_bottleneck_report.md')
    print(json.dumps({'rows': len(rows)}))
