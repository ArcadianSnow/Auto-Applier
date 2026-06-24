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
  { key: 'ai-engine',    title: 'Set up the AI engine' },
  { key: 'contact',      title: 'Contact' },
  { key: 'work-history', title: 'Work history' },
  { key: 'skills',       title: 'Skills' },
  { key: 'work-auth',    title: 'Work auth' },
  { key: 'targeting',    title: 'Targeting' },
  { key: 'telemetry',    title: 'Telemetry' },
  { key: 'email',        title: 'Email (optional)' },
  { key: 'web-prefs',    title: 'Control prefs' },
  { key: 'extras',       title: 'More details (optional)' },
  { key: 'done',         title: 'Done' },
];

function onboarding() {
  return {
    STEPS,
    step: 'contact',
    busy: false,
    lastSavedNote: '',
    validationError: '',   // inline "required field missing" message for the current step
    status: null,
    extracting: false,
    extractNote: '',
    seed: { status: 'idle', probed: 0, kept: 0, dead: 0, note: '', error: '' },
    _seedPoll: null,
    // First-run "Set up the AI engine" step: readiness checklist + two in-app bootstrap jobs.
    setup: {
      readiness: [],
      pull: { status: 'idle', percent: 0, phase: '', model: '', model_index: 0, model_count: 2, error: '' },
      browser: { status: 'idle', phase: '', error: '' },
    },
    _setupPoll: { 'pull-models': null, 'install-browser': null },
    goalChat: {
      open: false, busy: false, done: false, applied: false,
      step: null, answer: '', messages: [], draft: {}, suggestions: [],
    },

    contact: { name: '', email: '', phone: '', location: '', links: {} },
    workHistory: [],
    skillsText: '',
    workAuth: { work_authorization: '', requires_sponsorship: null },
    targeting: {
      titles: [], locations: [], remote_ok: true, onsite_ok: true,
      salary_floor: null, seniority: '', preferences: [],
    },
    targetingTitlesText: '',
    targetingLocationsText: '',
    telemetry: { enabled: false, handle: '', relay_url: '' },
    inbox: { user: '', password: '', host: 'imap.gmail.com', port: 993 },
    inboxBusy: false,
    inboxNote: '',
    webPrefs: {
      hotkey_enabled: true, hotkey: 'F6',
      idle_detect_enabled: false, idle_threshold_s: 60,
    },
    // Optional screener extras so the bot can fill those fields instead of leaving them blank.
    // languages is a free-text string (comma-separated) the server splits; salary_floor lives in
    // targeting (user_config), the rest in the fact bank. Blank language/notice/availability →
    // the resolver applies the owner defaults (English / 2 weeks).
    extras: { primary_nationality: '', notice_period: '', availability: '', languages: '',
              salary_floor: null, gender: '' },

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
        // A brand-new user (nothing saved yet) should meet the AI-engine setup FIRST —
        // otherwise scoring/discovery silently fail with no models. Returning/partly-
        // onboarded users are NOT forced through it (they reach it via the dashboard
        // "Setup needed" panel); for them the first-incomplete jump above stands.
        if (this._isFreshProfile()) this.step = 'ai-engine';
        // Load the setup readiness checklist (LLM models + browser) for the AI-engine step.
        this.loadReadiness();
        // Reflect an in-flight background board search (e.g. the tab was reopened) and resume
        // polling so the user sees it finish even across a reload.
        try {
          const sr = await fetch('/api/onboarding/seed-boards/status');
          if (sr.ok) {
            this.seed = await sr.json();
            if (this.seed.status === 'running') this._pollSeed();
          }
        } catch (e) { /* ignore */ }
        // Same for an in-flight model pull / browser install.
        for (const [action, key] of [['pull-models', 'pull'], ['install-browser', 'browser']]) {
          try {
            const r = await fetch(`/api/setup/${action}/status`);
            if (r.ok) {
              this.setup[key] = await r.json();
              if (this.setup[key].status === 'running') this._pollSetup(action, key);
            }
          } catch (e) { /* ignore */ }
        }
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
        preferences: t.preferences || [],
      };
      this.targetingTitlesText = (t.titles || []).join('\n');
      this.targetingLocationsText = (t.locations || []).join('\n');
      this.telemetry = { ...this.telemetry, ...(state.telemetry || {}) };
      // Inbox: hydrate the non-secret fields only — the password is never echoed back,
      // so the field stays blank (re-entering it is how you change/confirm it).
      const ib = state.inbox || {};
      this.inbox = {
        user: ib.user || '',
        password: '',
        host: ib.host || 'imap.gmail.com',
        port: ib.port || 993,
      };
      this.webPrefs = { ...this.webPrefs, ...(state.web || {}) };
      this.extras = {
        primary_nationality: state.primary_nationality || '',
        notice_period: state.notice_period || '',
        availability: state.availability || '',
        languages: (state.languages || []).join(', '),
        salary_floor: (state.targeting || {}).salary_floor ?? null,
        gender: (state.eeo || {}).gender || '',
      };
    },

    _firstIncomplete() {
      // STEPS keys map 1:1 to status flags (with the special 'done'
      // pseudo-step at the end). Walk them in order; first false wins.
      const flagMap = {
        'ai-engine':    null,   // surfaced, not gated (models can lag onboarding)
        'contact':      'has_contact',
        'work-history': 'has_work_history',
        'skills':       'has_skills',
        'work-auth':    'has_work_auth',
        'targeting':    'has_targeting',
        'telemetry':    'has_telemetry_decision',
        'email':        null,   // optional — no completion gate
        'web-prefs':    null,   // optional — no completion gate
        'extras':       null,   // optional — no completion gate
      };
      for (const s of STEPS) {
        if (s.key === 'done') return 'done';
        const flag = flagMap[s.key];
        if (flag && !this.status?.[flag]) return s.key;
      }
      return 'done';
    },

    _isFreshProfile() {
      // "Brand new" = no contact saved yet. Used to land first-run users on the AI-engine step.
      return !this.status?.has_contact;
    },

    goto(key) {
      this.step = key;
      this.validationError = '';  // a stale "required field" message shouldn't follow the user
      // Re-hydrate from the (latest) status snapshot so the step's
      // fields reflect what was last saved, not what was in the
      // textbox before navigation.
      if (this.status) this._hydrate(this.status);
    },

    isDone(key) {
      // AI engine: not a completion gate; reflect whether the readiness checks are all green.
      if (key === 'ai-engine') {
        return this.setup.readiness.length > 0
          && this.setup.readiness.every(c => c.status === 'PASS');
      }
      // Email is optional + not in the completion gate; reflect whether it's connected.
      if (key === 'email') return !!this.status?.inbox?.enabled;
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

    // ---- AI-engine setup (readiness + in-app model pull / browser install) ----

    async loadReadiness() {
      try {
        const r = await fetch('/api/setup/readiness');
        if (!r.ok) return;
        const j = await r.json();
        this.setup.readiness = j.checks || [];
      } catch (e) { /* best-effort */ }
    },

    setupLlmUnreachable() {
      // True when Ollama itself isn't reachable (vs. just missing models) — surface a Get-Ollama link.
      const llm = (this.setup.readiness || []).find(c => c.name === 'llm');
      return !!llm && llm.status !== 'PASS' && /unreachable/i.test(llm.detail || '');
    },

    async startPull() {
      try {
        const r = await fetch('/api/setup/pull-models/start', { method: 'POST' });
        this.setup.pull = await r.json();
      } catch (e) {
        this.setup.pull = { status: 'error', error: String(e) };
        return;
      }
      this._pollSetup('pull-models', 'pull');
    },

    async startBrowserInstall() {
      try {
        const r = await fetch('/api/setup/install-browser/start', { method: 'POST' });
        this.setup.browser = await r.json();
      } catch (e) {
        this.setup.browser = { status: 'error', error: String(e) };
        return;
      }
      this._pollSetup('install-browser', 'browser');
    },

    _pollSetup(action, key) {
      if (this._setupPoll[action]) clearInterval(this._setupPoll[action]);
      this._setupPoll[action] = setInterval(async () => {
        try {
          const r = await fetch(`/api/setup/${action}/status`);
          if (!r.ok) return;
          this.setup[key] = await r.json();
          if (this.setup[key].status !== 'running') {
            clearInterval(this._setupPoll[action]);
            this._setupPoll[action] = null;
            this.loadReadiness();  // refresh the green/amber checklist
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

    /** Set the inline validation message + bail; returns false so callers can `if (!_invalid(...))`. */
    _invalid(msg) {
      this.validationError = msg;
      this.lastSavedNote = '';
      return false;
    },

    async saveContact() {
      this.validationError = '';
      // Required fields: without name + email the has_contact gate stays false and the
      // dashboard banner persists — surface that NOW instead of silently "saving" empties.
      if (!(this.contact.name || '').trim() || !(this.contact.email || '').trim()) {
        return this._invalid('Name and email are required.');
      }
      if (await this._post('/api/onboarding/contact', this.contact)) {
        this.step = 'work-history';
      }
    },

    async saveWorkHistory() {
      this.validationError = '';
      const roles = this.workHistory.map(w => ({
        company: w.company,
        title: w.title,
        start: w.start,
        end: w.end,
        bullets: (w.bulletsText || '')
          .split('\n')
          .map(line => line.trim())
          .filter(Boolean),
      }));
      // At least one role with a company + title — an empty list leaves has_work_history false.
      if (!roles.some(r => (r.company || '').trim() && (r.title || '').trim())) {
        return this._invalid('Add at least one role with a company and title.');
      }
      if (await this._post('/api/onboarding/work-history', { work_history: roles })) {
        this.step = 'skills';
      }
    },

    async saveSkills() {
      this.validationError = '';
      const skills = (this.skillsText || '')
        .split(/[,\n]/)
        .map(s => s.trim())
        .filter(Boolean);
      if (skills.length === 0) {
        return this._invalid('Add at least one skill.');
      }
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
        preferences: Array.isArray(this.targeting.preferences)
          ? this.targeting.preferences : [],
      };
      if (await this._post('/api/onboarding/targeting', payload)) {
        this.step = 'telemetry';
      }
    },

    // ---- connect email (Direction 4 Phase D) — optional outcome tracking ----------------------
    // Writes inbox config to user_config.json + the App Password to <data_dir>/.env. The endpoint
    // verifies the credentials with a live IMAP login first, so a typo'd password fails HERE.

    async saveInbox() {
      this.inboxNote = '';
      const user = (this.inbox.user || '').trim();
      const password = (this.inbox.password || '').trim();
      if (!user || !password) {
        this.inboxNote = 'Enter your email address and a 16-char App Password (or Skip).';
        return;
      }
      this.inboxBusy = true;
      try {
        const r = await fetch('/api/onboarding/inbox', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            user, password,
            host: this.inbox.host || 'imap.gmail.com',
            port: Number(this.inbox.port) || 993,
          }),
        });
        const body = await r.json().catch(() => ({}));
        if (!r.ok) {
          this.inboxNote = `Could not connect: ${body.detail || r.statusText}`;
          return;
        }
        // Refresh status so the step shows the connected checkmark, then advance.
        this.inbox.password = '';   // never keep the secret in component state
        try {
          const sr = await fetch('/api/onboarding/state');
          if (sr.ok) this.status = await sr.json();
        } catch (e) { /* best-effort */ }
        this.inboxNote = body.note || 'Email connected.';
        this.step = 'web-prefs';
      } catch (e) {
        this.inboxNote = `Error: ${e}`;
      } finally {
        this.inboxBusy = false;
      }
    },

    skipInbox() {
      this.inboxNote = '';
      this.step = 'web-prefs';
    },

    // ---- goal-elicitation chat (Direction 1, Phase B) ---------------------------------------
    // A scripted Q&A that fills the targeting form for the user who isn't sure what to type. The
    // server scripts the questions + parses each answer (LLM-as-parser, deterministic fallback);
    // we just relay turns and, when done, drop the draft into the form for REVIEW (never auto-save).

    async startGoalChat() {
      this.goalChat = {
        open: true, busy: true, done: false, applied: false,
        step: null, answer: '', messages: [], draft: {}, suggestions: [],
      };
      await this._goalPost('');  // empty step => server returns the first question
    },

    async sendGoalAnswer() {
      const text = (this.goalChat.answer || '').trim();
      if (!text || this.goalChat.busy || this.goalChat.done) return;
      this.goalChat.messages.push({ role: 'you', text });
      const step = this.goalChat.step;
      this.goalChat.answer = '';
      this.goalChat.busy = true;
      await this._goalPost(step, text);
    },

    async _goalPost(step, answer) {
      try {
        const r = await fetch('/api/onboarding/goal-chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ step, answer: answer || '', draft: this.goalChat.draft }),
        });
        if (!r.ok) {
          const e = await r.json().catch(() => ({}));
          this.goalChat.messages.push(
            { role: 'bot', text: `Sorry, something went wrong: ${e.detail || r.statusText}` });
          return;
        }
        const data = await r.json();
        this.goalChat.draft = data.draft || this.goalChat.draft;
        this.goalChat.step = data.next_step;
        this.goalChat.done = !!data.done;
        // "Suggest, you confirm" role widening: keep any chips the server offers until tapped
        // (only overwrite when a turn actually returns a fresh set).
        if (data.suggestions && Array.isArray(data.suggestions.roles)) {
          this.goalChat.suggestions = data.suggestions.roles;
        }
        if (data.reply) this.goalChat.messages.push({ role: 'bot', text: data.reply });
      } catch (e) {
        this.goalChat.messages.push({ role: 'bot', text: `Error: ${e}` });
      } finally {
        this.goalChat.busy = false;
      }
    },

    applyGoalDraft() {
      // Drop the chat's collected draft into the targeting form for review. Mirrors the résumé
      // prefill: fills fields, does NOT save — the user clicks Save & continue when happy.
      const d = this.goalChat.draft || {};
      if (Array.isArray(d.titles)) this.targetingTitlesText = d.titles.join('\n');
      if (Array.isArray(d.locations)) this.targetingLocationsText = d.locations.join('\n');
      this.targeting.remote_ok = d.remote_ok !== false;
      this.targeting.onsite_ok = d.onsite_ok !== false;
      if (d.salary_floor !== undefined && d.salary_floor !== null) {
        this.targeting.salary_floor = d.salary_floor;
      }
      if (d.seniority) this.targeting.seniority = d.seniority;
      this.targeting.preferences = Array.isArray(d.preferences) ? d.preferences : [];
      this.goalChat.applied = true;
      this.goalChat.open = false;
    },

    addSuggestedRole(role) {
      // Tap a suggested adjacent role → add it to the chat's draft titles (deduped) and drop the chip.
      const titles = Array.isArray(this.goalChat.draft.titles) ? this.goalChat.draft.titles : [];
      if (!titles.some(t => String(t).toLowerCase() === role.toLowerCase())) titles.push(role);
      this.goalChat.draft.titles = titles;
      this.goalChat.suggestions = this.goalChat.suggestions.filter(r => r !== role);
      this.goalChat.messages.push({ role: 'bot', text: `Added "${role}".` });
    },

    async saveTelemetry() {
      const payload = {
        enabled: !!this.telemetry.enabled,
        handle: this.telemetry.handle || null,
        relay_url: this.telemetry.relay_url || null,
      };
      if (await this._post('/api/onboarding/telemetry', payload)) {
        this.step = 'email';
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
        this.step = 'extras';
      }
    },

    async saveExtras() {
      const payload = {
        primary_nationality: this.extras.primary_nationality || '',
        notice_period: this.extras.notice_period || '',
        availability: this.extras.availability || '',
        // Free-text string; the server splits on commas/newlines, trims, dedupes.
        languages: this.extras.languages || '',
        // salary_floor lives in targeting (user_config); the endpoint routes it there.
        salary_floor:
          this.extras.salary_floor === null || this.extras.salary_floor === ''
            ? null : Number(this.extras.salary_floor),
        gender: this.extras.gender || '',
      };
      if (await this._post('/api/onboarding/extras', payload)) {
        this.step = 'done';
      }
    },

    skipExtras() {
      this.step = 'done';
    },
  };
}

window.onboarding = onboarding;
