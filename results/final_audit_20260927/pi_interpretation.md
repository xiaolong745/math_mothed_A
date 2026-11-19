# Pi Contribution Interpretation

The frozen 120-combination matrix has 43 accepted immediate Pi steps across 36 combinations. The trace-backed immediate steps sum to 7,537 makespan cycles, which equals the aggregate Stage2-to-final reduction. The fixed no-Pi comparison is the Stage2 best for each combination: 34 combinations improve and 86 are tied; none regress. Two combinations have an accepted intermediate Pi step but end at the Stage2 value because the final incumbent comparison includes the Stage2 elite.

Phase-ordered best-so-far records show 60 combinations improved during Stage2 refinement, with 89,779 aggregate cycles of Stage1-to-Stage2 reduction. Pi then improved 34 final combinations, adding 7,537 cycles of reduction. This indicates most aggregate gain arrives in the P/refinement phase, while Pi is a smaller but independently measurable late-stage contribution.

The call log has no absolute timestamps, so `best_so_far.csv` uses the frozen driver phase order and records cumulative evaluator runtime as an audit timeline.
