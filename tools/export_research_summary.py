"""Export existing evidence and score provenance; never generate/evaluate plans."""
import csv
import hashlib
import json
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'results/research_summary_20260927'


def main():
    if (OUT / 'FROZEN.json').exists():
        raise SystemExit(
            'Research scores are frozen. Do not overwrite this release; '
            'create a separately named release for any future revision.')
    sources = {
        'score_rows': 'results/residency_20260927/latest_known_5core.csv',
        'frozen_historical': 'results/final_frozen_20260927/best_configs.json',
        'merged_historical': 'results/reseed_pair_20260927/elite_merged.json',
        'residency_revalidated': 'results/residency_20260927/best_configs.json',
        'frozen_summary': 'results/final_frozen_20260927/summary.csv',
        'selector': 'results/surrogate_selector_ablation_20260927_v5/summary.csv',
        'residency_methods': 'results/residency_20260927/method_summary.csv',
        'residency_timeline': 'results/residency_20260927/timeline_diagnostics.csv',
        'residency_pairs': 'results/residency_20260927/cache_pairs.csv',
        'residency_integrity': 'results/residency_20260927/official_integrity.json',
    }
    def read_csv(key):
        with (ROOT / sources[key]).open() as f:
            return list(csv.DictReader(f))
    configs = {k: json.loads((ROOT / sources[k]).read_text()) for k in (
        'frozen_historical', 'merged_historical', 'residency_revalidated')}
    score_rows = read_csv('score_rows')
    assert len(score_rows) == 30
    assert len({(r['case'], r['problem'], r['cores']) for r in score_rows}) == 30
    assert all(r['cores'] == '5' for r in score_rows)
    plans = {}
    for row in score_rows:
        key = f'{row["case"]}|p{row["problem"]}|k5'
        selected = configs[row['source']][key]
        assert selected.get('plan') and selected['makespan'] == int(row['makespan'])
        exact = int(row['singlecore_makespan']) / int(row['makespan'])
        assert abs(exact - float(row['speedup'])) < 1e-12
        row['speedup'] = exact
        row['plan_source'] = sources[row['source']]
        row['singlecore_source'] = sources['frozen_summary']
        plans[key] = selected
    metrics = []
    for problem in ('1', '2', '3'):
        rs = [r for r in score_rows if r['problem'] == problem]
        assert len(rs) == 10
        metrics.append({'problem': problem, 'cores': 5, 'cases': len(rs),
                        'mean_speedup': mean(r['speedup'] for r in rs),
                        'residency_revalidated_rows': sum(
                            r['source'] == 'residency_revalidated' for r in rs)})
    frozen = [r for r in read_csv('frozen_summary') if r['method'] == 'final_elite']
    assert len(frozen) == 120
    stats = {'scope': 'existing official evidence; no new evaluator invocation',
             'mean_5core_speedup': mean(r['speedup'] for r in score_rows),
             'frozen_120': {field: {
                 'improved': sum(float(r[field]) > 0 for r in frozen),
                 'tied': sum(float(r[field]) == 0 for r in frozen),
                 'degraded': sum(float(r[field]) < 0 for r in frozen)}
                 for field in ('relative_stage1_improvement', 'relative_stage2_improvement')}}
    OUT.mkdir(parents=True, exist_ok=True)
    for name, rs in [('score_rows.csv', score_rows), ('score_metrics.csv', metrics),
                     ('selector_evidence.csv', read_csv('selector')),
                     ('residency_evidence.csv', read_csv('residency_methods'))]:
        with (OUT / name).open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(rs[0]))
            writer.writeheader()
            writer.writerows(rs)
    for name, obj in [('best_configs_5core.json', plans), ('statistics.json', stats),
                      ('source_manifest.json', {path: hashlib.sha256(
                          (ROOT / path).read_bytes()).hexdigest() for path in sources.values()})]:
        (OUT / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'score_metrics': metrics, **stats}, indent=2))


if __name__ == '__main__':
    main()
