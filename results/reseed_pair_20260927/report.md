# Reseed pairing and runtime audit

- Problems in this batch: P1; pairs requested: 40. Completed pairs: 14; incomplete: 26.
 - Pairing fixes all parameters to the same `max_ops_per_sg`, depth window, weights, edge mode, balance=max, size slack, scheduler, and deterministic ordering; only `reseed` changes.
- An official incumbent is persisted after the first successful member of each pair. Invalid quotient candidates are retained as failures and cannot enter elite.

## Timing

- candidate_generation: 2.519s cumulative over 41 successful samples.
- candidate_generation_and_heft: 2.591s cumulative over 41 successful samples.
- heft_schedule: 0.055s cumulative over 41 successful samples.
- official_evaluator: 13.706s cumulative over 41 successful samples.
- quotient_legal_check: 0.011s cumulative over 41 successful samples.
- failed_generation_or_validation: 38.585s cumulative over 39 failed samples.
- Run-directory checkpoint mtime bracket (batch wall approximation): 57.994s.
- The bracket includes generation failures and process overhead; cumulative stage times are separate and must not be added as if parallel.
- Direct pair runs did not use the Oracle or evaluation cache; cache/I/O time is therefore not separately attributable in this batch. The residual wall interval includes failed generation and process overhead.

## Pair outcome by core
- k=2: 3/10 complete, improved=3, tied=0, regressed=0, mean delta false−true=64777.666666666664.
- k=3: 3/10 complete, improved=3, tied=0, regressed=0, mean delta false−true=52339.666666666664.
- k=4: 4/10 complete, improved=3, tied=0, regressed=1, mean delta false−true=69315.25.
- k=5: 4/10 complete, improved=4, tied=0, regressed=0, mean delta false−true=76889.5.

## Failure interpretation
- Failure records: 39. The dominant failure is the generator-produced subgraph quotient cycle; those candidates were not sent to the official evaluator.
- Incomplete pairs remain incomplete; their one-sided official result is retained as an incumbent but is excluded from paired performance claims.

## Pi follow-up
- No reseed candidate exceeded the unified historical elite in the completed pairs, so the rule “Pi only for a new historical winner” triggered zero Pi calls in this batch.
- Frozen and historical elites remain authoritative for all combinations.
