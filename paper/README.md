# 论文初版

- `main.tex`：中文论文源文件。
- `figures/`：由 `scripts/generate_figures.py` 从 `results/` 生成的图。
- `build/main.pdf`：本地编译产物，不跟踪到 Git。

这是历史初稿，不代表后续 100 图外部候选合并后的最终成绩。最新数据见
[结果索引](../results/README.md)。

重新生成图：

```bash
python3 paper/scripts/generate_figures.py
```

绘图前安装可选依赖：`python3 -m pip install -r requirements-figures.txt`。
编译需要自行安装 XeLaTeX；从 `paper/` 运行两次以更新交叉引用：

```bash
cd paper
mkdir -p build
xelatex -interaction=nonstopmode -output-directory=build main.tex
xelatex -interaction=nonstopmode -output-directory=build main.tex
```
