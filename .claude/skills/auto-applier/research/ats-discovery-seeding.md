# ATS Discovery Seeding — How to Get Company Board Identifiers

**Research date:** 2026-05-26
**Question:** How does a tool obtain the per-company "board identifier" (token / slug / site)
needed to query ATS job-posting APIs for discovery, when no ATS exposes a
"list all companies" endpoint?

**Short answer:** There is **no official enumeration endpoint** for any major ATS. The board
identifier is a per-company slug you must already know. You obtain a working list by **(a) reusing
existing open-source slug datasets, (b) harvesting slugs from public search-engine indexes, and
(c) confirm-probing candidate slugs against the free public APIs.** All three are practical and
low-risk; (a) is the fastest path to a Phase-1 list of 50-200 companies.

---

## Per-ATS reference

### Greenhouse

- **Public read API:** `https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs`
  (add `?content=true` for full descriptions; `/jobs/{id}` for one posting).
- **What `{board_token}` is:** the company's chosen job-board slug. It is exactly the path segment
  in the company's hosted board URL: `https://boards.greenhouse.io/{board_token}` (or the embed form
  `https://boards.greenhouse.io/embed/job_board?for={board_token}`). Examples confirmed in search
  results: `stripe`, `airbnb`, `figma`, `anthropic`, `databricks`, `coinbase`, `cloudflare`, `lyft`,
  `doordash`, `ecobee`, `cargurus`, `tripadvisor`, `justworks`, `cribl`, `levelaccess`, `togetherwork`.
- **Auth / ToS:** Greenhouse states "Job Board data is publicly available, so authentication is not
  required for any GET endpoints." Only the *application-submit* POST needs an API key. The docs page
  publishes **no rate limit and no separate ToS** for the read API.
- **Enumeration:** **No list-all-boards endpoint exists.** The slug is usually a clean, guessable
  version of the company name, but blind enumeration of the full keyspace is wasteful and abusive —
  use harvesting + confirm-probing instead (below).
- **Token casing note:** tokens are sometimes capitalized in the URL (e.g. `Cribl`) but the API is
  generally case-insensitive on the token; normalize to lowercase and keep the original as a fallback.

### Lever

- **Public read API:** `https://api.lever.co/v0/postings/{site}?mode=json`
  (human board: `https://jobs.lever.co/{site}`).
- **What `{site}` is:** a single per-company site name, "usually your company name with no spaces."
  Each company has exactly one site. Lever's own board is `lever`.
- **Auth / ToS:** Postings in the `published` state are explicitly public. Lever's README openly
  acknowledges: "These jobs may be scraped by third parties." Non-published jobs are hidden from the API.
- **Rate limits:** The only documented limit is on the **application-POST** path (HTTP 429 if more than
  ~2 application POSTs/second). No documented limit on the read/postings GET, but Lever reserves the
  right to change limits "without warning … to maintain the stability of Lever's systems."
- **Enumeration:** No list-all endpoint. Same harvest + confirm-probe approach as Greenhouse.

### Ashby

- **Public read API:** `https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`
  (human board: `https://jobs.ashbyhq.com/{slug}`).
- **What `{slug}` is:** the organization's "jobs page name" — the final path segment of the hosted
  board URL (e.g. `Ashby` in `https://jobs.ashbyhq.com/Ashby`). **Often case-sensitive** — preserve
  the casing exactly as seen on the public board.
- **Auth / ToS:** the `posting-api` board endpoint is unauthenticated and public; no filtering/search
  on this endpoint. Advanced endpoints require a customer API key. No published read rate limit.
- **Enumeration:** No list-all endpoint. Harvest + confirm-probe.

### Others worth supporting (same pattern, slug-keyed public APIs)
SmartRecruiters, Recruitee, Workable, Teamtailor, Personio, Breezy, BambooHR, Jobvite, Join.com — all
use a per-company slug and are slug-probed by the open datasets cited below. (Workday and Rippling use
a per-tenant host and are messier — defer past Phase 2.)

---

## Aggregation strategies (ranked by practicality + legality)

