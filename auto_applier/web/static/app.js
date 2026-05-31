/* Auto Applier v3 dashboard — Phase 4 (2/M).
 *
 * Two Alpine.js components: `dashboard()` and `jobDetail(id)`. Both are
 * declared on the global to keep things debuggable from the console — Alpine
 * looks them up by name via `x-data="dashboard()"` in the template.
 *
 * Live updates come from two channels:
 *   * polling /api/status + /api/sources + /api/queue + /api/history every
 *     POLL_INTERVAL_MS — refreshes the visible counts and tables.
 *   * an EventSource on /api/events — drives the recent-activity feed and
 *     prods the next poll-cycle to pick up state changes promptly.
 *
 * Polling is what keeps the panels truthful (SSE alone can't tell you the
 * total REVIEW count after a worker burst). The SSE stream is for "show me
 * what's happening now" feel — anything you'd otherwise refresh for.
 */

const POLL_INTERVAL_MS = 5000;
const MAX_RECENT_EVENTS = 40;

/**
 * Best-effort "1m ago"-style relative timestamp. Real i18n is v3.1.
 */
function ago(isoTs) {
  if (!isoTs) return '';
  const t = Date.parse(isoTs);
  if (Number.isNaN(t)) return isoTs;
  const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  return `${Math.floor(hr / 24)}d ago`;
}

