# Remote surrogate validation (2026-09-27)

- Scope: 20 fixed-pool combinations on four validation cases (`case_009`, `case_015`, `case_020`, `case_022`). These cases were not used for the historical surrogate training audit.
- Problems/cores: P1 at 2, 4, 5 cores; P2 at 4 cores; P3 at 4 cores.
- Both arms used the same deterministic candidate pool and the unmodified official evaluator.
- Baseline calls: 157; surrogate calls: 115; saved: 42 (26.8%).
- Every combination completed official evaluation; surrogate and full-pool best Makespan matched in all 20 combinations.
- No surrogate-selected result was worse than the fixed-pool baseline.
- Summed per-combination arm wall time: baseline 711.00s; surrogate 484.76s. These are sums, not the wall time of a parallel batch.
- One failed launch (missing `PYTHONPATH`) produced no evaluator calls and is excluded from the table.

The surrogate remains a ranking filter only. These results do not establish that it may replace the frozen elite or skip evaluator calls outside a fixed candidate-pool experiment.
