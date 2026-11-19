# 开源项目高价值候选接入审计

本轮只把开源 RegionGrow reseed 与 predecessor-sticky wavefront 加入候选池；官方 evaluator、config.txt、schedule_step1/2/3 未修改。所有 makespan 均为官方 evaluator 返回值。

| case | 5核 P1 | 5核 P2 | 5核 P3 | 三题平均最终加速比 |
|---|---:|---:|---:|---:|
| case_001 | 50628 | 47502 | 47502 | 4.8064 |
| case_005 | 64519 | 53716 | 47080 | 1.7643 |

结论：case_001 的 reseed 在 P1/k3、P1/k5 产生真实改善；case_005 的新候选没有超过 Stage 2 elite，最终按 min(Stage 1, Stage 2, 新方法) 保留 Stage 2。wavefront 在 case_001 的 18 个官方评估中没有优于既有候选。