| Rank | Strategy | Cost | Legality | Effort | Yield |
|------|----------|------|----------|--------|-------|
| 1 | **Reuse open MIT-licensed slug datasets** (OpenJobs, ats-scrapers/jobhive) | Free | Clean (MIT) | Low | 7k-86k slugs |
| 2 | **Search-engine harvesting** (`site:boards.greenhouse.io`, `site:jobs.lever.co`, `site:jobs.ashbyhq.com`) | Free | Public-index, ToS-OK if polite | Low-med | Thousands |
| 3 | **Confirm-probing candidate slugs** against the free public APIs | Free | Low risk if rate-limited | Med | High precision |
| 4 | **Tech-stack/technographic providers** (BuiltWith, TheirStack, etc.) | Paid ($295+/mo) | Clean but **violates project "zero cost" rule** | Low | Large, curated |
| 5 | **Blind keyspace enumeration** of slugs | Free | **Abusive — avoid** | High waste | Low |

**Detail on the top three (the only ones that fit "free + local + low-risk"):**

1. **Open datasets (best starting point).** Two MIT-licensed repos already solve the seeding problem:
   - **`outscal/OpenJobs`** (fork of `santifer/career-ops`) — `companies_v2.json` holds **~12,144
     companies**, ~7,007 with ATS links, ~2,100 routable to working adapters, across 13 ATS adapters
     (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Workday, Teamtailor, Recruitee, Personio,
     Breezy, BambooHR, Jobvite, Join.com). Ships `probe-ats.mjs` (slug-probes un-routed companies
     against 7 public ATS APIs) and `merge-probe-hits.mjs` (review/merge loop). MIT — directly reusable.
   - **`kalil0321/ats-scrapers` (jobhive)** — larger: **86,000+ companies across 47 ATS platforms**,
     with one CSV per ATS under `ats-companies/`. Grown by community PRs. MIT — directly reusable.
   - These give you a ready slug list per ATS *today*. Filter to Greenhouse/Lever/Ashby and to the
     job titles your users target.

2. **Search-engine harvesting (verified working).** A `site:` dork against the public board hosts
   returns real company boards. Confirmed live: `site:boards.greenhouse.io` returns `justworks`,
   `ecobee`, `cargurus`, `doordash`, `cribl`, `jobleads`, `levelaccess`, `tripadvisor`, `togetherwork`,
   `hellofresh`, etc. Equivalent dorks: `site:jobs.lever.co`, `site:jobs.ashbyhq.com`,
   `inurl:boards.greenhouse.io`. Parse the slug out of the URL path / the `for=` query param.
   This is reading a public search index — low ToS risk if you query politely and don't hammer.

3. **Confirm-probing (turns guesses into a verified list).** For any candidate slug (from a company
   name, a harvested URL, or a guess), issue **one** GET to the public API:
   - Greenhouse: `GET /v1/boards/{slug}/jobs` → HTTP 200 + non-empty `jobs[]` = valid.
   - Lever: `GET /v0/postings/{slug}?mode=json` → 200 + array = valid.
   - Ashby: `GET /posting-api/job-board/{slug}` → 200 + `jobs` = valid.
   Cache the result (valid/invalid) so you never re-probe the same slug. This is exactly what
   OpenJobs' `probe-ats.mjs` does and is the reusable mechanic for our own discovery layer.

**Why not BuiltWith/technographic providers:** they cleanly map "company → ATS" at scale, but the
useful tiers are paid ($295-$995/mo for BuiltWith). That violates this project's hard **zero-cost /
local-only** constraint, so it's noted for completeness only, not recommended.

---

## Recommended Phase 1 seeding approach (Greenhouse, ~50-200 tokens)

Goal: a curated, **verified** starter list of 50-200 Greenhouse board tokens with effectively zero
ToS risk and zero cost.

1. **Pull the open datasets.** Clone/download `outscal/OpenJobs` `companies_v2.json` and the
   `greenhouse` CSV from `kalil0321/ats-scrapers`. Extract every Greenhouse slug. (MIT-licensed —
   keep a short attribution note in our repo.)
2. **Filter to relevance.** Keep companies whose typical roles match the user's target titles
   (Data Analyst / Data Engineer, etc.) and/or a hiring-region match. This naturally trims thousands
   of slugs to a few hundred relevant ones.
