# ATS Market Landscape — Who Uses What, and What v3 Can Actually Reach

**Research date:** 2026-05-26. **Question:** which ATSes dominate, by segment, and does v3's
ATS choice (Greenhouse/Lever/Ashby/Workable/SmartRecruiters) cover a worthwhile slice?

## Market shape (2025)

- **Total ATS market** ~$3.3B (2025) → ~$4.9B (2030). North America = 37% (Fortune-500 + vendor
  HQs + regulation).
- **By segment:** large enterprises = **67% of revenue** (high volume, multi-site, deep HRIS/
  payroll integration); SMEs = 33% but **fastest-growing (~12% CAGR)**.
- **#1 by global share: iCIMS**, ahead of Oracle, Workday, Greenhouse. At the **enterprise** tier,
  **Workday / Oracle (Taleo) / SAP SuccessFactors** dominate via integrated HCM suites; **iCIMS /
  SmartRecruiters / Greenhouse** are the specialized challengers.
- **By company size (the part that matters for v3):**
  - **Startups / growth-stage → Ashby, Lever, Workable** (modern UI, cheap, fast setup, API-first).
    Ashby is surging — **4,000+ companies, 125+ switching off Greenhouse in the last year.**
  - **Mid-market / process-driven → Greenhouse** (structured hiring, biggest integration ecosystem).
  - **Enterprise → Workday Recruiting / iCIMS / Oracle / SAP** (governance, volume, HCM integration).

## What this means for v3 (the addressable surface)

v3's "no-login, public-API, slug-discoverable" ATS tier =
**Greenhouse + Lever + Ashby + Workable + SmartRecruiters**. Mapping to the market:

| Tier | ATSes | Login? | Public read API? | v3 reach |
|---|---|---|---|---|
| **Startup/growth** | Ashby, Lever, Workable | No | Yes (slug) | ✅ **Core target** — modern, API-open, lighter CAPTCHA (Lever hCaptcha; Ashby half-Enterprise) |
| **Mid / process-driven** | Greenhouse, SmartRecruiters | No | Yes (token) | ✅ Target — but GH = 100% reCAPTCHA **Enterprise** (→ assisted) |
| **Enterprise** | **Workday, iCIMS, Oracle/Taleo, SAP SF** | **Yes (per-tenant)** | **No** (closed/partner-gated) | ❌ **Out of scope** (spec §11) — per-tenant logins + closed APIs break the model |

**Honest scoping conclusion:**
- v3 well-covers the **modern-ATS tech-job segment** — which is exactly where the user's audience
  (data analyst / data engineer / SWE roles at startups + growth + mid-market tech) actually
  applies. The API-open tier is *built* for the kind of company that posts these roles.
- v3 **does not** reach the **enterprise-volume** segment (Workday/iCIMS/Oracle/SAP = the 67%
  revenue majority). Those need per-tenant accounts + closed APIs + (Workday) notoriously
  automation-hostile multi-step forms. Correctly deferred/avoided (§11, §6a). A future "assisted
  only, manual-login, per-tenant" Workday mode is possible but is a different, heavier product.
- **Net:** v3's target tier is a real and growing slice (Ashby's trajectory shows the modern tier
  is *gaining*), and it's the right slice for a tech job-seeker. Just don't claim "applies
  everywhere" — it applies across the **modern, API-discoverable ATS tier**, which is a feature
  (clean forms, public discovery, lighter anti-bot), not a limitation, for this audience.

## Prioritization (combines this with the CAPTCHA survey + score smoketest)

Auto-apply viability ranking for the v3 build (drivability × CAPTCHA × inventory):
1. **Lever** — invisible hCaptcha (no Enterprise), server-rendered, most selector-stable; our
   stack scores 0.9 v3. *Lead the auto path here.* Caveat: sparse open-role inventory.
2. **Ashby** — 50% lighter invisible reCAPTCHA (auto-viable on our 0.9 stack), 50% Enterprise
   (assisted); React SPA needs extra driver work; rich inventory + fast-growing.
3. **Greenhouse** — 100% reCAPTCHA Enterprise → **assisted**; richest inventory + biggest
   integration footprint, so still worth discovery + assisted apply.
4. **Workable / SmartRecruiters** — add next (no-login, slug APIs); CAPTCHA unmeasured (Phase 2).
5. **Browser boards (Indeed / ZipRecruiter)** via **JobSpy** — discovery breadth; apply is
   assisted/auto per the risk router.
6. ~~Workday / iCIMS / Oracle / SAP~~ — out of scope (login + closed API).

## Sources

- ATS market size + #1 iCIMS + enterprise share: https://www.imarcgroup.com/applicant-tracking-system-market ;
  https://www.icims.com/2025-apps-run-the-world-report/ ;
  https://www.globenewswire.com/news-release/2025/09/16/3150934/28124/en/applicant-tracking-system-market-outlook-report-2025-2030-with-profiles-of-oracle-icims-sap-workday-bullhorn-greenhouse-software-smartrecruiters-ukg-adp-and-jobvite.html
- Segment (startup vs enterprise) ATS choice + Ashby growth: https://www.index.dev/blog/greenhouse-vs-lever-vs-ashby-ats-comparison ;
  https://arc.dev/employer-blog/13-best-ats-for-startups-ashby-greenhouse-lever/ ;
  https://www.outsail.co/post/greenhouse-vs-lever-vs-ashby
