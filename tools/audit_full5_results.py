"""Summarize synchronized evidence without generating or evaluating plans."""
from collections import Counter
import csv
import json
from pathlib import Path
from statistics import mean

OUT = Path(__file__).resolve().parents[1] / 'results/full5_20260927'


def audit(out=OUT):
    with (out / 'summary.csv').open() as f:
        rows = list(csv.DictReader(f))
    groups = {p: [r for r in rows if r['problem'] == str(p)
                   and r['included_in_completed_mean'] == 'True'] for p in (1, 2, 3)}
    common = set.intersection(*[{r['case'] for r in group} for group in groups.values()])
    means = {p: mean(float(r['speedup']) for r in group if r['case'] in common)
             if common else None for p, group in groups.items()}
    attempts, outcomes, unfinished = 0, Counter(), 0
    for path in (out / 'cells').glob('*/events.jsonl'):
        starts, ends = 0, 0
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = event['event']
            if name == 'start':
                starts += 1
            elif name in ('ok', 'failed'):
                ends += 1
                outcomes['cache_hit' if event.get('cache_hit') else name] += 1
            else:
                outcomes[name] += 1
        attempts += starts
        unfinished += max(0, starts - ends)
    violations = [r['case'] + '/P' + r['problem'] for r in rows
                  if r['stage1_makespan'] and r['final_makespan']
                  and int(r['final_makespan']) > int(r['stage1_makespan'])]
    baselines = []
    for path in (out / 'baseline_status').glob('*/process.json'):
        baselines.append(json.loads(path.read_text()))
    result = {
        'requested_cells': 300, 'status_counts': dict(Counter(r['status'] for r in rows)),
        'common_completed_cases': sorted(common), 'common_case_count': len(common),
        'common_set_problem_mean_speedup': means,
        'common_set_three_problem_mean': mean(means.values()) if common else None,
        'full100_score_available': len(common) == 100,
        'stage1_degradation_violations': violations,
        'context_evaluate_attempts_including_cache': attempts,
        'context_outcomes': dict(outcomes), 'unfinished_context_attempts': unfinished,
        'call_count_note': 'Context attempts include cache/validation; unfinished starts may be in flight or killed. Singlecore calls counted separately.',
        'singlecore_processes_started': len(baselines),
        'singlecore_processes_succeeded': sum(r.get('exit_code') == 0 for r in baselines),
        'singlecore_processes_failed': sum(r.get('status') != 'running' and r.get('exit_code') != 0 for r in baselines),
        'finished_process_cpu_seconds': sum(float(r['cpu_seconds']) for r in rows if r['cpu_seconds'])
                                       + sum(r.get('cpu_seconds', 0) for r in baselines),
    }
    temp = out / 'progress_audit.tmp'
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    temp.replace(out / 'progress_audit.json')
    return result


if __name__ == '__main__':
    print(json.dumps(audit(), ensure_ascii=False, indent=2))
