"""Small, evaluator-output-only diagnostics for Stage 2 search audits."""

from __future__ import annotations


def diagnose(result: dict) -> dict:
    """Summarize bottleneck signals without replacing official Makespan."""
    timelines = result.get('per_core_timeline') or []
    core_ends = []
    pipe_busy = {}
    op_count = 0
    for core in timelines:
        ops = core.get('ops') or []
        op_count += len(ops)
        core_ends.append((core.get('core_id'), max((op.get('end', 0) for op in ops), default=0)))
        for op in ops:
            pipe = op.get('pipe')
            pipe_busy[pipe] = pipe_busy.get(pipe, 0) + int(op.get('duration', 0))
    busiest = max(core_ends, key=lambda item: item[1], default=(None, 0))
    contention = len(result.get('ddr_contention_log') or [])
    transfers = len(result.get('cross_core_transfers') or [])
    return {
        'makespan': result.get('makespan'),
        'busiest_core': busiest[0],
        'busiest_core_end': busiest[1],
        'pipe_busy_cycles': pipe_busy,
        'ddr_contention_events': contention,
        'cross_core_transfers': transfers,
        'timeline_op_count': op_count,
    }
