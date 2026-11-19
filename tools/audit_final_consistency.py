"""Consistency audit for the frozen Final Portfolio and unseen validation."""

from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

csv.field_size_limit(1 << 30)


def read(path):
    try:
        with path.open(newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for row in rows for k in row}) if rows else ['case']
    with path.open('w', newline='', encoding='utf-8') as fh:
        out = csv.DictWriter(fh, fieldnames=fields); out.writeheader(); out.writerows(rows)


def f(x):
    try: return float(x)
    except (TypeError, ValueError): return None


def main():
    root = Path(__file__).resolve().parents[1]
    out = root / 'results/final_audit_20260927'; out.mkdir(parents=True, exist_ok=True)
    final_rows = read(root / 'results/final/summary.csv')
    finals = [r for r in final_rows if r.get('method') == 'final_elite']
    calls = read(root / 'results/final/evaluator_calls.csv')
    attrs = read(root / 'results/final/pi_attribution.csv')
    # Cache pairing: retain the old file, replace only the repaired k4 row.
    pairs = read(root / 'results/stage3/final_cache_pair_audit/paired_results.csv')
    revised = read(root / 'results/stage3/final_cache_pair_audit_revision/paired_results.csv')
    pair_by_key = {(r.get('case'), r.get('cores')): r for r in pairs}
    for r in revised: pair_by_key[(r.get('case'), r.get('cores'))] = r
    pair_rows = list(pair_by_key.values())
    write(out / 'cache_trigger_audit.csv', pair_rows)

    # Baseline provenance and the user-reported historical case002/P1/k5 claim.
    provenance = []
    for r in finals:
        provenance.append({
            'case': r['case'], 'problem': r['problem'], 'cores': r['cores'],
            'stage1_source': r.get('stage1_source'),
            'stage1_makespan': r.get('stage1_makespan'),
            'stage2_makespan': r.get('stage2_makespan'),
            'final_makespan': r.get('makespan'),
            'final_source': r.get('source'),
            'official_evaluated': r.get('official_evaluated'),
            'historical_elite_used': r.get('used_historical_elite'),
        })
    provenance.append({'case': 'case_002', 'problem': 1, 'cores': 5,
                       'reported_historical_makespan': 63719,
                       'historical_plan_found': False,
                       'historical_context': 'No plan, evaluator record, or matching artifact found in local or remote results; cannot compare or retain as elite.',
                       'current_stage1_seed': next((r.get('stage1_makespan') for r in finals if r['case']=='case_002' and r['problem']=='1' and r['cores']=='5'), None),
                       'current_stage2': next((r.get('stage2_makespan') for r in finals if r['case']=='case_002' and r['problem']=='1' and r['cores']=='5'), None),
                       'current_final': next((r.get('makespan') for r in finals if r['case']=='case_002' and r['problem']=='1' and r['cores']=='5'), None)})
    write(out / 'baseline_provenance.csv', provenance)

    # Pi attribution: accepted steps are immediate-parent comparisons from the
    # official trace. Final-vs-Stage2 is the fixed no-Pi baseline comparison.
    attr_out = []
    for r in finals:
        key = (r['case'], r['problem'], r['cores'])
        accepted = [a for a in attrs if (a.get('case'), a.get('problem'), a.get('cores')) == key]
        attr_out.append({
            'case': r['case'], 'problem': r['problem'], 'cores': r['cores'],
            'direct_parent_makespan': accepted[0].get('before_makespan') if accepted else r.get('stage2_makespan'),
            'final_makespan': r.get('makespan'),
            'stage2_no_pi_makespan': r.get('stage2_makespan'),
            'immediate_pi_steps': len(accepted),
            'sum_immediate_pi_improvement': sum(f(a.get('makespan_improvement')) or 0 for a in accepted),
            'pi_vs_stage2_improvement': (f(r.get('stage2_makespan')) or 0) - (f(r.get('makespan')) or 0),
            'pi_candidate_present': int(r.get('pi_candidate_count') or 0) > 0,
            'accepted_pi_move': r.get('accepted_pi_move'),
            'interpretation': 'direct parent is trace-backed when an accepted Pi step exists; stage2 is the fixed no-Pi comparison',
        })
    write(out / 'pi_contribution.csv', attr_out)

    # Grouped frozen-matrix scores and cost.
    call_by = defaultdict(list)
    for c in calls:
        call_by[(c.get('case'), c.get('problem'), c.get('cores'))].append(c)
    groups = []
    for problem in ('1', '2', '3'):
        for cores in ('2', '3', '4', '5'):
            rs = [r for r in finals if r['problem'] == problem and r['cores'] == cores]
            improvements = [f(r.get('relative_stage1_improvement')) or 0 for r in rs]
            vals = [f(r.get('makespan')) for r in rs if f(r.get('makespan')) is not None]
            cs = [c for r in rs for c in call_by[(r['case'], problem, cores)]]
            groups.append({
                'problem': problem, 'cores': cores, 'case_count': len(rs),
                'avg_official_singlecore_speedup': statistics.mean([f(r['official_singlecore_speedup']) for r in rs if f(r.get('official_singlecore_speedup')) is not None]) if rs else None,
                'mean_stage1_relative_improvement': statistics.mean(improvements) if improvements else None,
                'median_stage1_relative_improvement': statistics.median(improvements) if improvements else None,
                'improved_count': sum(x > 0 for x in improvements),
                'tie_count': sum(x == 0 for x in improvements),
                'degraded_count': sum(x < 0 for x in improvements),
                'mean_final_makespan': statistics.mean(vals) if vals else None,
                'uncached_evaluator_calls': sum(c.get('status') == 'ok' and c.get('cache_hit') != 'True' for c in cs),
                'cached_evaluator_calls': sum(c.get('cache_hit') == 'True' for c in cs),
                'search_wall_clock_seconds': sum(f(r.get('elapsed_combo')) or 0 for r in rs),
                'evaluator_runtime_seconds': sum(f(c.get('runtime')) or 0 for c in cs),
            })
    write(out / 'group_metrics.csv', groups)

    # Phase-ordered best-so-far trace. The evaluator CSV has no wall-clock
    # timestamp; phase order is the deterministic order in the frozen driver.
    phase_order = {'stage1_seed': 0, 'stage1_fill': 0, 'historical_elite': 1,
                   'refinement': 2, 'stage3_pi': 3}
    progress = []
    for key, rs in defaultdict(list).items():
        pass
    by_combo = defaultdict(list)
    for c in calls:
        by_combo[(c.get('case'), c.get('problem'), c.get('cores'))].append(c)
    for key, rs in by_combo.items():
        rs = sorted(enumerate(rs), key=lambda x: (phase_order.get(x[1].get('phase'), 9), x[0]))
        best = None; elapsed = 0.0
        for n, (_, c) in enumerate(rs, 1):
            value = f(c.get('makespan')); elapsed += f(c.get('runtime')) or 0
            if value is not None and (best is None or value < best): best = value
            progress.append({'case': key[0], 'problem': key[1], 'cores': key[2],
                             'phase': c.get('phase'), 'evaluation_index': n,
                             'runtime_cumulative_seconds': elapsed,
                             'makespan': value, 'best_so_far': best,
                             'status': c.get('status'), 'record_order_basis': 'phase_order_then_csv_order'})
    write(out / 'best_so_far.csv', progress)

    report = [
        '# Final Portfolio Consistency Audit', '',
        f'- Frozen matrix rows: {len(finals)} (10 cases × 4 core counts × 3 problems).',
        f'- Stage 1 provenance: {sum(r.get("stage1_source") == "formal_stage1" for r in finals)} formal rows; {sum(r.get("stage1_source") == "missing_stage1_filled_seed" for r in finals)} deterministic fill rows.',
        '- Cache policy: case003/2 and case005/2,3,4,5 are the known candidates; missing combinations remain unknown.',
        '- Paired cache audit uses one identical complete P/C/Pi plan for P2 and P3. It does not compare independently optimized elites.',
        f'- Pi accepted steps: {len(attrs)} across {sum(r.get("accepted_pi_move") == "True" for r in finals)} final combinations; Stage2-to-final improvements are counted separately in pi_contribution.csv.',
        '- The reported case002/P1/k5=63719 has no recoverable plan or evaluator context and is excluded from the frozen elite.',
        '- best_so_far.csv is phase ordered because the evaluator call log has no absolute timestamp.',
    ]
    (out / 'report.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
    print(f'wrote audit to {out}')


if __name__ == '__main__':
    main()
