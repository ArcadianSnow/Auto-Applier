# Prior Art & Methodology — Who Else Is Building Auto-Apply, and How

**Research date:** 2026-05-26 (overnight autonomous session)
**Question:** Are other people/repos solving the same problem (automated job application across
ATSes), what methodologies do they use, and what can v3 adopt?

**TL;DR.** The space is crowded but **bifurcated**: (1) *LinkedIn Easy-Apply* bots (the bulk,
incl. the famous AIHawk) — which v3 deliberately **cut**; and (2) *ATS form fillers* — fewer,
and the credible ones (commercial Simplify/LazyApply; OSS neonwatty/job-apply-plugin) are
**assisted** (fill → human submits), exactly v3's posture. **Nobody credibly does hands-off
auto-submit through reCAPTCHA Enterprise for free** — the consensus is it's "practically
impossible without paid solvers," which validates v3's assisted-on-hostile design. The single
most actionable external finding: **browser *engine* determines reCAPTCHA pass-ability** —
Chromium stealth (incl. our patchright+Chrome) caps at a ~0.3–0.5 reCAPTCHA v3 score, while
Firefox-based `invisible_playwright`/Camoufox reach ~0.90. That likely **reshapes our auto-path
browser choice** (see §4 + smoketests §6).

---

## 1. Landscape — who's doing this

| Tool | Type | Platforms | Auto or Assisted | Stack | License | Relevance to v3 |
|---|---|---|---|---|---|---|
| **AIHawk** (`feder-cr/Jobs_Applier_AI_Agent_AIHawk`, ~28k★) | OSS | **LinkedIn Easy Apply only** | Auto-submit | Selenium + OpenAI GPT, YAML config | AGPL-ish | LinkedIn-only (v3 cut it); but its LLM answer/resume generation = our resolver/generator. Reviews say "harder than it looks," brittle. |
| **JobSpy** (`speedyapply/JobSpy`, ~16k★) | OSS lib | LinkedIn, **Indeed**, Glassdoor, Google, ZipRecruiter, Bayt, Naukri | Discovery only (scrape) | Python, concurrent, → pandas | MIT-ish | **High** — drop-in **discovery** for browser boards (Indeed/ZipRecruiter) we'd otherwise hand-scrape. Indeed = "best, no rate limiting"; LinkedIn rate-limits ~p10 (proxies needed); ~1000/search cap. |
| **neonwatty/job-apply-plugin** | OSS (Claude Code plugin) | LinkedIn, Greenhouse, Ashby, Lever, Rippling, Workday | **Assisted — never submits w/o confirmation** | Dual MCP: Chrome MCP (authed) + Playwright MCP (form fill) | OSS | **Very high** — near-identical design to v3: résumé→profile JSON, smart field mapping, refuses passwords/payment, prompts on salary/visa. Strong validation of our model. |
| **santifer/career-ops** (+ our fork source `outscal/OpenJobs`) | OSS | Greenhouse, Ashby, Lever, company pages | Discovery + fit scoring (no auto-submit) | Claude Code, Playwright, Go dashboard, 14 "skill modes" | MIT | **High** — same Claude-Code-driven space; CV-vs-JD reasoning = our scoring; its dataset already seeds our discovery (ats-discovery-seeding.md). |
| **simonfong6/auto-apply** | OSS | Greenhouse, Lever, Workday, Jobvite | Auto | Python + Selenium + Docker | — | Medium — same ATS targets; Selenium (older than our Playwright); README thin, would need source read. |
| **wodsuz/EasyApplyJobsBot** | OSS | LinkedIn, Glassdoor, Greenhouse, Monster, Djinni | Auto-submit | Python Selenium | — | Low–med — Easy-Apply-centric. |
| **Simplify** | Commercial (extension) | LinkedIn + ATS autofill | **Assisted** — scores fit, fills, *you* submit | Chrome extension | — | Validates: fit-score gate + assisted submit = v3's Strict gate + assisted. |
| **LazyApply** | Commercial (extension) | LinkedIn, Indeed | **Assisted** — "Job GPT" fills, pauses before submit; high-volume | Chrome extension | — | High-volume autofill; pauses pre-submit (same safety as v3). |
| **Sonara / "apply until hired"** | Commercial | aggregated boards | Claims **fully auto** + daily digest | SaaS | — | The "spray" end of the market — the quality/spam risk v3 explicitly rejects (§7a "well-applied, not spam"). |

