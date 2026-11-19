"""Check saved score/plan provenance and report it without running evaluators."""
import csv
import json
import math
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]


def read_csv(path):
    with path.open(newline='', encoding='utf-8-sig') as handle:
        return list(csv.DictReader(handle))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def report(root=ROOT):
    current = {(r['case'], r['problem']): r for r in read_csv(
        root / 'results/full5_20260927/summary.csv')}
    groups = []
    missing_denominators = set()
    for problem in (1, 2, 3):
        prefix = root / 'results/stage3' / f'openproject_p{problem}'
        rows = read_csv(prefix.with_name(prefix.name + '_merged_summary.csv'))
        external = {r['case']: r for r in read_csv(
            prefix.with_name(prefix.name + '_full100.csv'))}
        plans = json.loads(prefix.with_name(prefix.name + '_selected_configs.json').read_text())
        expected = {f'case_{i:03d}' for i in range(1, 101)}
        require(len(rows) == 100 and {r['case'] for r in rows} == expected,
                f'P{problem}: expected exactly 100 unique cases')
        require(set(plans) == {f'{case}|p{problem}|k5' for case in expected},
                f'P{problem}: plan coverage mismatch')
        improved = 0
        for row in rows:
            key = f'{row["case"]}|p{problem}|k5'
            before = current[row['case'], str(problem)]
            imported = external[row['case']]
            require(row['official_evaluated'] == row['legal'] == 'True',
                    f'{key}: missing saved official/legal evidence')
            require(imported['official_evaluated'] == 'True',
                    f'{key}: external re-evaluation not complete')
            require(row['problem'] == str(problem) and row['cores'] == '5',
                    f'{key}: wrong problem or core count')
            old, new, chosen = (int(row[k]) for k in
                                ('current_makespan', 'openproject_makespan', 'selected_makespan'))
            require(old == int(before['final_makespan']), f'{key}: current source mismatch')
            require(new == int(imported['makespan']), f'{key}: external source mismatch')
            require(chosen == min(old, new), f'{key}: selected plan degrades incumbent')
            source = 'openproject_plan' if new < old else 'current_verified'
            require(row['selected_source'] == source, f'{key}: incorrect source')
            require(plans[key]['makespan'] == chosen and plans[key]['plan'],
                    f'{key}: plan metadata mismatch')
            require(plans[key]['selected_source'] == source, f'{key}: plan source mismatch')
            denominator = int(row['singlecore_makespan'])
            require(denominator > 0 and chosen > 0, f'{key}: nonpositive makespan')
            if before['singlecore_makespan']:
                require(denominator == int(before['singlecore_makespan']),
                        f'{key}: official singlecore mismatch')
            else:
                missing_denominators.add(row['case'])
            require(math.isclose(float(row['speedup']), denominator / chosen, rel_tol=1e-12),
                    f'{key}: speedup mismatch')
            improved += chosen < old
        groups.append({
            'problem': problem, 'cores': 5, 'cases': len(rows),
            'before_mean_speedup': mean(int(r['singlecore_makespan']) /
                                        int(r['current_makespan']) for r in rows),
            'selected_mean_speedup': mean(float(r['speedup']) for r in rows),
            'improved_cases': improved, 'tied_cases': 100 - improved,
            'degraded_cases': 0,
        })
    return {
        'scope': 'Saved 100-case external-plan portfolio merge; no new evaluation',
        'groups': groups,
        'three_problem_mean_speedup': mean(g['selected_mean_speedup'] for g in groups),
        'before_three_problem_mean_speedup': mean(g['before_mean_speedup'] for g in groups),
        'external_singlecore_cases': sorted(missing_denominators),
        'note': 'Four singlecore denominators come from the external archive; official/legal flags are historical evidence, not a fresh evaluator run.',
    }


if __name__ == '__main__':
    print(json.dumps(report(), ensure_ascii=False, indent=2))