function dashboard() {
  return {
    status: {
      scheduler: { running: false, paused: false, pause_reasons: {} },
      jobs_by_state: {},
      pipeline_order: [],
      last_cycle: null,
    },
    sources: [],
    queue: { review: [], queued_apply: [], applying: [] },
    history: [],
    events: [],
    connState: 'connecting',
    controlBusy: false,
    sourceBusy: {},        // {sourceName: bool} — per-row spinner gate for (4/M)
    onboarding: null,      // (5/M) onboarding status snapshot for the banner
    _pollTimer: null,
    _eventSource: null,
    _pollInFlight: false,

    init() {
      this.refreshAll();
      this._pollTimer = setInterval(() => this.refreshAll(), POLL_INTERVAL_MS);
      this._openEventStream();
      window.addEventListener('beforeunload', () => this.teardown());
    },

    teardown() {
      if (this._pollTimer !== null) {
        clearInterval(this._pollTimer);
        this._pollTimer = null;
      }
      if (this._eventSource !== null) {
        this._eventSource.close();
        this._eventSource = null;
      }
    },

    /**
     * Re-fetch every dashboard data source in parallel. Guarded against
     * overlap so a slow request can't pile up multiple in-flight cycles.
     */
    async refreshAll() {
      if (this._pollInFlight) return;
      this._pollInFlight = true;
      try {
        const [status, sources, queue, history, onboarding] = await Promise.all([
          fetch('/api/status').then(r => r.json()),
          fetch('/api/sources').then(r => r.json()),
          fetch('/api/queue').then(r => r.json()),
          fetch('/api/history?limit=20').then(r => r.json()),
          // Best-effort — endpoint may not be reachable on a stripped
          // install (it's wired in (5/M)); the banner just stays hidden.
          fetch('/api/onboarding/state').then(r => r.ok ? r.json() : null)
            .catch(() => null),
        ]);
        this.status = status;
        this.sources = sources.sources || [];
        this.queue = queue;
        this.history = history.applications || [];
        this.onboarding = onboarding;
      } catch (e) {
        // Best-effort: keep stale data on screen rather than blanking.
        console.error('refreshAll failed', e);
      } finally {
        this._pollInFlight = false;
      }
    },

    _openEventStream() {
      if (typeof EventSource === 'undefined') {
        // Old browsers — fall back to polling-only. The dashboard still works.
        this.connState = 'no SSE (polling only)';
        return;
      }
      const es = new EventSource('/api/events');
      this._eventSource = es;
      es.addEventListener('hello', () => {
        this.connState = 'live';
      });
      es.addEventListener('event', (msg) => {
        try {
          const payload = JSON.parse(msg.data);
          this.events.unshift(payload);
          if (this.events.length > MAX_RECENT_EVENTS) {
            this.events.length = MAX_RECENT_EVENTS;
          }
          // Nudge a refresh — state-changing events tend to shift counts
          // we'd otherwise wait POLL_INTERVAL_MS to see.
          if (payload.status === 'ok' || payload.status === 'error') {
            this.refreshAll();
          }
        } catch (e) {
          console.warn('bad SSE payload', e);
        }
      });
      es.onerror = () => {
        // EventSource auto-reconnects on transient drops; surface the state
        // so the user knows.
        this.connState = 'reconnecting...';
      };
    },

    /**
     * Style modifier per pipeline state so terminal vs in-flight states
     * read differently in the panel grid.
     */
    cellClass(state) {
      if (state === 'APPLIED') return 'cell-good';
      if (state === 'REVIEW' || state === 'FAILED') return 'cell-warn';
      if (state === 'SKIPPED' || state === 'FILTERED') return 'cell-muted';
      return 'cell-normal';
    },

    /**
     * Active pause-reason strings for the status bar. Returns the values
     * (reason strings) so the UI doesn't need to render the source keys
     * directly — keeps display copy under designer control instead of
     * leaking 'manual' / 'hotkey' / 'idle' to the user.
     */
    pauseReasonsList() {
      const r = this.status?.scheduler?.pause_reasons || {};
      return Object.values(r).filter(Boolean);
    },

    /**
     * True iff the manual source is currently holding the pause. The
     * button label flips based on this — hotkey/idle pauses don't make
     * the button say 'Resume' because the dashboard can't clear those
     * (the user has to release F6 / become idle).
     */
    manuallyPaused() {
      const r = this.status?.scheduler?.pause_reasons || {};
      return Object.prototype.hasOwnProperty.call(r, 'manual');
    },

    /**
     * POST /api/control/{pause,resume} based on current manual-pause
     * state. Optimistically refreshes the status panel from the response
     * so the UI updates before the next poll tick.
     */
    async togglePause() {
      if (this.controlBusy) return;
      this.controlBusy = true;
      try {
        const endpoint = this.manuallyPaused()
          ? '/api/control/resume'
          : '/api/control/pause';
        const r = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        if (!r.ok) {
          console.error('control toggle failed', r.status);
          return;
        }
        const snap = await r.json();
        // Splice the new pause state into status so the UI updates
        // immediately. The next poll will rewrite this with the canonical
        // server state including counts etc.
        this.status = {
          ...this.status,
          scheduler: {
            ...this.status.scheduler,
            paused: !!snap.paused,
            pause_reasons: snap.reasons || {},
          },
        };
      } catch (e) {
        console.error('togglePause error', e);
      } finally {
        this.controlBusy = false;
      }
    },

    /**
     * Phase 4 (4/M) — open the captured login URL for a source. POSTs
     * to /api/sources/{source}/login; on success the URL fires in the
     * bot's persistent Chrome profile (cookies land where the apply
     * worker needs them).
     */
    async sourceLogin(name) {
      if (this.sourceBusy[name]) return;
      this.sourceBusy = { ...this.sourceBusy, [name]: true };
      try {
        const r = await fetch(`/api/sources/${encodeURIComponent(name)}/login`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          console.error('source login failed', r.status, err);
          alert(`Login launch failed: ${err.detail || r.statusText}`);
          return;
        }
        // Successful launch — the browser already opened the page; nothing
        // more to do client-side until the user clicks 'Mark logged in'.
      } catch (e) {
        console.error('sourceLogin error', e);
      } finally {
        const next = { ...this.sourceBusy };
        delete next[name];
        this.sourceBusy = next;
      }
    },

    /**
     * Phase 4 (4/M) — clear a source's AUTH_REQUIRED flag. Used after the
     * user has signed back in (either via /login above or their own
     * browser). The next refreshAll() drops the source's badge.
     */
    async sourceMarkHealthy(name) {
      if (this.sourceBusy[name]) return;
      this.sourceBusy = { ...this.sourceBusy, [name]: true };
      try {
        const r = await fetch(`/api/sources/${encodeURIComponent(name)}/healthy`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        });
        if (!r.ok) {
          console.error('source mark-healthy failed', r.status);
          return;
        }
        // Optimistic refresh so the badge clears immediately rather than
        // waiting POLL_INTERVAL_MS for the next tick.
        this.refreshAll();
      } catch (e) {
        console.error('sourceMarkHealthy error', e);
      } finally {
        const next = { ...this.sourceBusy };
        delete next[name];
        this.sourceBusy = next;
      }
    },

    ago,
  };
}