**Reading of the market:** the *credible* products converge on **assisted** (fill + human
submit) and **fit-scoring before applying**. The fully-auto "spray 1000 apps" tools are widely
reported as low-quality/spammy. v3's "wide cheap discovery → score → generate → guard →
auto-where-safe / assisted-otherwise" sits exactly where the credible tools are, with a
stronger correctness story (the fabrication guard, which none of these have).

---

## 2. Discovery methodology (how the field finds jobs)

- **ATS public read APIs** (what v3 does): Greenhouse/Lever/Ashby/SmartRecruiters/Workable —
  unauthenticated, structured, login-free. career-ops/OpenJobs and our adapters use this.
  *Best practice we already follow.*
- **Board scraping** (JobSpy): Indeed/ZipRecruiter/Glassdoor/LinkedIn via HTML/JSON scraping,
  concurrent, → DataFrame. Indeed has no rate limit; LinkedIn rate-limits fast (proxies).
  **Adopt JobSpy for the browser-board half of discovery** instead of hand-rolling scrapers
  (Phase 2). Caveat: scraping ToS risk + selector drift (JobSpy absorbs the maintenance).
- **Slug datasets + confirm-probe** (our ats-discovery-seeding.md) — the seeding piece; OpenJobs'
  `probe-ats.mjs` is the canonical mechanic. Already adopted.

## 3. Form-fill & answer methodology

- **Profile extraction → field mapping**: every serious tool extracts a structured profile
  from the résumé once, then maps to form fields. neonwatty stores `~/.claude-job-profile.json`;
  AIHawk uses YAML. **= v3's master fact bank (§6b).** Convergent design.
- **LLM for free-text answers**: AIHawk/LazyApply use an LLM ("Job GPT") to answer employer
  questions and tailor tone. **= v3's two-tier answer resolver (§8b)** — but v3 adds the
  semantic-match-first + bail-to-REVIEW + fabrication guard that they lack.
- **Stable per-ATS selectors for standard fields; discover custom Qs at runtime**: matches our
  ats-form-automation.md findings exactly.
- **Safety rails** (neonwatty): refuse passwords/payment/account-creation; confirm on
  salary/visa; never submit without confirmation. **v3 already encodes these** (manual login
  only; sensitive-field policy §8d; assisted submit). Good corroboration.

## 4. Stealth & CAPTCHA methodology — the decisive finding

**Two layers, as v3 already plans (§8c): fingerprint + behavior. But the 2026 data adds a
critical refinement we did NOT have — browser *engine* gates reCAPTCHA pass-ability.**

- **reCAPTCHA v3 score by engine (multiple 2026 sources):**
  - **Chromium-based stealth — incl. patchright+Chrome, nodriver, CloakBrowser — caps ~0.3–0.5**
    ("the Chromium reCAPTCHA ceiling"). reCAPTCHA's typical pass threshold is **0.5**, and a
    score <0.3 escalates to a visible challenge. So Chromium is **borderline-to-failing** on
    invisible reCAPTCHA, and **reCAPTCHA Enterprise wants higher still**.
  - **Firefox-based — `invisible_playwright` (feder-cr) and Camoufox — reach ~0.90**, well above
    threshold, via **C++ source-level patches** (navigator/screen/WebGL/canvas/fonts/audio/WebRTC/
    timezone), "no JavaScript lies for the detector to catch."
- **Cloudflare/bot-detection benchmark (separate axis, 31 targets):** nodriver 90.3% (zero hard
  blocks; AGPL, no Playwright drop-in, async rewrite needed), patchright+`channel=chrome` 80.6%,
  Camoufox 80.6%. **But this benchmark explicitly does NOT test reCAPTCHA** — so it ranks
  Cloudflare evasion, not our use case. nodriver wins Cloudflare yet is Chromium → likely same
  reCAPTCHA ceiling.
- **reCAPTCHA Enterprise (Greenhouse): consensus = unbeatable for free.** Practitioner/solver
  writeups: "without specialized [paid] services, bypassing is practically impossible";
  Enterprise is the toughest tier (IP reputation + browser-history ML + full fingerprint).
  Paid solvers (2captcha/capsolver/nextcaptcha) exist but cost money + variable success +
  ToS-risk → **out of bounds for v3 (zero-cost).** Assisted "human solves at the CAPTCHA step"
  is noted as fine for small/personal scale (= v3), "not viable at industrial scale."

