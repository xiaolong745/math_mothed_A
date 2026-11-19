"""Summarize official scheduling graph inputs for batch planning."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scheduler.graph_model import build_graph_model

COPY_TYPES = {"COPY_IN", "COPY_OUT"}


def analyze(path: Path) -> dict:
    graph = json.loads(path.read_text(encoding="utf-8"))
    model = build_graph_model(graph)
    compute_ops = [op for op in graph["ops"] if op.get("op") not in COPY_TYPES]
    pipes = Counter(op.get("pipe", "") for op in compute_ops)
    levels = Counter(info.depth_from_source for info in model.ops.values())
    compute_count = len(compute_ops)
    if compute_count < 2_000:
        size_class = "small"
    elif compute_count < 10_000:
        size_class = "medium"
    else:
        size_class = "large"
    return {
        "case": path.stem,
        "size_class": size_class,
        "ops_total": len(graph["ops"]),
        "ops_compute": compute_count,
        "ops_copy": len(graph["ops"]) - compute_count,
        "tensors": len(graph["tensors"]),
        "edges_raw": len(graph["edges"]),
        "edges_compute_dag": len(model.edges),
        "cycles_compute": sum(int(op.get("cycles", 0)) for op in compute_ops),
        "tensor_bytes_total": sum(int(tensor.get("size", 0)) for tensor in graph["tensors"]),
        "dag_depth": max((info.depth_from_source for info in model.ops.values()), default=0),
        "max_level_width": max(levels.values(), default=0),
        "pipe_m_ops": pipes.get("PIPE_M", 0),
        "pipe_v_ops": pipes.get("PIPE_V", 0),
        "pipe_m_cycles": sum(int(op.get("cycles", 0)) for op in compute_ops if op.get("pipe") == "PIPE_M"),
        "pipe_v_cycles": sum(int(op.get("cycles", 0)) for op in compute_ops if op.get("pipe") == "PIPE_V"),
        "graph_path": str(path),
        "status": "ok",
        "error": "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path, default=Path("results/data_analysis"))
    args = parser.parse_args()

    paths = sorted(path for path in args.data_dir.glob("case_*.json")
                   if path.stem[5:].isdigit())
    rows = []
    for path in paths:
        try:
            rows.append(analyze(path))
        except Exception as exc:
            rows.append({"case": path.stem, "size_class": "invalid",
                         "graph_path": str(path), "status": "error",
                         "error": f"{type(exc).__name__}: {exc}"})

    args.output_dir.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with (args.output_dir / "case_inventory.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    valid = [row for row in rows if row["status"] == "ok"]
    summary = {"input_count": len(paths), "valid_count": len(valid),
               "invalid_count": len(rows) - len(valid),
               "classes": dict(Counter(row["size_class"] for row in valid)),
               "metrics": {}}
    metric_keys = ("ops_total", "ops_compute", "ops_copy", "tensors", "edges_raw",
                   "cycles_compute", "tensor_bytes_total", "dag_depth", "max_level_width")
    for key in metric_keys:
        values = sorted(row[key] for row in valid)
        if not values:
            continue
        summary["metrics"][key] = {
            "min": values[0], "median": statistics.median(values),
            "p90_nearest_rank": values[(9 * len(values) + 9) // 10 - 1],
            "max": values[-1], "sum": sum(values),
        }
    summary["pipe_totals"] = {
        "PIPE_M_ops": sum(row["pipe_m_ops"] for row in valid),
        "PIPE_V_ops": sum(row["pipe_v_ops"] for row in valid),
        "PIPE_M_cycles": sum(row["pipe_m_cycles"] for row in valid),
        "PIPE_V_cycles": sum(row["pipe_v_cycles"] for row in valid),
    }
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"analyzed {len(paths)} cases: {len(valid)} valid, {len(rows) - len(valid)} invalid")
    print(f"outputs: {args.output_dir / 'case_inventory.csv'}, {args.output_dir / 'summary.json'}")
    return 0 if len(valid) == len(paths) else 1


if __name__ == "__main__":
    raise SystemExit(main())
