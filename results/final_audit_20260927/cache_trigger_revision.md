# Cache Trigger Revision

The frozen 10-case matrix contains `case_001`, `case_002`, `case_004`, `case_006`, `case_008`, `case_010`, `case_012`, `case_013`, `case_017`, and `case_023`. It does not contain `case_003` or `case_005`, so their known cache candidates were never eligible for this matrix. They were not treated as inactive; they remain separate known candidates.

The old source file for `case_005|k4` was incomplete (`pi_c235/best_configs.json` had no k4 key), so the first paired audit recorded that row as `unknown`. The affected row was repaired from `pi_k4_new/best_configs.json` and rerun alone. The previous audit remains under `results/stage3/final_cache_pair_audit/`; the revised row is under `results/stage3/final_cache_pair_audit_revision/`.

Each paired row uses one identical complete P/C/Pi plan for both official P2 and P3 evaluators:

| case | cores | P2 | P3 | P2-P3 |
|---|---:|---:|---:|---:|
| case005 | 2 | 60652 | 60402 | 250 |
| case005 | 3 | 52865 | 52824 | 41 |
| case005 | 4 | 53623 | 52109 | 1514 |
| case005 | 5 | 53716 | 47080 | 6636 |
| case003 | 2 | 318524 | 317307 | 1217 |

These are paired evaluator differences, not differences between independently optimized P2/P3 elites. All five pairs are positive and therefore support retaining the known cache candidate list.
