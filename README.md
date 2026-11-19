# 通用神经网络处理器下的多核调度

[![Repository checks](https://github.com/xiaolong745/math_mothed_A/actions/workflows/checks.yml/badge.svg)](https://github.com/xiaolong745/math_mothed_A/actions/workflows/checks.yml)

面向 DAG 计算图的多核切图与调度研究项目，使用赛题官方 evaluator 验证 P1/P2/P3 的真实 Makespan。
主线为：**多样化候选生成 → 预算内局部精修 → 官方评估 → 保留历史最优方案**。

## 方法与目录

- **Stage 1**：Simple、RegionGrow、BandComp 候选组合，使用 HEFT-lite 生成核心映射和子图顺序。
- **局部精修**：连续拓扑片段、问题相关 Gain、FM-style boundary move、短链移动和受限同核顺序交换。
- **评估闭环**：合法性检查、可选 Spill Oracle、候选去重、缓存和预算管理；仅已完成官方评估的合法方案可以更新 elite。
- **探索性实验**：学习代理和宏块驻留保留代码与负结果记录，未启用为主流程优化。

```text
code/        赛题官方评估器及 schedule_step1/2/3
data/        100 张原始计算图与 config.txt
scheduler/   图建模、候选生成、调度、精修与评估适配
tools/       实验入口、结果汇总与审计工具
tests/       现有单元测试
docs/        算法说明、实验历程与成绩边界
results/     精选成绩、逐格来源与冻结快照
paper/       论文草稿源文件、图与绘图脚本
```

## 环境与快速开始

Python **3.10+**；调度、测试和官方评估仅使用 Python 标准库。官方 evaluator 使用 CPU，PPU/GPU 不参与。
完整批次进程管理使用 Linux/POSIX 接口。

```bash
git clone https://github.com/xiaolong745/math_mothed_A.git
cd math_mothed_A
python3 -m venv .venv
source .venv/bin/activate
python3 -m unittest discover -s tests -v
```

先跑一个图的 Stage 1 候选组合：

```bash
python3 -m scheduler.run_scheduler --graph data/case_001.json \
  --cores 5 --problems 1,2,3 --max-candidates 12 --timeout 300 \
  --output results/local_stage1
```

运行包含 Stage 2 和 Π 精修的流程；下例生成独立新结果，不覆盖发布成绩，也不注入历史方案：

```bash
python3 -m tools.run_final_portfolio --cases case_001 --cores 5 \
  --problems 1,2,3 --stage1-dir results/local_stage1 --no-historical \
  --max-seconds 60 --max-evaluations 20 --pi-limit 6 --timeout 300 \
  --output results/local_final
```

`--max-seconds` 仅约束局部精修，Stage 1 生成、单核基线与 Π 后处理另计时间。
两条命令均写出 `summary.csv` 和 `best_configs.json`；Stage 1 还写出单独方案文件。
大型图可能触发官方调用超时，不能把超时方案当作完成评估。

直接调用官方评估器：

```bash
python3 code/multicore_cut_evaluate_problem_1.py data/case_001.json \
  results/local_stage1/plans/case_001_p1_k5.json --config data/config.txt
```

P2/P3 使用对应的 `multicore_cut_evaluate_problem_2.py` / `problem_3.py` 和方案。
完整官方流程与配置说明见 [评估器文档](docs/evaluator_guide.md)。

## 已有成绩与范围

逐格加速比为 **官方单核 Makespan / 最终合法多核 Makespan**，按图取算术平均，再对三题等权平均。
OneOp 不是正式单核基线；加速比不自动换算成比赛总分。

| 结果快照（均为五核） | 图数 | P1 | P2 | P3 | 三题等权平均 |
|---|---:|---:|---:|---:|---:|
| 历史论文冻结成绩 | 10 | 3.459816× | 4.482766× | 4.568239× | **4.170274×** |
| 全量批次原候选池，包含超时前已验证方案 | 100 | 3.333112× | 4.196611× | 4.331984× | **3.953902×** |
| 加入外部已保存方案、官方重评后逐格择优 | 100 | 3.621511× | 4.309317× | 4.439591× | **4.123473×** |

外部方案来自 [2026_match_model_A_openproject](https://github.com/zhuangzhuangaiyutingya/2026_match_model_A_openproject)。
300 个外部方案完成本项目官方重评后，179 格找到更快候选、121 格保留原 Makespan，无格退化。
这是固定 100 图上的候选池扩充结果，不能归因于已经移植了开源搜索算法，也不证明未见图泛化。

原全量批次有 **270 格正常完成、30 格部分超时**；超时前已验证的方案仍保留。
100 图计分使用本项目 96 个官方单核分母，另外 4 个来自外部归档；已重叠的 96 个分母完全一致。
原 `group_metrics.csv` 的 P1/P2/P3 只覆盖各自正常完成子集，不能将其中的 3.907000× 当成完整 100 图成绩。
论文十图成绩与全量 100 图范围不同，二者均保留原始来源。

```bash
# 只读取已有成绩，不生成候选，不运行 evaluator
python3 -m tools.report_published_scores

# 校验冻结归档以及当前官方代码、配置与输入图
python3 results/paper_freeze_20260927/verify_freeze.py
python3 -m tools.verify_repository_inputs
```

成绩文件、来源和复现边界见 [结果索引](results/README.md)，方法收益与失败尝试见
[迭代历程与算法收益](docs/迭代历程与算法收益.md)。

## 批量实验与远端同步

Linux 上的固定参数全量批次入口为 `python3 -m tools.run_full5_batch`。
它先跑 12 张代表图，再按执行门槛扩展到 100 图 × 五核 × 三题；12 个独立进程并发，
每格 900 秒进程上限，60 秒仅为局部精修预算。历史协议见
[全量评估说明](results/full5_20260927/README.md)。
此入口的结果目录固定为 `results/full5_20260927`；重跑时应使用独立工作副本，
删除副本中该历史结果目录后执行，以免把新批次写入发布成绩。

远端结果只读同步工具要求显式提供自己的主机与目录：

```bash
python3 -m tools.sync_full5_progress --host user@host --port 22 \
  --remote-dir /path/to/project/results/full5_20260927 \
  --output results/remote_copy
```

默认仅同步精选汇总，`--detail` 额外下载逐格记录，`--watch` 每分钟同步直至远端结束。
仓库不包含私有远端地址、SSH 密钥、缓存或原始大体积 Trace。

## 论文与来源

[paper/](paper/README.md) 保存历史论文草稿，不将它自动更新为最新全量实验结论。
绘图依赖单独安装：`python3 -m pip install -r requirements-figures.txt`。
编译需要自行安装 XeLaTeX；仓库不携带下载的编译器或系统安装包。

赛题附件与外部计划的归属见 [第三方来源说明](THIRD_PARTY_NOTICES.md)。
