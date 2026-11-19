"""Merge focused Pi runs, cache candidates, attribution, and verified elites."""

from __future__ import annotations

import csv
import json
from pathlib import Path
csv.field_size_limit(1024 * 1024 * 1024)


def read_rows(path):
    try:
        with path.open(newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def write_csv(path, rows):
    fields = sorted({key for row in rows for key in row}) if rows else ['case']
    with path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(rows)


def main():
    root = Path('results/stage3')
    runs = [root / 'pi_k4_new', root / 'pi_c235',
            root / 'pi_case003_p2', root / 'pi_case003_p3']
    pi_rows = []
    calls, attribution = [], []
    config_by_key = {}
    for run in runs:
        pi_rows.extend(read_rows(run / 'summary.csv'))
        calls.extend(read_rows(run / 'evaluator_calls.csv'))
        attribution.extend(read_rows(run / 'pi_attribution.csv'))
        try:
            config_by_key.update(json.loads((run / 'best_configs.json').read_text(encoding='utf-8')))
        except FileNotFoundError:
            pass
    # Keep the most recent run for duplicate Stage 2/Pi rows.
    unique = {}
    for row in pi_rows:
        key = (row.get('case'), row.get('problem'), row.get('cores'),
               row.get('method'), row.get('variant', ''))
        unique[key] = row
    pi_rows = list(unique.values())
    stage1_rows = {(r['case'], r['problem'], r['num_cores']): r
                   for r in read_rows(Path('results/stage1/summary.csv'))}
    stage1_configs = {}
    try:
        stage1_configs = json.loads(Path('results/stage1/best_configs.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        pass
    keys = sorted({(r['case'], r['problem'], r['cores']) for r in pi_rows})
    summary = []
    elite = {}
    for case, problem, cores in keys:
        combo = [r for r in pi_rows if (r.get('case'), r.get('problem'), r.get('cores')) == (case, problem, cores)
                 and r.get('status') == 'ok' and r.get('makespan')]
        if not combo:
            continue
        s1 = stage1_rows.get((case, problem, cores))
        if s1:
            summary.append({'case': case, 'problem': problem, 'cores': cores,
                            'method': 'stage1', 'makespan': s1['makespan'],
                            'added_copy_bytes': s1.get('added_copy_bytes'),
                            'official_evaluated': True, 'verified': True,
                            'prediction_only': False, 'is_final_elite': False,
                            'source': 'stage1_summary'})
        stage2 = next((r for r in combo if r.get('method') == 'stage2'), None)
        if stage2:
            summary.append({**stage2, 'method': 'stage2', 'verified': True,
                            'prediction_only': False, 'is_final_elite': False,
                            'source': 'remote_stage2'})
        pi_only = [r for r in combo if r.get('method') == 'pi_reorder']
        best = min(combo, key=lambda r: (int(r['makespan']), int(r.get('added_copy_bytes') or 0)))
        if pi_only:
            best_pi = min(pi_only, key=lambda r: (int(r['makespan']), int(r.get('added_copy_bytes') or 0)))
            if int(best_pi['makespan']) <= int(stage2['makespan']) if stage2 else True:
                summary.append({**best_pi, 'method': 'stage3_pi', 'verified': True,
                                'prediction_only': False, 'is_final_elite': False,
                                'source': 'pi_reorder_official'})
        summary.append({**best, 'method': 'final_elite', 'verified': True,
                        'prediction_only': False, 'is_final_elite': True,
                        'source': 'min_verified_stage2_pi'})
        config_key = f'{case}|p{problem}|k{cores}'
        plan = config_by_key.get(config_key, {}).get('plan')
        if plan is None:
            plan = stage1_configs.get(config_key, {}).get('plan')
        elite[config_key] = {
            'case': case, 'problem': int(problem), 'num_cores': int(cores),
            'makespan': int(best['makespan']),
            'added_copy_bytes': int(best.get('added_copy_bytes') or 0),
            'source': best.get('method'), 'official_evaluated': True,
            'plan': plan,
        }
    write_csv(root / 'pi_summary.csv', summary)
    write_csv(root / 'evaluator_calls.csv', calls)
    write_csv(root / 'pi_attribution.csv', attribution)
    (root / 'elite_merged.json').write_text(json.dumps(elite, ensure_ascii=False, indent=2), encoding='utf-8')

    # Cache activation is based only on complete, directly comparable P2/P3
    # official results. Missing combinations remain explicitly unknown.
    cache_rows = []
    for case in sorted({r['case'] for r in read_rows(root / 'remote_audit.csv')}):
        for cores in (2, 3, 4, 5):
            p2 = next((r for r in read_rows(root / 'remote_audit.csv')
                       if r['case'] == case and r['problem'] == '2' and r['cores'] == str(cores)), None)
            p3 = next((r for r in read_rows(root / 'remote_audit.csv')
                       if r['case'] == case and r['problem'] == '3' and r['cores'] == str(cores)), None)
            if not p2 or not p3 or not p2.get('makespan') or not p3.get('makespan'):
                status, delta = 'cache_unknown', None
            else:
                delta = int(p2['makespan']) - int(p3['makespan'])
                status = 'cache_inactive' if abs(delta) <= 1 else ('cache_candidate' if delta > 1 else 'cache_unknown')
            cache_rows.append({'case': case, 'cores': cores,
                               'p2_makespan': p2.get('makespan') if p2 else None,
                               'p3_makespan': p3.get('makespan') if p3 else None,
                               'delta_cache_p2_minus_p3': delta,
                               'status': status,
                               'complete_comparison': bool(p2 and p3)})
    write_csv(root / 'cache_candidate_list.csv', cache_rows)
    print(f'assembled {len(summary)} summary rows, {len(attribution)} attribution rows, {len(elite)} elites')


if __name__ == '__main__':
    main()
