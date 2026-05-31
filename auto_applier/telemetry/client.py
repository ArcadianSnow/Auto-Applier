"""Mirror relay client — the out-of-band drainer (spec §9, Phase 5 4/M).

This is the ONLY outbound-network code in the product, and it is reachable only
when telemetry is opted in AND a ``relay_url`` is configured. It walks the
:class:`auto_applier.telemetry.mirror.MirrorQueue` produced by (2/M), POSTs each
already-scrubbed row to the owner-hosted relay, and marks it delivered or failed
so the queue's backoff ladder reschedules the failures.

## Why a separate one-shot drainer, not a synchronous pipeline step

Spec §9: "Runs out-of-band so a slow relay never blocks the pipeline." A relay
redeploy or a flaky network must never stall the apply loop. So the drainer is
**not** wired into the scheduler's synchronous maintenance hook (which would put
HTTP latency on the loop's critical path). It is a standalone ``av3 mirror drain``
the user schedules on their own cron / Task Scheduler, the same cheap-to-rerun
shape as ``av3 backup`` / ``av3 prune``. An integrated async drain *tick* (its own
cancellable task with a hard time budget) is a deliberate later refinement, not a
v3.0 requirement — the queue is durable and the backoff is bounded, so nothing is
lost by draining on an external cadence.

## Security posture

The app holds **no Turso token** — the relay does (spec §9). The client just
POSTs scrubbed JSON; the relay re-scrubs (second line of defence), rate-limits by
``user_id`` / IP, and rejects malformed rows. A compromised client has no secret
to steal and the relay can drop an abusive caller. Rows are already scrubbed at
enqueue time (2/M), so even the in-flight payload carries no PII.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from auto_applier.telemetry.mirror import MirrorQueue

__all__ = ["DrainResult", "MirrorClient", "PostFn"]


# A pluggable POST transport: ``(url, json_body) -> http_status_int``. Raising is
# treated as a transport failure (network down, DNS, timeout). Injectable so tests
# never touch the network; the default uses httpx.
PostFn = Callable[[str, dict[str, Any]], int]


@dataclass(frozen=True)
class DrainResult:
    """Outcome of one drain pass."""

    attempted: int
    delivered: int
    failed: int

    @property
    def all_delivered(self) -> bool:
        return self.attempted > 0 and self.failed == 0


def _httpx_post(timeout_s: float) -> PostFn:
    """Build the default httpx-backed POST transport. Imported lazily so the
    telemetry package doesn't pull httpx unless a real drain runs."""
    import httpx

    def _post(url: str, body: dict[str, Any]) -> int:
        resp = httpx.post(url, json=body, timeout=timeout_s)
        return resp.status_code

    return _post


def _ingest_url(relay_url: str) -> str:
    return relay_url.rstrip("/") + "/ingest"


class MirrorClient:
    """Drains a :class:`MirrorQueue` to the owner-hosted relay.

    Construct with the queue, the configured ``relay_url``, and (optionally) a
    POST transport. :meth:`drain` does one bounded pass; schedule repeated calls
    externally (``av3 mirror drain`` on a cron).
    """

    def __init__(
        self,
        queue: MirrorQueue,
        relay_url: str,
        *,
        timeout_s: float = 10.0,
        post: PostFn | None = None,
    ):
        self.queue = queue
        self.relay_url = relay_url
        self.timeout_s = timeout_s
        self._post = post or _httpx_post(timeout_s)

    def drain(self, *, limit: int = 50, now_iso: str | None = None) -> DrainResult:
        """POST up to ``limit`` due rows; mark each delivered/failed.

        Per-row isolation: one row's transport exception never aborts the pass —
        it's marked failed (bumping its backoff) and the loop continues. The
        relay's re-scrub means a row the relay *rejects* (4xx) is still marked
        failed and retried; a persistently-rejected row eventually ages out via
        the retention prune of delivered rows is N/A (it's never delivered) — so
        4xx-permanent rows retry on the (bounded) top backoff step forever until
        an operator prunes. That's acceptable for a 3–4 person tool; a malformed
        row is a bug to fix, not silently dropped.
        """
        url = _ingest_url(self.relay_url)
        rows = self.queue.next_due(limit=limit, now_iso=now_iso)
        delivered = 0
        failed = 0
        for row in rows:
            body = {"category": row.category, "payload": row.payload, "schema": 1}
            try:
                status = self._post(url, body)
            except Exception as exc:  # noqa: BLE001 — any transport error → retry
                self.queue.mark_failed(row.id, f"{type(exc).__name__}: {exc}")
                failed += 1
                continue
            if 200 <= status < 300:
                self.queue.mark_delivered(row.id)
                delivered += 1
            else:
                self.queue.mark_failed(row.id, f"HTTP {status}")
                failed += 1
        return DrainResult(attempted=len(rows), delivered=delivered, failed=failed)
