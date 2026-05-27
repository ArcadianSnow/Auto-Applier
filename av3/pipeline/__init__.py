"""Pipeline: staged workers + the ``@stage`` instrumentation wrapper (spec §7).

Phase 0 ships the ``@stage`` spine only; the staged-worker queue lands in Phase 3.
"""

from av3.pipeline.stage import (
    StageSkip,
    get_run_id,
    new_run_id,
    set_run_id,
    stage,
)

__all__ = ["StageSkip", "get_run_id", "new_run_id", "set_run_id", "stage"]
