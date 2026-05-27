"""Source adapters (spec §6). Phase 1 ships Greenhouse only (discovery + describe + probe).

The formal capability model (Discoverer / Describer / Applier protocols) is Phase 2;
the vertical slice uses a concrete ``GreenhouseSource`` — crudest thing that proves the
end-to-end path (spec §11b).
"""

from av3.sources.greenhouse import (
    GreenhouseError,
    GreenhouseSource,
    JobListing,
    confirm_probe,
)

__all__ = ["GreenhouseError", "GreenhouseSource", "JobListing", "confirm_probe"]
