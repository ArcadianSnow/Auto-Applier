/* Auto Applier v3 — onboarding wizard (Phase 4 (5/M)).
 *
 * Single Alpine.js component driving the multi-step wizard. Each step's
 * "Save" button posts to /api/onboarding/<step>; the server returns the
 * updated status snapshot which the component splices in. The user can
 * close the tab between any two steps and the state survives.
 *
 * No build step — served as-is.
 */

const STEPS = [
  { key: 'contact',      title: 'Contact' },
  { key: 'work-history', title: 'Work history' },
  { key: 'skills',       title: 'Skills' },
  { key: 'work-auth',    title: 'Work auth' },
  { key: 'targeting',    title: 'Targeting' },
  { key: 'telemetry',    title: 'Telemetry' },
  { key: 'web-prefs',    title: 'Control prefs' },
  { key: 'done',         title: 'Done' },
];

function onboarding() {
  return {
    STEPS,
    step: 'contact',
    busy: false,
    lastSavedNote: '',
    status: null,
    extracting: false,
    extractNote: '',
    seed: { status: 'idle', probed: 0, kept: 0, dead: 0, note: '', error: '' },
    _seedPoll: null,

    contact: { name: '', email: '', phone: '', location: '', links: {} },
    workHistory: [],
    skillsText: '',
    workAuth: { work_authorization: '', requires_sponsorship: null },
    targeting: {
      titles: [], locations: [], remote_ok: true, onsite_ok: true,
      salary_floor: null, seniority: '',
    },
    targetingTitlesText: '',
    targetingLocationsText: '',
    telemetry: { enabled: false, handle: '', relay_url: '' },
    webPrefs: {
      hotkey_enabled: true, hotkey: 'F6',
      idle_detect_enabled: false, idle_threshold_s: 60,
    },

    async load() {
      try {
        const r = await fetch('/api/onboarding/state');
        if (!r.ok) return;
        this.status = await r.json();
        this._hydrate(this.status);
        // Jump the user to the first INCOMPLETE step so they don't
        // re-walk steps they already finished.
        this.step = this._firstIncomplete();
        // ...EXCEPT never silently skip work-auth: it's the legally
        // sensitive step (work authorization + sponsorship) and a value
        // pre-seeded into master.json would otherwise satisfy its gate
        // and auto-jump the user clean past it, so they never confirm
        // the sponsorship answer (the bug that put requires_sponsorship
        // out of sync with a "US citizen" auth). If the auto-jump lands
        // anywhere AFTER work-auth, clamp back to work-auth so it's shown
        // for explicit confirmation at least once.
        const order = STEPS.map(s => s.key);
        if (order.indexOf(this.step) > order.indexOf('work-auth')) {
          this.step = 'work-auth';
        }
        // Reflect an in-flight background board search (e.g. the tab was reopened) and resume
        // polling so the user sees it finish even across a reload.
        try {
          const sr = await fetch('/api/onboarding/seed-boards/status');
          if (sr.ok) {
            this.seed = await sr.json();
            if (this.seed.status === 'running') this._pollSeed();
          }
        } catch (e) { /* ignore */ }
      } catch (e) {
        console.error('onboarding load failed', e);
      }
    },

    _hydrate(state) {
      this.contact = { ...this.contact, ...(state.contact || {}) };
      this.workHistory = (state.work_history || []).map(w => ({
        ...w, bulletsText: (w.bullets || []).join('\n'),
      }));
      this.skillsText = (state.skills || []).join('\n');
      this.workAuth = {
        work_authorization: state.work_authorization || '',
        requires_sponsorship: state.requires_sponsorship,
      };
      const t = state.targeting || {};
      this.targeting = {
        titles: t.titles || [],
        locations: t.locations || [],
        remote_ok: t.remote_ok !== false,
        onsite_ok: t.onsite_ok !== false,
        salary_floor: t.salary_floor ?? null,
        seniority: t.seniority || '',
      };
      this.targetingTitlesText = (t.titles || []).join('\n');
      this.targetingLocationsText = (t.locations || []).join('\n');
      this.telemetry = { ...this.telemetry, ...(state.telemetry || {}) };
      this.webPrefs = { ...this.webPrefs, ...(state.web || {}) };
    },

    _firstIncomplete() {
      // STEPS keys map 1:1 to status flags (with the special 'done'
      // pseudo-step at the end). Walk them in order; first false wins.
      const flagMap = {
        'contact':      'has_contact',
        'work-history': 'has_work_history',
        'skills':       'has_skills',
        'work-auth':    'has_work_auth',
        'targeting':    'has_targeting',
        'telemetry':    'has_telemetry_decision',
        'web-prefs':    null,   // optional — no completion gate
      };
      for (const s of STEPS) {
        if (s.key === 'done') return 'done';
        const flag = flagMap[s.key];
        if (flag && !this.status?.[flag]) return s.key;
      }
      return 'done';
    },

    goto(key) {
      this.step = key;
      // Re-hydrate from the (latest) status snapshot so the step's
      // fields reflect what was last saved, not what was in the
      // textbox before navigation.
      if (this.status) this._hydrate(this.status);
    },

    isDone(key) {
      const flagMap = {
        'contact':      'has_contact',
        'work-history': 'has_work_history',
        'skills':       'has_skills',
        'work-auth':    'has_work_auth',
        'targeting':    'has_targeting',
        'telemetry':    'has_telemetry_decision',
      };
      const flag = flagMap[key];
      return flag ? !!this.status?.[flag] : false;
    },

    stepClass(key) {
      if (key === this.step) return 'step-active';
      if (this.isDone(key)) return 'step-done-row';
      return '';
    },

    addWork() {
      this.workHistory.push({
        company: '', title: '', start: '', end: '', bullets: [],
        bulletsText: '',
      });
    },

    removeWork(idx) {
      this.workHistory.splice(idx, 1);
    },

    async extractResume(ev) {
      // Upload a résumé → server extracts a fact-bank DRAFT → pre-fill the résumé-derived steps
      // for the user to REVIEW. Nothing is saved here; the per-step Save buttons still persist.
      const f = ev?.target?.files?.[0];
      if (!f) return;
      this.extracting = true;
      this.extractNote = '';
      try {
        // FileReader → data URL → strip the "data:...;base64," prefix → raw base64 (robust for
        // any size; avoids spreading a large byte array). base64-in-JSON => no multipart needed.
        const b64 = await new Promise((resolve, reject) => {
          const fr = new FileReader();
          fr.onload = () => resolve(String(fr.result).split(',', 2)[1] || '');
          fr.onerror = () => reject(fr.error);
          fr.readAsDataURL(f);
        });
        const r = await fetch('/api/onboarding/extract-resume', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filename: f.name, content_b64: b64 }),
        });
        if (!r.ok) {
          const e = await r.json().catch(() => ({}));
          this.extractNote = `Could not read that file: ${e.detail || r.statusText}`;
          return;
        }
        this._hydrateExtracted(await r.json());
        this.extractNote = 'Filled from your résumé. Review each step and click Save — '
          + 'nothing is stored until you do.';
      } catch (e) {
        this.extractNote = `Error: ${e}`;
      } finally {
        this.extracting = false;
        if (ev?.target) ev.target.value = '';  // let the user re-pick the same file
      }
    },

    _hydrateExtracted(fb) {
      // Pre-fill ONLY the résumé-derived steps (contact / work history / skills). Never touches
      // work-auth / targeting / telemetry — a résumé doesn't supply those, and they stay the
      // user's explicit answers.
      fb = fb || {};
      this.contact = { ...this.contact, ...(fb.contact || {}) };
      this.workHistory = (fb.work_history || []).map(w => ({
        ...w, bulletsText: (w.bullets || []).join('\n'),
      }));
      this.skillsText = (fb.skills || []).join('\n');
    },

    async startSeed() {
      // Background "find companies": kick off the probe, then poll. The user can keep onboarding
      // (or leave) while it runs — the server-side sweep finishes and saves the boards regardless.
      const titles = (this.targetingTitlesText || '')
        .split(/[,\n]/).map(s => s.trim()).filter(Boolean);
      try {
        const r = await fetch('/api/onboarding/seed-boards/start', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ titles }),
        });
        this.seed = await r.json();
      } catch (e) {
        this.seed = { status: 'error', error: String(e) };
        return;
      }
      this._pollSeed();
    },

    _pollSeed() {
      if (this._seedPoll) clearInterval(this._seedPoll);
      this._seedPoll = setInterval(async () => {
        try {
          const r = await fetch('/api/onboarding/seed-boards/status');
          if (!r.ok) return;
          this.seed = await r.json();
          if (this.seed.status !== 'running') {
            clearInterval(this._seedPoll);
            this._seedPoll = null;
          }
        } catch (e) { /* transient — keep polling */ }
      }, 1500);
    },

    async _post(endpoint, payload) {
      this.busy = true;
      this.lastSavedNote = '';
      try {
        const r = await fetch(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          this.lastSavedNote = `Save failed: ${err.detail || r.statusText}`;
          return false;
        }
        this.status = await r.json();
        this.lastSavedNote = 'Saved.';
        return true;
      } catch (e) {
        this.lastSavedNote = `Error: ${e}`;
        return false;
      } finally {
        this.busy = false;
      }
    },

    async saveContact() {
      if (await this._post('/api/onboarding/contact', this.contact)) {
        this.step = 'work-history';
      }
    },

    async saveWorkHistory() {
      const payload = {
        work_history: this.workHistory.map(w => ({
          company: w.company,
          title: w.title,
          start: w.start,
          end: w.end,
          bullets: (w.bulletsText || '')
            .split('\n')
            .map(line => line.trim())
            .filter(Boolean),
        })),
      };
      if (await this._post('/api/onboarding/work-history', payload)) {
        this.step = 'skills';
      }
    },

    async saveSkills() {
      const skills = (this.skillsText || '')
        .split(/[,\n]/)
        .map(s => s.trim())
        .filter(Boolean);
      if (await this._post('/api/onboarding/skills', { skills })) {
        this.step = 'work-auth';
      }
    },

    async saveWorkAuth() {
      const payload = {
        work_authorization: this.workAuth.work_authorization,
        requires_sponsorship: this.workAuth.requires_sponsorship,
      };
      if (await this._post('/api/onboarding/work-auth', payload)) {
        this.step = 'targeting';
      }
    },

    async saveTargeting() {
      const titles = (this.targetingTitlesText || '')
        .split(/[,\n]/)
        .map(s => s.trim())
        .filter(Boolean);
      const locations = (this.targetingLocationsText || '')
        .split(/[,\n]/)
        .map(s => s.trim())
        .filter(Boolean);
      const payload = {
        titles, locations,
        remote_ok: !!this.targeting.remote_ok,
        onsite_ok: !!this.targeting.onsite_ok,
        salary_floor:
          this.targeting.salary_floor === null ||
          this.targeting.salary_floor === ''
            ? null : Number(this.targeting.salary_floor),
        seniority: this.targeting.seniority || '',
      };
      if (await this._post('/api/onboarding/targeting', payload)) {
        this.step = 'telemetry';
      }
    },

    async saveTelemetry() {
      const payload = {
        enabled: !!this.telemetry.enabled,
        handle: this.telemetry.handle || null,
        relay_url: this.telemetry.relay_url || null,
      };
      if (await this._post('/api/onboarding/telemetry', payload)) {
        this.step = 'web-prefs';
      }
    },

    async saveWebPrefs() {
      const payload = {
        hotkey_enabled: !!this.webPrefs.hotkey_enabled,
        hotkey: this.webPrefs.hotkey || 'F6',
        idle_detect_enabled: !!this.webPrefs.idle_detect_enabled,
        idle_threshold_s: Number(this.webPrefs.idle_threshold_s) || 60,
      };
      if (await this._post('/api/onboarding/web-prefs', payload)) {
        this.step = 'done';
      }
    },
  };
}

window.onboarding = onboarding;
