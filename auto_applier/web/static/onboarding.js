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
