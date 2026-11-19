"""Offline selector comparison using already completed evaluator calls.

The script never calls the official evaluator.  It reconstructs the exact
candidate pool for each saved remote ablation batch, applies several cheap
selectors, and looks up the saved Makespan for the selected candidates.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from pathlib import Path

from scheduler.evaluation.surrogate import HistorySurrogate, feature_dict
from scheduler.candidate import candidate_signature
from scheduler.graph_model import build_graph_model
from scheduler.search.portfolio import build_candidates
from scheduler.stage2 import _proxy


def _read_rows(paths):
    rows = []
    for path in paths:
        with Path(path).open(newline="", encoding="utf-8") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def _normalise(values):
    lo, hi = min(values), max(values)
    if hi <= lo:
        return [0.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


def _analytic_scores(features, problem):
    """Cheap parser score; lower is preferred and no evaluator data is used."""
    names = [
        "estimated_copy_bytes", "unique_cut_bytes", "sync_events_proxy",
        "mv_imbalance", "critical_path_cycles", "pipe_conflict",
        "data_reuse_loss",
    ]
    cols = {name: _normalise([f[name] for f in features]) for name in names}
    # Keep the same broad communication/MV/critical-path priorities while
    # making the scale explicit.  Problem labels only alter cheap weights.
    if problem == 1:
        weights = {"estimated_copy_bytes": .20, "unique_cut_bytes": .10,
                   "sync_events_proxy": .20, "mv_imbalance": .25,
                   "critical_path_cycles": .20, "pipe_conflict": .05,
                   "data_reuse_loss": 0.0}
    elif problem == 2:
        weights = {"estimated_copy_bytes": .25, "unique_cut_bytes": .15,
                   "sync_events_proxy": .10, "mv_imbalance": .30,
                   "critical_path_cycles": .15, "pipe_conflict": .05,
                   "data_reuse_loss": 0.0}
    else:
        weights = {"estimated_copy_bytes": .20, "unique_cut_bytes": .10,
                   "sync_events_proxy": .10, "mv_imbalance": .25,
                   "critical_path_cycles": .15, "pipe_conflict": .05,
                   "data_reuse_loss": .15}
    return [sum(weights[n] * cols[n][i] for n in names)
            for i in range(len(features))]


def _diverse_extra(ordered, chosen, total):
    """Reserve candidates from generators not represented yet."""
    chosen = list(chosen)
    generators = {row["candidate_name"] for row in chosen}
    for row in ordered:
        if len(chosen) >= total:
            break
        if int(row["index"]) in {int(x["index"]) for x in chosen}:
            continue
        if row["candidate_name"] not in generators:
            chosen.append(row); generators.add(row["candidate_name"])
    return chosen


def _select(pool, features, surrogate, graph_model, problem, cores, total, seed):
    ids = list(range(len(pool)))
    mandatory = [0] if ids else []  # Simple is the common required seed.
    remaining = [i for i in ids if i not in mandatory]
    # This is the production Stage 2 cheap proxy (no evaluator data), kept
    # under the same interface for the offline comparison.
    analytic = [_proxy(graph_model, c)[0] for c in pool]
    analytic_order = sorted(remaining, key=lambda i: (analytic[i], i))
    pred = [surrogate.predict(f) for f in features]
    surrogate_order = sorted(remaining, key=lambda i: (pred[i], i))
    rng = random.Random(seed)
    random_order = list(remaining); rng.shuffle(random_order)
    def finish(order):
        selected = [i for i in mandatory]
        # Reserve one exploration slot, then fill the remainder by score.
        ranked = order[:max(0, total - len(selected) - 1)]
        selected.extend(ranked)
        leftovers = [i for i in order if i not in selected]
        extras = []
        generators = {pool[i].generator_name for i in selected}
        for i in leftovers:
            if len(extras) >= total - len(selected):
                break
            if pool[i].generator_name not in generators:
                extras.append(i); generators.add(pool[i].generator_name)
        selected.extend(extras)
        for i in leftovers:
            if len(selected) >= total:
                break
            if i not in selected:
                selected.append(i)
        return selected[:total]
    # Reproduce the production surrogate's exact top-k plus diversity rule;
    # this avoids replacing its selection with an approximation in the
    # offline audit.
    ranked = surrogate.rank(pool, graph_model, problem, cores,
                            top_k=4, exploration=2)
    selected_surrogate = [0] if pool else []
    for item in ranked:
        sig = candidate_signature(graph_model, item[2])
        idx = next(i for i, c in enumerate(pool)
                   if candidate_signature(graph_model, c) == sig)
        if idx not in selected_surrogate:
            selected_surrogate.append(idx)
    return {
        "analytic": finish(analytic_order),
        "random": finish(random_order),
        "surrogate": selected_surrogate[:total],
        "full": ids,
        "scores": {"analytic": analytic, "surrogate": pred},
    }


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", nargs="+", required=True)
    ap.add_argument("--model", type=Path,
                    default=root / "results/surrogate_audit_20260927/model.json")
    ap.add_argument("--output", type=Path,
                    default=root / "results/surrogate_selector_ablation_20260927")
    ap.add_argument("--seeds", default="11,23,47,89,131")
    args = ap.parse_args(argv)
    model_sur = HistorySurrogate.from_dict(json.loads(args.model.read_text()))
    rows = _read_rows(args.calls)
    grouped = {}
    for row in rows:
        key = (row["case"], int(row["problem"]), int(row["cores"]))
        grouped.setdefault(key, []).append(row)
    detail, summary = [], []
    seeds = [int(x) for x in args.seeds.split(",") if x]
    for (case, problem, cores), group in sorted(grouped.items()):
        baseline = [r for r in group if r["arm"] == "baseline"]
        if not baseline:
            continue
        surrogate_rows = [r for r in group if r["arm"] == "surrogate"]
        graph_path = root / "data" / f"{case}.json"
        graph = json.loads(graph_path.read_text())
        graph_model = build_graph_model(graph)
        pool = build_candidates(graph_model, cores, "A" if problem == 1 else "B", max_candidates=len(baseline))
        # The saved call index is the deterministic pool index.
        by_index = {int(r["index"]): r for r in baseline}
        if set(by_index) != set(range(len(pool))):
            continue
        feats = [feature_dict(graph_model, c, problem, cores) for c in pool]
        # The three bounded selectors use exactly the same budget as the
        # already-run surrogate arm for this pool.  The full arm is retained
        # only as the pool optimum reference.
        total = len(surrogate_rows) or len(baseline)
        selections = _select(pool, feats, model_sur, graph_model, problem, cores, total, seeds[0])
        pool_best = min(int(r["makespan"]) for r in baseline if r["status"] == "ok")
        mandatory_best = int(by_index[0]["makespan"]) if by_index[0]["status"] == "ok" else None
        for method, selected in [("full", selections["full"]),
                                 ("analytic", selections["analytic"]),
                                 ("surrogate", selections["surrogate"])]:
            vals = [int(by_index[i]["makespan"]) for i in selected
                    if by_index[i]["status"] == "ok"]
            best = min(vals) if vals else None
            detail.append({"case": case, "problem": problem, "cores": cores,
                           "method": method, "seed": "", "pool_size": total,
                           "selected_count": len(selected),
                           "contains_pool_optimum": bool(best == pool_best),
                           "best_makespan": best, "pool_best_makespan": pool_best,
                           "regret": (best - pool_best) if best is not None else None,
                           "regret_ratio": ((best - pool_best) / pool_best) if best is not None else None,
                           "mandatory_contains_optimum": mandatory_best == pool_best,
                           "official_result_source": "saved_calls"})
        # Random arms are repeated with fixed seeds; this is offline lookup.
        for seed in seeds:
            rand_sel = _select(pool, feats, model_sur, graph_model, problem, cores, total, seed)["random"]
            vals = [int(by_index[i]["makespan"]) for i in rand_sel
                    if by_index[i]["status"] == "ok"]
            best = min(vals) if vals else None
            detail.append({"case": case, "problem": problem, "cores": cores,
                           "method": "random", "seed": seed, "pool_size": total,
                           "selected_count": len(rand_sel),
                           "contains_pool_optimum": bool(best == pool_best),
                           "best_makespan": best, "pool_best_makespan": pool_best,
                           "regret": (best - pool_best) if best is not None else None,
                           "regret_ratio": ((best - pool_best) / pool_best) if best is not None else None,
                           "mandatory_contains_optimum": mandatory_best == pool_best,
                           "official_result_source": "saved_calls"})
    args.output.mkdir(parents=True, exist_ok=True)
    fields = sorted({k for r in detail for k in r})
    with (args.output / "detail.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields); w.writeheader(); w.writerows(detail)
    for method in sorted(set(r["method"] for r in detail)):
        rr = [r for r in detail if r["method"] == method]
        regrets = [float(r["regret_ratio"]) for r in rr if r["regret_ratio"] is not None]
        summary.append({"method": method, "rows": len(rr),
                        "pool_optimum_retained": sum(r["contains_pool_optimum"] for r in rr),
                        "pool_optimum_rate": sum(r["contains_pool_optimum"] for r in rr) / len(rr),
                        "mean_regret_ratio": sum(regrets) / len(regrets) if regrets else None,
                        "median_regret_ratio": sorted(regrets)[len(regrets)//2] if regrets else None,
                        "worst_regret_ratio": max(regrets) if regrets else None,
                        "mean_selected_count": sum(int(r["selected_count"]) for r in rr) / len(rr),
                        "official_result_source": "saved_calls"})
    with (args.output / "summary.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=sorted(summary[0])); w.writeheader(); w.writerows(summary)
    (args.output / "report.md").write_text(
        "# Offline selector ablation\n\n"
        "All Makespans are looked up from previously completed official evaluator calls. "
        "No evaluator output, trace, or Makespan was used as a pre-selection feature. "
        "The fixed candidate pool uses Simple (index 0) as the common required seed; "
        "historical elite signatures were not present in these call files and are therefore "
        "reported as unavailable rather than inferred.\n", encoding="utf-8")


if __name__ == "__main__":
    main()
