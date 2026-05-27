"""Source adapters (spec §6). Greenhouse + Lever + Ashby discovery (public APIs).

The formal capability model (Discoverer / Describer / Applier protocols) is Phase 2; for
now these are concrete per-ATS sources — crudest thing that proves the path (spec §11b).
"""

from av3.sources.ashby import AshbyListing, AshbySource
from av3.sources.greenhouse import (
    GreenhouseError,
    GreenhouseSource,
    JobListing,
    confirm_probe,
)
from av3.sources.lever import LeverListing, LeverSource

__all__ = [
    "AshbyListing",
    "AshbySource",
    "GreenhouseError",
    "GreenhouseSource",
    "JobListing",
    "LeverListing",
    "LeverSource",
    "confirm_probe",
]
