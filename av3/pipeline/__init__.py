"""Pipeline: staged workers + the ``@stage`` instrumentation wrapper (spec §7).

Phase 0 shipped the ``@stage`` spine; Phase 2 (3/N) added the apply worker (spec §7 #7).
Phase 3 (1/M) added the embedding pre-filter (spec §7 #3); Phase 3 (2/M) the score worker
(spec §7 #5); Phase 3 (3/M) the optimize+Strict gate (spec §7 #6). The staged-worker
scheduler arrives in (5/M).
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
from av3.pipeline.optimize_worker import (
    OptimizeRunSummary,
    OptimizeWorker,
)
from av3.pipeline.score_worker import (
    AXIS_NAMES,
    ScoreRunSummary,
    ScoreWorker,
    parse_dimensions,
)
from av3.pipeline.stage import (
    StageSkip,
    get_run_id,
    new_run_id,
    set_run_id,
    stage,
)

__all__ = [
    "AXIS_NAMES",
    "ApplyRunSummary",
    "ApplyWorker",
    "DriverEntry",
    "FilterRunSummary",
    "FilterWorker",
    "OptimizeRunSummary",
    "OptimizeWorker",
    "ScoreRunSummary",
    "ScoreWorker",
    "StageSkip",
    "build_bank_summary",
    "default_drivers",
    "get_run_id",
    "new_run_id",
    "parse_dimensions",
    "set_run_id",
    "stage",
]