3. **Confirm-probe each candidate once.** For each slug, `GET /v1/boards/{slug}/jobs`. Keep only slugs
   returning 200 with ≥1 open job. Rate-limit to ~1 request/second, set a descriptive `User-Agent`,
   and **cache the result** (mirror the existing 72h LLM-cache pattern) so re-runs don't re-hit the API.
4. **Persist as a seed file.** Store the verified set as `data/ats_boards.csv`
   (columns: `ats, slug, company_name, last_seen_jobs, last_checked`) so it's Excel-inspectable like
   the project's other CSVs and survives across continuous-run cycles.
5. **Land at 50-200 tokens.** Steps 1-3 comfortably exceed 200 verified Greenhouse tokens; truncate to
   the top-N by relevance for the initial run.

This needs no scraping of company sites and no paid service — just two public datasets and one polite
GET per candidate.

---

## Recommended Phase 2 approach (scaling the list)

1. **Add a harvest crawler.** Periodically run `site:boards.greenhouse.io`, `site:jobs.lever.co`,
   `site:jobs.ashbyhq.com` dorks (and `inurl:` / `for=` variants), parse slugs out of result URLs,
   and feed new candidates into the confirm-probe step. Schedule this in continuous-run / refinement
   windows so it never competes with the apply loop.
2. **Generalize the probe to all slug-keyed ATSes.** Port OpenJobs' `probe-ats.mjs` logic into a small
   `browser/ats_probe.py` (or a discovery adapter) covering Greenhouse, Lever, Ashby first, then
   SmartRecruiters / Recruitee / Workable / Breezy. One GET per (slug, ATS), cached, fail-closed.
3. **Name-to-slug generation.** From any company name we encounter (e.g. via LinkedIn discovery, which
   is already discovery-only in this project), generate candidate slugs (lowercased, de-spaced, common
   variants) and confirm-probe them — converting "company name" into "board token" automatically.
4. **Incremental refresh + decay.** Re-check known boards on a cadence; drop slugs that 404 or return
   zero jobs for N consecutive checks (companies churn ATSes). Track `last_checked` / `last_seen_jobs`
   in the seed CSV.
5. **Contribute back (optional).** New verified slugs can be PR'd back to the MIT datasets — cheap
   community goodwill and keeps our list aligned with upstream.

**ToS / rate-limit guardrails for both phases:** read endpoints are explicitly public on Greenhouse,
Lever, and Ashby; only the *apply* endpoints are auth-gated and rate-limited. Stay polite anyway —
single-threaded ~1 req/s per host, descriptive User-Agent, aggressive caching, exponential backoff on
429/5xx, and never probe the full slug keyspace blindly. This keeps discovery firmly inside the
"publicly available data" framing each vendor documents.

---

## Sources (verified)

- Greenhouse Job Board API (auth-free public GET, board_token, no list-all endpoint):
  https://developers.greenhouse.io/job-board.html
- Greenhouse "find your board token in the board URL" + customer scale (220k+ companies):
  https://support.greenhouse.io/hc/en-us/articles/360007039771-Token-glossary ,
  https://apify.com/automation-lab/greenhouse-jobs-scraper
- Lever Postings API (site name = company name no-spaces; published jobs public/scrapable; 429 on apply POSTs):
  https://github.com/lever/postings-api/blob/master/README.md
- Ashby public posting-api (jobs page name as slug; unauthenticated board endpoint):
  https://developers.ashbyhq.com/docs/public-job-posting-api
- OpenJobs dataset + probe-ats.mjs (12,144 companies, 7 ATS slug-probe, MIT):
  https://github.com/outscal/OpenJobs
- jobhive / ats-scrapers dataset (86,000+ companies, 47 ATS, per-ATS CSVs, MIT):
  https://github.com/kalil0321/ats-scrapers
- Google dork verification — live `site:boards.greenhouse.io` results (justworks, ecobee, cargurus,
  doordash, cribl, tripadvisor, togetherwork, hellofresh, levelaccess, jobleads): web search, 2026-05-26
- Technographic/tech-stack detection (BuiltWith pricing $295+/mo; ATS detection coverage):
  https://trends.builtwith.com/websitelist/Greenhouse ,
  https://theirstack.com/en/blog/best-technographic-data-apis
- Additional open scrapers confirming the slug-keyed pattern:
  https://github.com/adgramigna/job-board-scraper