**Implication for v3 (updates §8c):**
1. Our cross-ATS survey said GH=100% Enterprise, Ashby=50% Enterprise/50% invisible reCAPTCHA,
   Lever=invisible hCaptcha. Combine with the engine finding:
   - **Greenhouse Enterprise → assisted** regardless of engine (free auto-pass not realistic).
   - **Ashby/GH *invisible* reCAPTCHA, and Lever hCaptcha → auto is plausible ONLY with a
     Firefox-based engine (~0.9 score)**; our current patchright+Chrome (~0.3–0.5) likely
     under-performs here.
2. **Action:** evaluate swapping the *auto-path* browser to **`invisible_playwright` or Camoufox
   (Firefox)** for reCAPTCHA/hCaptcha sites; keep patchright+Chrome (or nodriver) for
   Cloudflare-fronted browser boards. This is a backend-selection-per-site decision, not a
   wholesale swap. (Both Firefox tools are MIT/free; Camoufox already an optional dep.)
3. **Smoketest needed:** measure our *actual* patchright+Chrome reCAPTCHA v3 score, then compare
   to a Firefox engine. Test page: `antcpt.com/score_detector` (no submit, safe). → §6.

## 5. What v3 adopts / rejects

**Adopt:**
- **JobSpy** for browser-board discovery (Indeed/ZipRecruiter/Glassdoor) — Phase 2, instead of
  hand-rolled scrapers. (Wrap it behind our source-capability model.)
- **Firefox stealth engine (`invisible_playwright`/Camoufox) for the reCAPTCHA/hCaptcha auto
  path** — pending the §6 smoketest confirming the score gap on our machine.
- **neonwatty's safety rails** as a checklist (refuse password/payment/account-creation; confirm
  salary/visa) — we have most; verify completeness when building the resolver.

**Reject / already-better:**
- LinkedIn Easy-Apply automation (AIHawk et al.) — v3 cut LinkedIn (TLS fingerprinting + ToS).
- Paid CAPTCHA solvers — violates zero-cost; ToS-risk.
- Fully-auto "spray" (Sonara-style) — violates "well-applied, not spam" (§7a); v3's guard +
  fit-gate is the differentiator.

**v3's genuine differentiators vs all of the above:** (a) the **fabrication guard** (none of
them verify generated-résumé claims against a fact bank — they'll happily let an LLM lie);
(b) **SQLite state machine + observability spine** (these are mostly fire-and-forget scripts);
(c) **discovery-via-API breadth** feeding scoring before any browser opens.

---

## 6. Smoketest results

Harness: `scripts/smoketest_captcha_score.py` (loads a public reCAPTCHA v3 tester with our
stealth `BrowserSession`, extracts the score; never submits anything).

### S1 — our current stack's reCAPTCHA v3 score → **0.9 (PASS), confirmed ×2** ⭐

Ran patchright + **real Chrome** (`channel=chrome`) + our **persistent profile** against
`antcpt.com/score_detector`. Result: **score = 0.9** (max), confirmed on two runs 75 s apart
(raw readout: *"Score: 0.9; User Agent and OS: Chrome 14…"*). 0.9 ≥ 0.5 pass threshold →
invisible reCAPTCHA v3 would **pass silently**.

**This REVERSES the blogs' "Chromium reCAPTCHA ceiling ~0.3–0.5" claim *for our setup*** — and
the reason is the actionable insight: reCAPTCHA v3 heavily weights **IP reputation + browser
history + cookies**, and our stack already carries all three (real Chrome binary via `channel`,
a **persistent shared profile** with accumulated history, a residential IP). The blogs measured
*clean/fresh* fingerprints. So **profile reputation is a first-class stealth lever, and we
already pull it.** Consequences:
- **We likely do NOT need to switch to a Firefox engine** (invisible_playwright/Camoufox) for
  the auto path — our Chromium stack is already passing-grade on standard invisible reCAPTCHA.
  Keep invisible_playwright/Camoufox documented as a **fallback** if a fresh profile or a
  specific site ever scores low. (De-prioritizes the §4 "switch to Firefox" lean.)
- **The auto-pass outlook for the *invisible reCAPTCHA* tier is now GOOD** — Ashby's lighter
  50% and any non-Enterprise Greenhouse posting should auto-pass on our current stack.
