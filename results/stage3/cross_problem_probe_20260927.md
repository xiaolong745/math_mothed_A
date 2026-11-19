# Cross Problem Portfolio Probe

本探针把同一 case、五核下已通过官方评估的 P1/P2/P3 方案互相带入目标问题，再由目标问题官方 evaluator 重新评估。所有基线来自 `results/full5_20260927/summary.csv`；不使用预测值替换 elite，也不修改官方代码。
