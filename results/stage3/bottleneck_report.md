# Stage 3 远端结果审计

审计组合数：28；诊断分类：{'communication': 12, 'unknown': 4, 'cache': 4, 'cross_core_sync': 4, 'mv_imbalance': 4}

正式比较基线优先使用 Stage 1 summary；Stage 1 缺失的组合单独标记为 `missing_stage1_filled_seed`，不冒充已有 Stage 1 成绩。

## 主要观察

- P3 与 P2 Makespan 相同：4/9 个已审计 P3 组合。
- spill_added_copy_bytes > 0：0 个组合。
- 可读取官方 raw trace：24/28 个组合。
- Stage 2 严格不退化记录：28/28。

## 分类计数

- `cache`：4
- `communication`：12
- `cross_core_sync`：4
- `mv_imbalance`：4
- `unknown`：4

下界诊断使用 `max(W_M/k, W_V/k, critical_path_cycles)`，只用于定位，不替代官方 Makespan。

## Stage 3-Pi 定向扩展

case005 的 12 个组合中，10 个通过真实官方 evaluator 得到 Pi 改善，2 个持平；没有组合退化。改善覆盖 P1/P2/P3 和多个核数，说明收益不是单一 `case005/P2/k4` 偶然点。

Pi attribution 共记录 12 个被接受的候选：9 个归因为 Trace 可观测的 Pipe overlap/空闲变化，3 个归因为 COPY 等待减少。官方 Trace 没有发布依赖关键路径长度，因此 `critical_path_length` 保留 null。P2/P3 Cache 条件列表中，case001 四个核数均为 `cache_inactive`；case005 四个核数和 case003 已完成的 2 核为 `cache_candidate`；case003 其余核数为 `cache_unknown`。

本阶段停止在受限 Pi 邻域。没有引入 PEFT、ALNS、超图或新的大规模切图算法。

## case003 定向复评

case003 已完成的 2 核 P2/P3 各尝试 1 个 Pi 邻居，P2 318524→318524、P3 317307→317307，均持平；没有把未改善候选写入新 elite。完整 Stage 3-Pi 汇总目前包含 17 个组合：case001 的 3 个 4 核回归、case005 的 12 个组合、case003 已完成的 2 个 P2/P3 组合。
