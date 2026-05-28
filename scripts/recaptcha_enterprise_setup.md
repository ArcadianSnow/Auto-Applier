# GCP setup — self-hosted reCAPTCHA Enterprise score rig (one-time, ~10 min, free)

Goal: measure **our own** reCAPTCHA *Enterprise* score with the real apply-path browser stack,
sending **zero** job applications. This resolves the last Phase-1 auto-pass unknown (does our
stack pass the Enterprise tier Greenhouse ships? — see `research/prior-art-and-methodology.md`
§6 and `docs/v3-architecture.md` §8c). Driven by `scripts/measure_enterprise_score.py`.

Everything here is on Google Cloud's **free tier** (reCAPTCHA Enterprise: first ~10,000
assessments/month free, no card-charge for this volume). You do need a GCP account with a
billing profile attached (required even for free-tier API enablement).

---

## Step 1 — Create / pick a GCP project

1. Go to <https://console.cloud.google.com/>.
2. Top bar → project dropdown → **New Project**. Name it e.g. `auto-applier-captcha-rig`.
3. Note the **Project ID** (NOT the display name — it's the lowercase id, often with a number
   suffix, e.g. `auto-applier-captcha-rig-1234`). This is `GCP_PROJECT_ID`.

## Step 2 — Enable the reCAPTCHA Enterprise API

1. With the project selected, go to
   <https://console.cloud.google.com/apis/library/recaptchaenterprise.googleapis.com>.
2. Click **Enable**. (If it asks to link billing, link your billing account — free tier still
   applies; this volume won't be charged.)

## Step 3 — Create a SCORE-BASED key with `localhost` allowed

1. Go to **Security → reCAPTCHA** (<https://console.cloud.google.com/security/recaptcha>).
2. Click **Create key**.
3. Fill in:
   - **Display name:** `av3-rig-localhost`
   - **Application type:** **Website**
   - **Domains:** add **`localhost`** (just type `localhost`, click Add). This is the key bit —
     score-based keys reject other domains by default, and Google explicitly supports adding
     `localhost` for a *development* key.
   - **Use checkbox challenge:** **OFF** / leave unchecked → this makes it a **score-based**
     (invisible) key, which is what mirrors Greenhouse's invisible Enterprise behavior.
   - Leave WAF / other toggles **off**.
4. Click **Create key**.
5. Copy the **Key ID** shown (a ~40-char string). This is the **public site key** →
   `RECAPTCHA_ENTERPRISE_SITE_KEY`.

## Step 4 — Create an API key for the Assessment call

The script reads the score back via the Assessment API using an API key (simpler than a
service account; fine for a throwaway local rig — Google recommends service accounts only for
production backends).

1. Go to **APIs & Services → Credentials**
   (<https://console.cloud.google.com/apis/credentials>).
2. **Create credentials → API key**. Copy the key string → `RECAPTCHA_ENTERPRISE_API_KEY`.
3. (Recommended) Click the new key → **Edit** → under **API restrictions** choose
   *Restrict key* → select **reCAPTCHA Enterprise API** → Save. This limits blast radius if the
   key leaks. Leave **Application restrictions** as *None* (the call comes from your machine).

## Step 5 — Set env vars and run

PowerShell (this session, or persist with `[Environment]::SetEnvironmentVariable(...,'User')`):

```powershell
$env:GCP_PROJECT_ID                = "auto-applier-captcha-rig-1234"   # your Project ID
$env:RECAPTCHA_ENTERPRISE_SITE_KEY = "6Lxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
$env:RECAPTCHA_ENTERPRISE_API_KEY  = "AIzaxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
python "scripts\measure_enterprise_score.py" --trials 3
```

Expected output: 3 trials, each printing `score=<0.0..1.0>` and a verdict, then an overall
mean/min/max and a verdict line.

---

## Reading the result

| Mean score | Meaning for the v3 auto path |
|---|---|
| **≥ 0.7** | Strong human signal → Greenhouse Enterprise auto-pass very likely. Auto path is viable on our current stack. |
| **0.5–0.7** | Passes the typical 0.5 threshold → auto viable, but sites can set stricter thresholds. |
| **0.3–0.5** | Borderline → treat Greenhouse as **assisted** (our documented safe default). |
| **< 0.3** | Fail → escalates/blocks → **assisted only**. Confirms the field-wide retreat to assisted. |

Whatever the number, write it into `research/prior-art-and-methodology.md` (§6, the S-series)
and `docs/v3-architecture.md` §8c, then update memory `[[project_v3_rewrite]]`.

## Honest caveats (don't over-read the number)

- **Our key ≠ Greenhouse's key.** We read *our* Enterprise key's score for *our* browser. The
  per-visitor risk score Google's ML assigns is driven by browser/IP/profile/behavior signals
  (visitor-side), so it's a faithful proxy — but Greenhouse may set a different pass threshold
  and feed extra server-side signals we can't replicate. Read this as "is our stack
  *plausibly* human-grade to Enterprise ML," not a guaranteed pass/fail of Greenhouse itself.
- **New keys see no traffic.** Google notes score-based keys "rely on seeing real traffic," so
  a brand-new key may score conservatively at first. If the first run looks low, re-run a few
  times over a few minutes (the script's `--trials` does several in one session); a persistently
  high score is the meaningful signal, a single low one on a cold key is not.
- **Action name** passed to `--action` must match between mint and assessment (the script keeps
  them in sync automatically). Using `apply`/`submit_application` only affects the `action`
  echo, not the score.
- This rig sends traffic only to Google's reCAPTCHA endpoints — **never** to any ATS or job
  posting. No application is submitted.
