# Auto Applier

A **local-first desktop service that automates job applications.** It discovers jobs (ATS APIs +
browser boards), scores each posting against a single **master fact bank**, generates a tailored
résumé + cover letter per job under a **fabrication guard**, and applies — fully automatic on clean
ATS forms, **assisted** (bot pre-fills, you submit) on hostile ones. It runs as an always-on
background **worker** controlled from a **local web dashboard**.

The core pipeline runs **fully locally and costs nothing** — local LLM (Ollama), local SQLite, no
cloud, no paid APIs. The one scoped exception is opt-in, scrubbed error telemetry (default **off**).

> **Status:** active personal / small-group project. The discovery → scoring → generation pipeline is
> validated end-to-end; live ATS apply is gated behind `--dry-run` by default. Also supports a
> **discovery + scoring only** mode where you apply manually and record it (see *Manual mode* below).

---

## Why it's built the way it is

This is a ground-up **v3** rewrite. The earlier version fought five root causes; v3 fixes each:

| v2 pain | v3 fix |
|---|---|
| CSV "database" (schema drift, no atomic writes, dedup hacks) | **SQLite is the system of record** (WAL, explicit transactions) |
| Browser-apply as the spine (anti-detect arms race) | **API-for-discovery + browser-for-apply** (a detection-risk router picks auto vs assisted) |
| State inferred, not stored | An explicit **job state machine** — one allowed-transitions table; dedup/resume/retry are queries on `state` |
| Synchronous per-platform pipeline | **Staged async workers** draining a status queue (`filter → score → optimize → apply`) |
| No observability | An automatic **event spine** — every stage emits start/ok/error/skip to `events.db`; CLI reads it directly |

Other load-bearing pieces: a **single master fact bank** (one source of truth; a résumé is *generated*
per job, never hand-maintained), a **fabrication guard** (a generated résumé may use only facts in the
bank — any unsupported claim drops the job to human review), capability-based **source adapters**
(Greenhouse / Lever / Ashby via public APIs; browser boards via JobSpy), and **maximal-stealth**
browser automation (patchright + real Chrome, manual login only, never retry through a CAPTCHA).

Full design rationale: [`docs/v3-architecture.md`](docs/v3-architecture.md).

---

## Architecture at a glance

```
discover ─▶ filter ─▶ score ─▶ optimize ─▶ apply
(ATS APIs   (embedding (LLM 7-axis  (résumé +     (auto on clean ATS,
 + boards)   pre-filter) vs profile)  cover letter  assisted on hostile;
                                       + guard)      positive confirm → APPLIED)
```

- **`config/`** — Pydantic settings (validated on load; `doctor` fails fast).
- **`db/`** — SQLite engine + schema + repositories (`app.db`: jobs / scores / applications).
- **`domain/`** — pure dataclasses + the job **state machine** (`state.py`).
- **`llm/`** — local Ollama completion + embeddings, versioned prompts.
- **`resume/`** — the fact bank, per-job generation, the fabrication guard, ATS-safe PDF render.
- **`sources/`** — capability-based ATS adapters + the browser apply drivers / stealth session.
- **`pipeline/`** — the staged workers, the always-on scheduler, retention, and the `@stage`
  observability wrapper.
- **`telemetry/`** — the always-local event sink + the opt-in, scrubbed remote mirror.
- **`web/`** — FastAPI app + dashboard (live pipeline, review queue, history), onboarding wizard.

---

## Install

Requires **Python 3.11+**.

```bash
pip install -e ".[v3]"        # core deps (FastAPI, pydantic, httpx, …)
av3 install-browser           # fetch Chromium on first run (real Chrome via channel is the primary path)
av3 init-db                   # create the data dir + app.db + events.db
av3 doctor                    # preflight: config, DBs, LLM reachable, backups — exits non-zero on FAIL
```

You'll also want [Ollama](https://ollama.com) running locally for scoring/generation.

## Run

```bash
av3 launch                    # one-click: starts the worker + server, opens the dashboard
av3 serve [--port P]          # web UI + background worker (power-user entry)
av3 run [--dry-run]           # always-on headless staged loop (the production loop)

# Per-stage workers (testing / doctor) — each drains one state:
av3 filter | score | optimize | apply   [--once] [--limit N]

# Observability (reads events.db directly — no log files):
av3 errors [--since 2h] [--stage X]      av3 stats [--since 7d]      av3 status
```

`--dry-run` is the default everywhere an apply could fire; `--no-dry-run` is the gated path that
submits real applications.

## Manual mode (discovery + scoring only, you apply)

Run the pipeline as a ranked-shortlist generator and record your own applications:

```bash
av3 shortlist --family data_platform --location remote --name my-list   # saved .md + .json
av3 applied --shortlist my-list --all      # mark the batch APPLIED (mode=manual)
av3 pass <job-id>                          # "looked, not interested"
av3 outcome <job-id> interview             # log a result (feeds analytics)
```

Manually-applied jobs leave the queue, are deduped out of future discovery, and never resurface.

---

## Reliability invariants (never compromised for throughput)

- **Manual login only; headed browser only; never retry through a CAPTCHA** → downgrade to assisted.
- **Mid-form break → fail fast to review, no retry** (retries risk duplicate/garbled submissions).
- **`APPLIED` only on a positive submit confirmation** — never inferred from a click.
- **Fabrication guard:** a generated résumé may use only facts in the bank; any unsupported company /
  title / date / credential / skill drops the job to human review.

## Data & privacy

All data lives under a local data dir (relocatable via `AV3_DATA_DIR`): `app.db` (system of record),
`events.db` (observability), `profile/master.json` (your fact bank), generated artifacts, and a
persistent browser profile. **All of it is gitignored** — nothing personal is committed. The only
network egress in the product is the opt-in, scrubbed, default-off telemetry mirror.

## Tests

```bash
pip install -e ".[v3,dev]"
pytest tests/                 # unit + contract suite (live smoke/eval markers excluded by default)
```

---

## License & scope

Personal/small-group project, shared as-is. Not affiliated with any job board or ATS; respects each
platform's manual-login and anti-automation posture by design.
