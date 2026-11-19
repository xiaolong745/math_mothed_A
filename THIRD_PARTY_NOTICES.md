# 来源与归属

## 赛题附件

`code/`、`data/config.txt`、`data/case_###.json` 以及两份官方算法说明
（`docs/核内调度算法.md`、`docs/多核并行模拟执行算法.md`）来自本项目使用的
“通用神经网络处理器下的多核调度问题”赛题附件。
本次整理未修改官方 evaluator、`schedule_step1/2/3` 或配置。
仓库中的历史 manifest 可用于核对本项目实际使用的版本。

## 外部调度方案

外部参考项目：
[zhuangzhuangaiyutingya/2026_match_model_A_openproject](https://github.com/zhuangzhuangaiyutingya/2026_match_model_A_openproject)。
历史对比记录的版本为 `f1b90b7897de390934fa583bf024dad565600261`。

`results/stage3/openproject_p*_selected_configs.json` 中的部分调度方案来自该项目
已保存的五核计划，另一些来自本项目。逐格来源记录在同目录的
`openproject_p*_merged_summary.csv`，外部计划的本项目官方重评记录为
`openproject_p*_full100.csv`。

本项目没有把外部项目的完整候选生成或搜索代码复制成自己的实现；
导入已知图的计划并择优产生的成绩不代表对未见图的泛化结果。
详细过程见 [迭代记录](docs/迭代历程与算法收益.md)。

## 使用范围

本仓库没有为第三方赛题附件或外部方案重新授予许可；其权利仍归原作者或主办方。
当前未为整个混合来源仓库添加统一开源许可证。
