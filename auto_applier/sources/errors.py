"""Shared, cross-ATS source errors.

Kept in its own module (rather than inside one ATS adapter) so every source can raise the
same signal and the pipeline can branch on it once, without importing a specific ATS.
"""

from __future__ import annotations

__all__ = ["BoardNotFound"]


class BoardNotFound(RuntimeError):
    """The ATS says this board token does not exist (HTTP 404).

    Distinct from a generic source error on purpose. A 404 is **permanent and specific** — a
    company renamed or retired its board slug — where a timeout or a 5xx is transient. Owner
    decision (2026-07-31): a dead token is **never dropped from targeting**; we keep sweeping it
    every run so it self-heals the day the board comes back, and record the attempt as a marked
    ``failure - 404`` rather than a generic error.

    That distinction is the point. Before it, the owner's spine carried 13 identical
    ``GreenhouseError: board token 'dbtlabsinc' not found (404)`` rows — one per daily run for
    weeks — in the same bucket as real failures, where recurring known noise masks new problems.
    As a marked skip it stays fully visible (``av3 doctor`` names it; the event row keeps the
    reason) without polluting ``av3 errors``.

    Carries ``ats`` + ``token`` as attributes so handlers can report the board without parsing
    the message.
    """

    def __init__(self, ats: str, token: str):
        self.ats = ats
        self.token = token
        super().__init__(f"board token '{token}' not found (404)")
