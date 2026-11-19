"""Report all 300 requested cells, including missing/timeout rows."""
import csv
import json
from pathlib import Path
from statistics import mean, median
import time


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def rows(path):
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def write_csv(path, data):
    fields = sorted({k for row in data for k in row}) or ['status']
    temp = path.with_suffix('.tmp')
    with temp.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(data)
    temp.replace(path)


def report(root, out):
    historical = read(out/'historical_best.json')
    summary, call_rows, best_configs = [], [], {}
    for i in range(1,101):
        case = f'case_{i:03d}'
        single = read(out/'baselines'/f'{case}.json').get('makespan')
        single_process = read(out/'baseline_status'/case/'process.json')
        for problem in (1,2,3):
            key = f'{case}|p{problem}|k5'
            d = out/'cells'/f'{case}_p{problem}_k5'
            process = read(d/'process.json')
            incumbent = read(d/'verified_incumbent.json')
            old = historical.get(key, {})
            final = next((r for r in rows(d/'summary.csv') if r.get('method')=='final_elite'
                          and r.get('makespan') and r.get('official_evaluated')=='True'), {})
            events = []
            if (d/'events.jsonl').exists():
                with (d/'events.jsonl').open() as f:
                    for line in f:
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass  # only a concurrently appended final line may be incomplete
            ended = process.get('status') in ('exited','timeout','launch_failed')
            complete = bool(final and process.get('status')=='exited' and process.get('exit_code')==0)
            fresh = incumbent.get('makespan')
            selected = incumbent
            selected_source = 'new_official' if incumbent else 'none'
            if old and (not incumbent or old['makespan'] < fresh):
                selected, selected_source = old, 'historical_retained'
            if selected:
                best_configs[key] = {**selected, 'selected_source': selected_source,
                                     'search_complete': complete}
            status = ('complete' if complete else 'partial_timeout' if process.get('status')=='timeout' and incumbent
                      else 'timeout_no_new_plan' if process.get('status')=='timeout'
                      else 'partial_failed' if ended and incumbent else 'failed' if ended
                      else 'running' if process.get('status')=='running' else 'queued')
            stage1 = int(final['stage1_makespan']) if final.get('stage1_makespan') else None
            failed = [r for r in events if r['event'] in ('failed','oracle_failed')]
            row = {'case':case,'problem':problem,'cores':5,'status':status,
                   'fresh_makespan':fresh,'historical_makespan':old.get('makespan'),
                   'final_makespan':selected.get('makespan'),'selected_source':selected_source,
                   'stage1_makespan':stage1,'stage2_makespan':final.get('stage2_makespan'),
                   'improvement_vs_stage1':1-selected['makespan']/stage1 if stage1 and selected else None,
                   'singlecore_makespan':single,
                   'singlecore_status':single_process.get('status','queued'),
                   'speedup':single/selected['makespan'] if single and selected else None,
                   'included_in_completed_mean':bool(complete and single and selected_source=='new_official'),
                   'official_evaluated':bool(selected), 'prediction_only':False,
                   'added_copy_bytes':selected.get('added_copy_bytes'),
                   'spill_added_copy_bytes':selected.get('spill_added_copy_bytes'),
                   'cache_hit_rate':selected.get('cache_hit_rate'),
                   'evaluator_calls':sum(r['event']=='failed' or (r['event']=='ok' and not r.get('cache_hit')) for r in events),
                   'cache_hits':sum(r['event']=='ok' and r.get('cache_hit') for r in events),
                   'oracle_calls':sum(r['event']=='oracle' and not r.get('cache_hit') for r in events),
                   'failed_calls':len(failed),'last_error':failed[-1].get('error','') if failed else '',
                   'wall_seconds':process.get('wall_seconds',time.time()-process['started_at'] if process.get('started_at') else None),
                   'cpu_seconds':process.get('cpu_seconds'),'max_rss_kib':process.get('max_rss_kib'),
                   'time_to_best':incumbent.get('time_to_best'),'exit_code':process.get('exit_code')}
            summary.append(row)
            call_rows.extend({**r,'case':case,'problem':problem,'cores':5} for r in events if r['event']!='start')
    metrics=[]
    for p in (1,2,3):
        group=[r for r in summary if r['problem']==p]
        scored=[r for r in group if r['included_in_completed_mean']]
        deltas=[r['improvement_vs_stage1'] for r in scored if r['improvement_vs_stage1'] is not None]
        vals=[r['speedup'] for r in scored]
        metrics.append({'problem':p,'requested':100,'complete':sum(r['status']=='complete' for r in group),
                        'scored_complete':len(scored),'partial':sum(r['status'].startswith('partial') for r in group),
                        'failed_or_timeout':sum(r['status'] in ('failed','timeout_no_new_plan') for r in group),
                        'pending':sum(r['status'] in ('queued','running') for r in group),
                        'mean_speedup_completed_only':mean(vals) if vals else None,
                        'median_speedup_completed_only':median(vals) if vals else None,
                        'min_speedup_completed_only':min(vals) if vals else None,
                        'improved_vs_stage1':sum(d>0 for d in deltas),'tied_vs_stage1':sum(d==0 for d in deltas),
                        'degraded_vs_stage1':sum(d<0 for d in deltas)})
    write_csv(out/'summary.csv',summary)
    write_csv(out/'group_metrics.csv',metrics)
    write_csv(out/'evaluator_calls.csv',call_rows)
    write_csv(out/'worst_completed.csv',sorted([r for r in summary if r['included_in_completed_mean']],key=lambda r:r['speedup'])[:20])
    temp=out/'best_configs.tmp';temp.write_text(json.dumps(best_configs,ensure_ascii=False));temp.replace(out/'best_configs.json')
    state=read(out/'state.json')
    text=['# 五核全量评估进度','',f'当前阶段：{state.get("phase","preparing")}；目标 100 图 × 3 问题 = 300 格。',
          '成绩均值只统计正常完成且具有官方单核分母的新评估格子；未完成行全部保留。',
          '历史 elite 在超时或失败时仍保留，但不混入新完成格子的均值。', '',
          '| 问题 | 正常完成 | 可计分 | 部分结果 | 无新结果失败/超时 | 待完成 | 完成子集均值 |',
          '|---|---:|---:|---:|---:|---:|---:|']
    for m in metrics:
        avg=f'{m["mean_speedup_completed_only"]:.6f}' if m['mean_speedup_completed_only'] is not None else '—'
        text.append(f'| P{m["problem"]} | {m["complete"]}/100 | {m["scored_complete"]} | {m["partial"]} | {m["failed_or_timeout"]} | {m["pending"]} | {avg} |')
    text += ['', '候选规则与论文代码快照一致；统一 60 秒精修预算、20 次精修评估预算、6 个 Π 候选，'
             '每次官方调用超时 300 秒；额外组合进程上限 900 秒。Stage1 填充和 Π 的成本单独计入总时间。',
             '未开启学习代理或驻留宏块；原 Portfolio 中已存在的 reseed/wavefront 保持快照语义。',
             '不覆盖论文冻结成绩，不将本批次未完成子集均值称为全量 100 图成绩。']
    (out/'report.md').write_text('\n'.join(text)+'\n',encoding='utf-8')
    return metrics


if __name__=='__main__':
    root=Path(__file__).resolve().parents[1]
    print(json.dumps(report(root,root/'results/full5_20260927'),ensure_ascii=False,indent=2))