- **Caveat — Enterprise still unresolved.** antcpt measures *standard* v3, not reCAPTCHA
  **Enterprise** (what Greenhouse ships, 100% in our survey). Enterprise adds IP-reputation +
  browser-history ML + extra signals and is stricter; a 0.9 standard-v3 score does **not**
  guarantee a passing Enterprise score. That still requires a real submit to learn (gated).

**Net auto-pass picture (presence survey × engine score):** Lever (hCaptcha) and the
invisible-reCAPTCHA tiers of Ashby/GH look **auto-viable on our existing stack**; GH/Ashby
**Enterprise** remains the one genuine unknown, pending a gated real-submit test.

### S2 — Firefox engine A/B (invisible_playwright/Camoufox) → **DEFERRED (non-urgent)**

Planned to A/B a Firefox engine (claimed ~0.9 v3). **Skipped tonight on purpose:** S1 shows our
Chromium stack already scores 0.9, so a Firefox A/B wouldn't change the recommendation for
standard v3, and it can't resolve Enterprise either. Documented as a ready fallback (MIT, free,
2-line drop-in: `pip install git+…/invisible_playwright; python -m invisible_playwright fetch`).

### S3 — JobSpy discovery trial → **works out of the box** ✅

`pip install python-jobspy`, then a 5-result Indeed query ("data analyst", Remote, US)
returned 5 real postings with `title, company, location, date_posted, job_url, site` — **no
proxy, no auth, no error** (confirms its "Indeed = best scraper, no rate limiting" claim).
Sample: Cigna "Business Analytics Advisor", Calendly "Senior Data Analyst", Allstate
"Reporting Automation & Dashboard Senior". → **Adopt JobSpy as the Phase-2 browser-board
discovery source** (Indeed/ZipRecruiter/Glassdoor), wrapped behind our source-capability model,
instead of hand-rolling + maintaining scrapers. Note for LinkedIn via JobSpy: rate-limits ~p10,
needs proxies — and LinkedIn is cut from v3 anyway, so use JobSpy for Indeed/ZipRecruiter only.

### S4 — reCAPTCHA *Enterprise* self-hosted rig → **BUILT, awaiting user GCP key + run** ⏳

The §6-note option (b) measurement rig now exists: `scripts/measure_enterprise_score.py` +
setup `scripts/recaptcha_enterprise_setup.md`. It serves a one-page Enterprise score-key harness
on `localhost`, mints tokens with our real apply-path `BrowserSession` (patchright + real Chrome
+ persistent profile), and reads the score back via the Assessment API — **zero job
applications**. Verified buildable end-to-end *except* the live measurement: imports + guard
path + HTML-template `.format()` (JS braces) + local server serving all green; the request shape
(`POST .../projects/{project}/assessments?key={api_key}` with `event.{token,siteKey,
expectedAction}`) and **API-key auth** (vs service account) both confirmed against Google docs;
`localhost` is an allowed domain for a score-based *dev* key (Google explicitly supports it).

**Blocked on the user only:** provision a free Enterprise score-based key + API key in their GCP
account (steps in the setup doc), set 3 env vars, run `--trials 3`. **RESULT: <pending — fill in
mean/min/max + verdict here after the run>.** Honest caveat baked into the doc: our key ≠
Greenhouse's key (different threshold + server-side signals), and new keys score conservatively
until they've seen traffic — read a *persistently* high score as the signal, not one cold-key low.

### Smoketest bottom line

1. **Our existing stealth stack already scores 0.9 on standard reCAPTCHA v3** — the auto path is
   more viable than the survey alone implied, for everything except reCAPTCHA *Enterprise*. Keep
   patchright + real Chrome + persistent profile; Firefox engine is a documented fallback, not a
   needed switch.
2. **reCAPTCHA Enterprise (Greenhouse) is the sole remaining auto-pass unknown** — only a gated
   real submit resolves it.
3. **Adopt JobSpy** for Phase-2 board discovery (validated working).

### Note — can we measure our reCAPTCHA *Enterprise* score safely? (the GH unknown)