function jobDetail(jobId) {
  return {
    jobId,
    loading: true,
    error: null,
    data: null,
    assistedBusy: false,
    assistedNote: '',

    async load() {
      this.loading = true;
      this.error = null;
      try {
        const r = await fetch(`/api/jobs/${this.jobId}`);
        if (!r.ok) {
          this.error = `HTTP ${r.status} ${r.statusText}`;
          return;
        }
        this.data = await r.json();
      } catch (e) {
        this.error = String(e);
      } finally {
        this.loading = false;
      }
    },

    /**
     * Phase 4 (4/M) — the latest ASSISTED_PENDING application or null.
     * Drives whether the assisted-submit card renders + which row the
     * confirm/cancel buttons target.
     */
    latestAssistedPending() {
      const apps = this.data?.applications || [];
      for (let i = apps.length - 1; i >= 0; i--) {
        if (apps[i].status === 'ASSISTED_PENDING') return apps[i];
      }
      return null;
    },

    async assistedOpen() {
      if (this.assistedBusy) return;
      this.assistedBusy = true;
      this.assistedNote = '';
      try {
        const r = await fetch(`/api/jobs/${this.jobId}/assisted/open`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          this.assistedNote = `Could not open: ${body.detail || r.statusText}`;
          return;
        }
        const note = body?.launch?.note || '';
        this.assistedNote = note
          ? `Opened — ${note}`
          : 'Opened in the bot browser.';
      } catch (e) {
        this.assistedNote = `Error: ${e}`;
      } finally {
        this.assistedBusy = false;
      }
    },

    async assistedConfirm() {
      if (this.assistedBusy) return;
      if (!confirm('Mark this application as APPLIED? Only do this after '
                 + 'you clicked submit on the form yourself.')) return;
      this.assistedBusy = true;
      this.assistedNote = '';
      try {
        const r = await fetch(`/api/jobs/${this.jobId}/assisted/confirm`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          this.assistedNote = `Confirm failed: ${body.detail || r.statusText}`;
          return;
        }
        this.assistedNote = 'Marked APPLIED. Reloading the job...';
        await this.load();
      } catch (e) {
        this.assistedNote = `Error: ${e}`;
      } finally {
        this.assistedBusy = false;
      }
    },

    async assistedCancel() {
      if (this.assistedBusy) return;
      if (!confirm('Mark this assisted attempt as cancelled? The job stays '
                 + 'in REVIEW.')) return;
      this.assistedBusy = true;
      this.assistedNote = '';
      try {
        const r = await fetch(`/api/jobs/${this.jobId}/assisted/cancel`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: '{}',
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          this.assistedNote = `Cancel failed: ${body.detail || r.statusText}`;
          return;
        }
        this.assistedNote = 'Marked cancelled. Reloading the job...';
        await this.load();
      } catch (e) {
        this.assistedNote = `Error: ${e}`;
      } finally {
        this.assistedBusy = false;
      }
    },
  };
}

// Expose for Alpine — x-data="dashboard()" needs the symbol on window.
window.dashboard = dashboard;
window.jobDetail = jobDetail;
