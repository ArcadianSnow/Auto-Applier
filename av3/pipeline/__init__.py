"""Pipeline: staged workers + the ``@stage`` instrumentation wrapper (spec §7).

Phase 0 shipped the ``@stage`` spine; Phase 2 (3/N) added the apply worker (spec §7 #7).
Phase 3 (1/M) added the embedding pre-filter (spec §7 #3). Score/optimize workers and
the staged-worker scheduler arrive in later Phase 3 sub-phases.
"""

from av3.pipeline.apply_worker import (
    ApplyRunSummary,
    ApplyWorker,
    DriverEntry,
    default_drivers,
)
from av3.pipeline.filter_worker import (
    FilterRunSummary,
    FilterWorker,
    build_bank_summary,
)
from av3.pipeline.stage import (
    StageSkip,
    get_run_id,
    new_run_id,
    set_run_id,
    stage,
)

__all__ = [
    "ApplyRunSummary",
    "ApplyWorker",
    "DriverEntry",
    "FilterRunSummary",
    "FilterWorker",
    "StageSkip",
    "build_bank_summary",
    "default_drivers",
    "get_run_id",
    "new_run_id",
    "set_run_id",
    "stage",
]
