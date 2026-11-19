"""Trace-based attribution for fixed-P/C same-core order changes."""

from __future__ import annotations

from collections import defaultdict


def _ops_by_pipe(raw):
    result = defaultdict(list)
    for core in raw.get('per_core_timeline', []) or []:
        for op in core.get('ops', []) or []:
            item = dict(op)
            item['core_id'] = core.get('core_id')
            result[(item['core_id'], item.get('pipe'))].append(item)
    for key in result:
        result[key].sort(key=lambda op: (op.get('start', 0), op.get('end', 0), op.get('op_id', 0)))
    return result


def _trace_metrics(raw):
    if not raw:
        return {
            'makespan': None, 'pipe_order': None, 'pipe_idle_cycles': None,
            'pipe_overlap_cycles': None, 'cross_core_wait_cycles': None,
            'copy_wait_cycles': None, 'critical_path_length': None,
            'critical_path_source': 'unavailable',
        }
    by_pipe = _ops_by_pipe(raw)
    idle = {}
    overlap = 0
    for key, ops in by_pipe.items():
        if not ops:
            idle[key] = 0
            continue
        span = max(int(op.get('end', 0)) for op in ops) - min(int(op.get('start', 0)) for op in ops)
        busy = sum(max(0, int(op.get('end', 0)) - int(op.get('start', 0))) for op in ops)
        idle[key] = max(0, span - busy)
    # Pairwise overlap among different pipes on each core is directly
    # observable from the operation intervals.
    for core in raw.get('per_core_timeline', []) or []:
        ops = core.get('ops', []) or []
        for index, left in enumerate(ops):
            for right in ops[index + 1:]:
                if left.get('pipe') == right.get('pipe'):
                    continue
                overlap += max(0, min(int(left.get('end', 0)), int(right.get('end', 0)))
                           - max(int(left.get('start', 0)), int(right.get('start', 0))))
    transfer_wait = []
    copy_wait = []
    for transfer in raw.get('cross_core_transfers', []) or []:
        release = transfer.get('copy_in_release')
        out_end = transfer.get('copy_out_end')
        in_start = transfer.get('copy_in_start')
        if release is not None and out_end is not None:
            transfer_wait.append(max(0, int(release) - int(out_end)))
        if release is not None and in_start is not None:
            copy_wait.append(max(0, int(in_start) - int(release)))
    return {
        'makespan': raw.get('makespan'),
        'pipe_order': {f'{core}:{pipe}': [op.get('op_id') for op in ops]
                       for (core, pipe), ops in by_pipe.items()},
        'pipe_idle_cycles': dict(idle),
        'pipe_overlap_cycles': overlap,
        'cross_core_wait_cycles': sum(transfer_wait) if transfer_wait else None,
        'copy_wait_cycles': sum(copy_wait) if copy_wait else None,
        # The official result does not publish a dependency-critical trace;
        # never substitute a static DAG proxy for this field.
        'critical_path_length': None,
        'critical_path_source': 'unavailable_from_official_trace',
    }


def attribute(before_raw, after_raw, before_label='', after_label=''):
    """Return before/after observed metrics and a conservative cause label."""
    before = _trace_metrics(before_raw)
    after = _trace_metrics(after_raw)
    improvement = (int(before['makespan']) - int(after['makespan'])
                   if before['makespan'] is not None and after['makespan'] is not None
                   else None)
    idle_before = sum(before['pipe_idle_cycles'].values()) if before['pipe_idle_cycles'] else None
    idle_after = sum(after['pipe_idle_cycles'].values()) if after['pipe_idle_cycles'] else None
    if improvement is None or improvement <= 0:
        cause = 'not_accepted'
    elif idle_before is not None and idle_after is not None and idle_after < idle_before:
        cause = 'pipe_overlap'
    elif (before['cross_core_wait_cycles'] is not None
          and after['cross_core_wait_cycles'] is not None
          and after['cross_core_wait_cycles'] < before['cross_core_wait_cycles']):
        cause = 'cross_core_wait_reduction'
    elif (before['copy_wait_cycles'] is not None
          and after['copy_wait_cycles'] is not None
          and after['copy_wait_cycles'] < before['copy_wait_cycles']):
        cause = 'copy_wait_reduction'
    else:
        cause = 'unknown'
    return {
        'before_label': before_label, 'after_label': after_label,
        'before_makespan': before['makespan'], 'after_makespan': after['makespan'],
        'makespan_improvement': improvement, 'attribution': cause,
        'before_pipe_order': before['pipe_order'], 'after_pipe_order': after['pipe_order'],
        'before_pipe_idle_cycles': before['pipe_idle_cycles'],
        'after_pipe_idle_cycles': after['pipe_idle_cycles'],
        'before_pipe_overlap_cycles': before['pipe_overlap_cycles'],
        'after_pipe_overlap_cycles': after['pipe_overlap_cycles'],
        'before_cross_core_wait_cycles': before['cross_core_wait_cycles'],
        'after_cross_core_wait_cycles': after['cross_core_wait_cycles'],
        'before_copy_wait_cycles': before['copy_wait_cycles'],
        'after_copy_wait_cycles': after['copy_wait_cycles'],
        'critical_path_length': None,
        'critical_path_source': 'unavailable_from_official_trace',
    }
