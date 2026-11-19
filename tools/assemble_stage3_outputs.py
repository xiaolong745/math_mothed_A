"""Assemble focused Pi experiment and audit records into Stage 3 artifacts."""

from __future__ import annotations

import csv
import json
from pathlib import Path


def rows(path):
    try:
        with path.open(newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def main():
    root = Path('results/stage3')
    pi = rows(root / 'pi_summary_remote.csv')
    stage1 = {(r['case'], r['problem'], r['num_cores']): r
              for r in rows(Path('results/stage1/summary.csv'))}
    out = []
    keys = sorted({(r['case'], r['problem'], r['cores']) for r in pi})
    for key in keys:
        case, problem, cores = key
        old = stage1.get((case, problem, cores))
        if old:
            out.append({
                'case': case, 'problem': problem, 'cores': cores,
                'method': 'stage1', 'status': 'ok', 'makespan': old['makespan'],
                'added_copy_bytes': old.get('added_copy_bytes'),
                'official_evaluated': True, 'is_formal_stage1': True,
                'is_final_elite': False, 'baseline_source': 'stage1_summary',
                'error': '',
            })
        stage2 = next(r for r in pi if (r['case'], r['problem'], r['cores']) == key
                      and r['method'] == 'stage2')
        out.append({**stage2, 'method': 'stage2', 'is_formal_stage1': False,
                    'is_final_elite': False, 'baseline_source': 'stage2_remote',
                    'official_evaluated': True})
        candidates = [r for r in pi if (r['case'], r['problem'], r['cores']) == key
                      and r.get('makespan') and r['status'] == 'ok']
        best = min(candidates, key=lambda r: (int(r['makespan']),
                                              int(r.get('added_copy_bytes') or 0)))
        if best['method'] != 'stage2':
            out.append({**best, 'method': 'new_method_pi',
                        'is_formal_stage1': False, 'is_final_elite': False,
                        'baseline_source': 'pi_reorder_official',
                        'official_evaluated': True})
        out.append({**best, 'method': 'final_elite', 'is_formal_stage1': False,
                    'is_final_elite': True, 'baseline_source': 'min_stage1_stage2_pi',
                    'official_evaluated': True})
    fields = sorted({k for r in out for k in r})
    with (root / 'summary.csv').open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(out)
    pi_calls = rows(root / 'pi_evaluator_calls_remote.csv')
    with (root / 'evaluator_calls.csv').open('w', newline='', encoding='utf-8') as fh:
        fields = sorted({k for r in pi_calls for k in r}) if pi_calls else ['case']
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(pi_calls)
    configs = json.loads((root / 'pi_best_configs_remote.json').read_text(encoding='utf-8'))
    (root / 'best_configs.json').write_text(json.dumps(configs, ensure_ascii=False, indent=2), encoding='utf-8')
    spill_fields = ['case', 'problem', 'cores', 'source', 'spill_added_copy_bytes', 'available']
    with (root / 'spill_metrics.csv').open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=spill_fields); writer.writeheader()
        for key in keys:
            writer.writerow({'case': key[0], 'problem': key[1], 'cores': key[2],
                             'source': 'not_collected_in_pi_ablation',
                             'spill_added_copy_bytes': '', 'available': False})
    print(f'assembled {len(out)} summary rows')


if __name__ == '__main__':
    main()
