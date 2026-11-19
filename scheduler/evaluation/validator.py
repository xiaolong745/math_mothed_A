"""Bridge Stage 1 candidates to the repository's authoritative plan checks."""

from pathlib import Path
import sys


def validate_plan(graph_json, candidate):
    root = Path(__file__).resolve().parents[2]
    code = str(root / 'code')
    if code not in sys.path:
        sys.path.insert(0, code)
    from stub_multicore_cut_and_schedule import derive_multicore_plan

    try:
        view = derive_multicore_plan(graph_json, candidate.to_plan())
    except (ValueError, RuntimeError, KeyError, TypeError) as exc:
        return False, str(exc)
    return True, view
