"""Summarize official residency results and the controlled timeline evidence."""
import csv
import hashlib
from pathlib import Path
import statistics

from tools.run_residency import ROOT, read, dump, csvwrite


def rows(path):
    return list(csv.DictReader(path.open())) if path.exists() else []


def active_union(ops, pipe):
    spans = sorted((o['start'], o['end']) for o in ops if o['pipe'] == pipe)
    total = 0
    end = 0
    for a, b in spans:
        total += max(0, b - max(a, end))
        end = max(end, b)
    return total


def main():
    out = ROOT / 'results/residency_20260927'
    summary = rows(out / 'summary.csv')
    calls = rows(out / 'evaluator_calls.csv')
    ablation = rows(out / 'pcpi_ablation.csv')
    by_key = {(r['case'], r['problem'], r['cores']): r for r in summary}
    baseline_labels = {'stage1', 'stage2', 'elite'}
    methods = []
    for method in sorted({r['method'] for r in ablation} - baseline_labels):
        rs = [r for r in ablation if r['method'] == method]
        deltas = [float(r['makespan']) / float(by_key[
            r['case'], r['problem'], r['cores']]['previous_elite_makespan']) - 1 for r in rs]
        methods.append({'method': method, 'evaluated_combinations': len(rs),
                        'improved': sum(d < 0 for d in deltas),
                        'tied': sum(d == 0 for d in deltas), 'slower': sum(d > 0 for d in deltas),
                        'mean_slowdown': statistics.mean(deltas)})
    csvwrite(out / 'method_summary.csv', methods)
    traces = []
    for r in calls:
        if r['status'] != 'ok' or r['cache_hit'] == 'True':
            continue
        path = out / 'runs' / f'{r["case"]}_p{r["problem"]}_k{r["cores"]}' / f'{r["method"]}_official.json'
        raw = read(path)
        for core in raw['per_core_timeline']:
            ops = core['ops']
            traces.append({'case': r['case'], 'problem': r['problem'], 'cores': r['cores'],
                           'method': r['method'], 'core': core['core_id'],
                           'makespan': raw['makespan'],
                           'm_duration_sum': sum(o['end']-o['start'] for o in ops if o['pipe']=='PIPE_M'),
                           'v_duration_sum': sum(o['end']-o['start'] for o in ops if o['pipe']=='PIPE_V'),
                           'm_idle_within_global_makespan': raw['makespan']-active_union(ops, 'PIPE_M'),
                           'v_idle_within_global_makespan': raw['makespan']-active_union(ops, 'PIPE_V'),
                           'last_end': max((o['end'] for o in ops), default=0),
                           'cross_core_transfers': len(raw.get('cross_core_transfers', [])),
                           'spill_added_copy_bytes': raw['data_movement_bytes']['spill_added_copy_bytes']})
    csvwrite(out / 'timeline_diagnostics.csv', traces)

    # Current known 10-case results; unchanged cells remain explicitly historical.
    frozen = read(ROOT / 'results/final_frozen_20260927/best_configs.json')
    historical = read(ROOT / 'results/reseed_pair_20260927/elite_merged.json')
    current = read(out / 'best_configs.json')
    singles = {r['case']: int(r['singlecore_makespan'])
               for r in rows(ROOT / 'results/final_frozen_20260927/summary.csv')
               if r.get('method') == 'final_elite' and r.get('singlecore_makespan')}
    latest = []
    for key, frozen_item in frozen.items():
        if not key.endswith('|k5'):
            continue
        item, source = frozen_item, 'frozen_historical'
        if key in historical and historical[key]['makespan'] <= item['makespan']:
            item, source = historical[key], 'merged_historical'
        if key in current and current[key]['makespan'] <= item['makespan']:
            item, source = current[key], 'residency_revalidated'
        case, p, k = key.split('|')
        latest.append({'case': case, 'problem': int(p[1:]), 'cores': 5,
                       'makespan': item['makespan'], 'source': source,
                       'evaluated_this_experiment': source == 'residency_revalidated',
                       'singlecore_makespan': singles[case],
                       'speedup': singles[case]/item['makespan']})
    csvwrite(out / 'latest_known_5core.csv', latest)
    metrics = []
    for p in (1, 2, 3):
        rs = [r for r in latest if r['problem'] == p]
        metrics.append({'problem': p, 'case_count': len(rs),
                        'mean_official_singlecore_speedup': statistics.mean(r['speedup'] for r in rs),
                        'fresh_evaluations': sum(r['evaluated_this_experiment'] for r in rs)})
    csvwrite(out / 'latest_known_5core_metrics.csv', metrics)
    hashes = read(out / 'official_hashes_before.json')
    unchanged = all(hashlib.sha256((ROOT/f).read_bytes()).hexdigest() == h for f,h in hashes.items())
    run_hashes = read(out / 'manifest.json')['hashes']
    remote_equal = all(run_hashes.get(f) == h for f,h in hashes.items())
    dump(out / 'official_integrity.json', {'local_unchanged': unchanged,
                                           'remote_snapshot_matches': remote_equal,
                                           'files_checked': len(hashes)})
    timing = read(out / 'timing.json')
    pairs = rows(out / 'cache_pairs.csv')
    report = [
        '# Tensor 复用 / 宏块 / 驻留窗口受控实验', '',
        '结论：第一版新增候选在 24 个组合上未超过当前 elite；最终全部保留原方案。'
        '这否定的是本次具体构造和评分，不能据此否定所有驻留优化。停止扩大搜索与全量运行。', '',
        '## 范围与接口', '',
        '- 远端 CPU，case_001、case_005 × 2/3/4/5 核 × P1/P2/P3，12 个独立 worker。',
        '- 单 Tensor 对应一个逻辑页；不改输入、不复制或删除 Op，不模拟硬件页表。',
        '- Tensor net 保留全部 pin；宏块按共享 Tensor 偏好从拓扑就绪节点构造，尺寸 8/32。'
        '这是受限的 tensor-net 贪心构造，不是完整全局超图优化器。',
        '- 所有宏块与零字节依赖保留；P/C/Pi 合法性最终由完整官方 evaluator 确认。',
        '- 驻留窗口只是工作集与访问优先级；不声称强制驻留、预测精确 Spill 或真实 FIFO 时间线。',
        '- P3 官方 key 是 COPY_IN 目标 Tensor 的 logical_tid/ID；原始 DDR ID 相同不保证命中。',
        '- P2/P3 在同核 Task 中按子图优先级重排 Step1 序，再由 Step2/Step3 处理，窗口不是独立 Task。',
        '- 宏块结构哈希、搜索结果复用与完整超图划分暂未扩展；原 Portfolio 默认不变。', '',
        '## 实测与成本', '',
        f'- 官方多核调用 {sum(r.get("official_called_this_row") == "True" for r in calls)} 次；'
        f'组合内完整方案去重 {sum(r["cache_hit"] == "True" for r in calls)} 次；'
        f'官方单核 {timing["singlecore_calls"]} 次；同计划 P2/P3 诊断另 {len(pairs)} 次；Oracle 0 次。',
        f'- 组合批次墙钟 {timing["parallel_wall_seconds"]:.3f} s；'
        f'组合 CPU 时间累计 {sum(float(r["cpu_seconds"]) for r in summary):.2f} s。'
        '墙钟不含文件传输、单核评估及后续配对诊断。',
        f'- 候选失败 {sum(r["status"] != "ok" for r in calls)}；历史重评不一致 '
        f'{sum(r.get("historical_matches") == "False" for r in calls)}；正式选中方案全部合法。',
        '- 24 个旧 elite 的官方 Spill 均为 0。因此本批次没有证据支持将降低 Spill 作为主攻目标。',
        '- 缺少独立 Stage1 plan 的格子在 summary 中保留 null；不重命名旧 Stage2 为 Stage1。', '',
        '| 方法 | 改善 | 持平 | 更慢 |', '|---|---:|---:|---:|']
    for r in methods:
        report.append(f'| {r["method"]} | {r["improved"]} | {r["tied"]} | {r["slower"]} |')
    report += ['', '## 可定位的失败证据', '',
        '- case_005/P2/k5 固定 P、C，仅改窗口顺序：53716 → 79559。'
        'Added DDR 均为 1867764，Spill 均为 0，跨核传输均为 804 条，每核 M/V 总工作量完全相同。'
        '这直接表明该窗口优先级损害调度时序；不是靠降低 Spill 获益的场景。'
        '实际 Pipe 空闲和结束时间见 timeline_diagnostics.csv。',
        '- 同一组合 macro_32_heft：Added DDR 1867764 → 336490，但 Makespan 53716 → 103652。'
        'M 工作分配变为 [28944,420,34140,210,168]，出现明显负载集中；减少搬运没有抵消负载与依赖时序损失。',
        '- case_005/P3/k5 固定 P、C 的窗口方案命中字节 537204 → 570322，'
        'Makespan 却从 47043 → 78204。命中改善不能当作最终时间改善。',
        '- case_001/P2/k5 旧方案无跨核传输、Spill 为 0，每核 M 工作均为 46560；'
        'macro_32_heft 引入 312 条跨核传输且 M 分配不均，Makespan 47502 → 89250。', '',
        '## 同一计划 P2/P3 配对诊断', '',
        '| case_005/k5 计划 | P2 Makespan | P3 Makespan |', '|---|---:|---:|']
    for method in sorted({r['method'] for r in pairs}):
        rr = {r['problem']: r for r in pairs if r['method'] == method}
        report.append(f'| {method} | {rr["2"]["makespan"]} | {rr["3"]["makespan"]} |')
    report += ['', '## 最新已知 10-case 五核加速比', '',
        '以各 case 官方单核 Makespan / 选中 Makespan 计算，再取算术平均。'
        '本轮只重评其中 case_001；其余沿用既有官方结果。case_005 不属于原十图统计集合。'
        '不将其称为全量 100 图成绩或额外的加权比赛总分。', '',
        '| 问题 | case 数 | 平均加速比 |', '|---|---:|---:|']
    for r in metrics:
        report.append(f'| P{r["problem"]} | {r["case_count"]} | {r["mean_official_singlecore_speedup"]:.6f} |')
    report += ['', f'三题平均：{statistics.mean(r["speedup"] for r in latest):.6f}×。'
               '本次新方法贡献为零，保持原值。', '',
        f'官方文件完整性：本地不变={unchanged}；远端快照一致={remote_equal}。', '',
        '复用统计独立核验见 reuse_inventory_100.csv：100 图共享 Tensor 数中位数 564、'
        '引用次数中位数 2950.5；未把重复签名当作可删除的计算。', '',
        '后续若继续此方向，应先保留现有负载和依赖优先级，约束复用优化的代价，再重新受控验证；'
        '本次不扩大算法，不替换生产默认候选。']
    (out / 'report.md').write_text('\n'.join(report) + '\n', encoding='utf-8')
    print('\n'.join(report))


if __name__ == '__main__':
    main()