**No public per-visitor Enterprise score-detector exists.** Enterprise scores are computed
server-side and returned only to the **site administrator** via the Google Cloud reCAPTCHA
assessment API / console — never exposed to the client (this is *why* antcpt can show a
standard-v3 score: antcpt owns the key + a backend; no equivalent exists for arbitrary
Enterprise sites). So the GH-Enterprise auto-pass unknown can be resolved only by:
- **(a) a gated real submit** to a live Greenhouse Enterprise form (user decision — sends a
  real application), or
- **(b) a self-hosted measurement rig (free, safe):** create our OWN Google Cloud reCAPTCHA
  **Enterprise score-based key** (free tier ~10k assessments/mo), serve a tiny local HTML page
  with it, load it with our `BrowserSession` to mint a token, then call the assessment API with
  our secret to read *our own* Enterprise score. Needs the user's GCP account/consent but sends
  **zero** job applications. **Recommended next de-risking step for the GH-Enterprise question.**

## 7. Strategic synthesis — where v3 sits vs the field

**Three independent credible tools — `neonwatty/job-apply-plugin`, `career-ops`, and the
commercial Simplify/LazyApply — all REFUSE to auto-submit** (human clicks final submit).
`career-ops` is explicit: *"The system never submits an application — you always have the final
call,"* and it carries **no CAPTCHA/anti-detect code at all** because not-auto-submitting means
never fighting a CAPTCHA. The only tools that claim hands-off auto-submit are either LinkedIn
Easy-Apply (a structurally easier, login-gated surface — and cut from v3) or the "spray 1000
apps" SaaS (Sonara) widely flagged as spammy/low-quality.

**Implication — this is the most important strategic read of the night:**
- **v3's `BROWSER_AUTO`-where-safe ambition is *more aggressive than the entire established
  field*.** That's a genuine differentiator **iff** the auto-pass holds — but the field's
  unanimous retreat to assisted (plus GH = 100% Enterprise) says the safe bet is: **ship
  assisted as the rock-solid default; treat auto as a measured, per-tier *upside*** earned only
  where the data supports it (Lever hCaptcha + Ashby/GH *invisible* reCAPTCHA, where our stack's
  0.9 standard-v3 score makes silent passing plausible). **Never bet the product on
  auto-through-Enterprise.**
- v3's *real* moats over all of them: the **fabrication guard** (none verify generated-résumé
  claims — they let the LLM lie), the **SQLite state machine + observability spine** (they're
  fire-and-forget scripts), and **API-first discovery breadth** feeding scoring before any
  browser opens. Lean into these; don't over-index on winning the CAPTCHA arms race.
- Adoptable patterns confirmed across the field: structured profile → field mapping (= fact
  bank), LLM for free-text answers (= resolver, + our guard), per-listing résumé adaptation
  (= generation), YAML/JSON portal config, Playwright PDF generation, human-approval gate.

---

## Sources

- AIHawk: https://github.com/feder-cr/Jobs_Applier_AI_Agent_AIHawk ; review: https://applyghost.com/blog/ai-hawk-review
- JobSpy: https://github.com/speedyapply/JobSpy ; https://pypi.org/project/python-jobspy/
- neonwatty/job-apply-plugin: https://github.com/neonwatty/job-apply-plugin
- santifer/career-ops: https://github.com/santifer/career-ops
- simonfong6/auto-apply: https://github.com/simonfong6/auto-apply
- invisible_playwright (Firefox, 0.90 v3): https://github.com/feder-cr/invisible_playwright
- Anti-detect benchmark 2026 (nodriver/patchright/camoufox/curl_cffi): https://ianlpaterson.com/blog/anti-detect-browser-benchmark-patchright-nodriver-curl-cffi/
- Stealth tool comparison: https://github.com/pim97/anti-detect-browser-tools-tech-comparison ; https://github.com/techinz/browsers-benchmark
- reCAPTCHA v3 score test: https://antcpt.com/score_detector/ ; https://cleantalk.org/recaptcha-v3-score-test
- reCAPTCHA Enterprise "practically impossible without paid solvers": https://habr.com/en/articles/898198/ ; https://2captcha.com/p/recaptcha_enterprise
- Commercial tool comparisons: https://blog.fastapply.co/auto-apply-jobs-tools-compared-2026 ; https://jobright.ai/blog/2025s-best-auto-apply-tools-for-tech-job-seekers/
- Zapply (Greenhouse/Lever reCAPTCHA Enterprise hacking): https://vanja.io/zapply-hacking-greenhouse-and-lever/
