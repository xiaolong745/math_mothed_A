# 结果与证据索引

本目录只跟踪发布所需的汇总、计划与历史冻结快照。
原始 Trace、逐候选 evaluator 缓存、进程日志和临时 checkpoint 保留在本地，均被 `.gitignore` 排除。
此次整理不新增实验，也不修改任何冻结成绩。

| 目录 | 内容与用途 |
|---|---|
| `research_summary_20260927/` | 历史十图五核的固定 `score_rows.csv` 与成绩来源；受 `FROZEN.json` 保护 |
| `paper_freeze_20260927/` | 独立历史代码/配置归档与哈希；可执行 `verify_freeze.py` 校验 |
| `stage1/` | 原 Stage 1 的 30 格四核结果；不是 120 格完整基线 |
| `final_frozen_20260927/` | 2/3/4/5 核 × 三题 × 十图的 120 格历史组合结果 |
| `final_audit_20260927/` | 对上述组合结果的基线来源、调用数与 Π 归因审计 |
| `full5_20260927/` | 100 图五核原批次：270 格正常完成、30 格部分超时；每格保留已验证方案 |
| `stage3/openproject_p[123]_full100.csv` | 外部五核计划在本项目官方 evaluator 下的 300 个重评记录 |
| `stage3/openproject_p[123]_merged_summary.csv` | 原候选池与外部方案逐格取优，含来源与分母 |
| `stage3/openproject_p[123]_selected_configs.json` | 与合并汇总逐格对应的最终合法计划，可重新交给官方 evaluator |
| `reseed_pair_20260927/` | 补种配对探针及历史 elite 合并证据 |
| `residency_20260927/` | 宏块/驻留负结果与消融，不计作有效 Makespan 贡献 |
| `surrogate_validation_20260927/`、`surrogate_selector_ablation_20260927_v5/` | 代理筛选的调用数与漏选边界 |
| `validation_unseen_20260927/` | 有限未调参图验证，含未完成范围 |

## 计分协议

每格 `speedup = singlecore_makespan / selected_makespan`。
每题按图取算术平均，三题再等权平均。
历史十图为 001、002、004、006、008、010、012、013、017、023。

最新已保存的 100 图合并成绩为：P1 **3.621511×**、P2 **4.309317×**、
P3 **4.439591×**，三题 **4.123473×**。
其中 179 格使用更快的外部计划，其余 121 格保持原 Makespan。
96 个单核分母来自本项目，另 4 个缺失分母来自外部归档；96 个重叠分母一致。
逐格方案均已有官方评估记录；本次发布不声称重新执行了全部官方评估。

`full5_20260927/group_metrics.csv` 仅统计正常完成子集：P1 83 格、P2 93 格、P3 93 格。
各题集合不同，其均值再平均得到的 **3.907000×** 不是完整 100 图成绩。
历史十图冻结分数 **4.170274×** 也不能与全量分数直接比较优劣。

读取与一致性核对：

```bash
python3 -m tools.report_published_scores
```

外部候选来源见 [第三方说明](../THIRD_PARTY_NOTICES.md)。
细节与局限见 [迭代记录](../docs/迭代历程与算法收益.md) 和
[外部方案对比报告](stage3/openproject_comparison_20260927.md)。
