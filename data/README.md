# 原始计算图与配置

`case_001.json` 至 `case_100.json` 是赛题附件中的 100 张原始图，
`config.txt` 是配套官方配置；本次发布保留原始字节。

运行 `python3 -m tools.verify_repository_inputs` 可对照历史评估 manifest 校验。
输入格式与官方流程见 [评估器说明](../docs/evaluator_guide.md)。

评估器在本目录生成的 `*_res.json`、`*_trace.json`、`*_log.txt` 为运行输出，
不纳入 Git。原始附件及评估器来源见 [第三方来源说明](../THIRD_PARTY_NOTICES.md)。
