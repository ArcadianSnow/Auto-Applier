"""Measure OUR reCAPTCHA *Enterprise* score safely (zero job applications).

This is the self-hosted measurement rig from research/prior-art-and-methodology.md (§6 option b)
and the §5 Phase-1 decision gate. It resolves the one remaining auto-pass unknown:

    "Does our patchright + real Chrome + persistent-profile stack pass reCAPTCHA
     *Enterprise* (the tier Greenhouse ships, 100% in our survey)?"

reCAPTCHA Enterprise scores are computed server-side and returned ONLY to the site
administrator via the Assessment API -- never to the client. So no public per-visitor
Enterprise detector exists (antcpt measures *standard* v3 only). The only safe way to read
our own Enterprise score is to own the key: we create our OWN Enterprise score-based key in a
GCP project, serve a tiny page with it on localhost, mint a token with our real stealth
BrowserSession, then call the Assessment API with our secret to read the score back.

Flow:
    1. Serve a one-page HTML harness on http://localhost:PORT/ with our Enterprise site key.
    2. Drive it with av3.sources.browser.session.BrowserSession (the EXACT stack the apply
       path uses -- patchright, real Chrome channel, persistent profile).
    3. Page calls grecaptcha.enterprise.execute(siteKey, {action}) -> token at window.__token.
    4. We POST {token, siteKey, expectedAction} to the Assessment API and read
       riskAnalysis.score (0.0 bot .. 1.0 human) + tokenProperties.valid.
    5. Repeat N trials to see score stability (S1 confirmed 0.9 standard-v3 across 2 runs).

NO job application is ever submitted. The only network egress is to Google's reCAPTCHA
endpoints (the JS + the assessment), exactly as a real ATS form would trigger.

------------------------------------------------------------------------------------------
SETUP (one-time, in the user's GCP account) -- see scripts/recaptcha_enterprise_setup.md
------------------------------------------------------------------------------------------
Set these env vars before running (PowerShell: $env:NAME = "value"):

    GCP_PROJECT_ID                  e.g. "auto-applier-captcha-rig"
    RECAPTCHA_ENTERPRISE_SITE_KEY   the *score-based* (no-challenge) key; PUBLIC, goes in HTML
    RECAPTCHA_ENTERPRISE_API_KEY    an API key restricted to the reCAPTCHA Enterprise API
    RECAPTCHA_ACTION                optional, default "apply" (must match between mint+assess)

The Enterprise key MUST list `localhost` as an allowed domain (free; see the setup doc).

Usage:
    python scripts/measure_enterprise_score.py
    python scripts/measure_enterprise_score.py --trials 5 --action submit_application
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402  (same client the av3 sources use)

from av3.config import load_settings  # noqa: E402
from av3.sources.browser.session import BrowserSession  # noqa: E402

_ASSESS_URL = (
    "https://recaptchaenterprise.googleapis.com/v1/projects/{project}/assessments?key={api_key}"
)

# Minimal harness page. {SITE_KEY} and {ACTION} are substituted before serving. The score-based
# Enterprise key renders no visible widget; execute() runs invisibly and resolves with a token.
_PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>av3 enterprise rig</title>
<script src="https://www.google.com/recaptcha/enterprise.js?render={SITE_KEY}"></script>
</head>
<body>
<h1>av3 reCAPTCHA Enterprise measurement rig</h1>
<p id="status">minting token...</p>
<script>
  window.__token = null;
  window.__err = null;
  function mint() {{
    try {{
      grecaptcha.enterprise.ready(function () {{
        grecaptcha.enterprise.execute('{SITE_KEY}', {{action: '{ACTION}'}})
          .then(function (t) {{
            window.__token = t;
            document.getElementById('status').textContent = 'TOKEN_READY len=' + t.length;
            document.title = 'TOKEN_READY';
          }})
          .catch(function (e) {{
            window.__err = String(e);
            document.getElementById('status').textContent = 'ERROR: ' + window.__err;
          }});
      }});
    }} catch (e) {{
      window.__err = String(e);
      document.getElementById('status').textContent = 'EXC: ' + window.__err;
    }}
  }}
  // expose a re-mint hook so one page load can produce several independent tokens
  window.__remint = function () {{ window.__token = null; window.__err = null; mint(); }};
  mint();
</script>
</body></html>
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_server(html: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        def log_message(self, *_args):  # silence per-request logging
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def _assess(project: str, api_key: str, site_key: str, action: str, token: str) -> dict:
    url = _ASSESS_URL.format(project=project, api_key=api_key)
    body = {"event": {"token": token, "siteKey": site_key, "expectedAction": action}}
    resp = httpx.post(url, json=body, timeout=15.0)
    if resp.status_code != 200:
        raise RuntimeError(f"assessment HTTP {resp.status_code}: {resp.text[:400]}")
    return resp.json()


async def _mint_token(page, timeout_s: float = 30.0) -> str:
    """Poll window.__token after a (re)mint. Raises on JS error or timeout."""
    waited = 0.0
    while waited < timeout_s:
        await asyncio.sleep(1.0)
        waited += 1.0
        err = await page.evaluate("() => window.__err")
        if err:
            raise RuntimeError(f"grecaptcha error: {err}")
        token = await page.evaluate("() => window.__token")
        if token:
            return token
    raise TimeoutError(f"no token after {timeout_s:.0f}s (check site key + localhost domain)")


def _verdict(score: float) -> str:
    if score >= 0.7:
        return "STRONG human -> Enterprise auto-pass very likely"
    if score >= 0.5:
        return "PASS-likely -> auto viable at typical 0.5 threshold"
    if score >= 0.3:
        return "BORDERLINE -> site-dependent; treat as assisted"
    return "FAIL-likely -> escalates / blocks -> assisted only"


async def run(trials: int, action: str) -> int:
    project = os.environ.get("GCP_PROJECT_ID", "").strip()
    site_key = os.environ.get("RECAPTCHA_ENTERPRISE_SITE_KEY", "").strip()
    api_key = os.environ.get("RECAPTCHA_ENTERPRISE_API_KEY", "").strip()
    action = os.environ.get("RECAPTCHA_ACTION", action).strip() or action

    missing = [
        n for n, v in (
            ("GCP_PROJECT_ID", project),
            ("RECAPTCHA_ENTERPRISE_SITE_KEY", site_key),
            ("RECAPTCHA_ENTERPRISE_API_KEY", api_key),
        ) if not v
    ]
    if missing:
        print("ERROR: missing env var(s): " + ", ".join(missing))
        print("See scripts/recaptcha_enterprise_setup.md for the one-time GCP setup.")
        return 2

    html = _PAGE_TEMPLATE.format(SITE_KEY=site_key, ACTION=action)
    port = _free_port()
    server = _make_server(html, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://localhost:{port}/"
    print(f"[rig] serving harness at {base}  (action='{action}', trials={trials})")
    print(f"[rig] project={project}  site_key=...{site_key[-6:]}")

    settings = load_settings()
    session = BrowserSession(settings.browser_profile_dir)
    scores: list[float] = []
    try:
        await session.start()
        page = await session.new_page()
        await page.goto(base, wait_until="domcontentloaded")

        for i in range(1, trials + 1):
            if i > 1:
                await page.evaluate("() => window.__remint()")
            print(f"\n[trial {i}/{trials}] minting token...")
            token = await _mint_token(page)
            print(f"[trial {i}] token len={len(token)}; calling Assessment API...")
            result = _assess(project, api_key, site_key, action, token)

            tp = result.get("tokenProperties", {})
            ra = result.get("riskAnalysis", {})
            valid = tp.get("valid")
            if not valid:
                print(f"[trial {i}] token INVALID: {tp.get('invalidReason')}  (full: {json.dumps(tp)})")
                continue
            score = ra.get("score")
            reasons = ra.get("reasons", [])
            got_action = tp.get("action")
            print(f"[trial {i}] score={score}  action='{got_action}'  reasons={reasons or '[]'}")
            if isinstance(score, (int, float)):
                scores.append(float(score))
                print(f"[trial {i}] ==> {_verdict(float(score))}")
    finally:
        await session.stop()
        server.shutdown()

    print("\n" + "=" * 70)
    if scores:
        avg = sum(scores) / len(scores)
        print(f"RESULT: {len(scores)} valid score(s): {scores}")
        print(f"        mean={avg:.2f}  min={min(scores):.2f}  max={max(scores):.2f}")
        print(f"        OVERALL: {_verdict(avg)}")
        print("\nNOTE: this is OUR Enterprise key scoring OUR browser/IP/profile. Greenhouse uses")
        print("its own key + thresholds, but the per-visitor risk score Google's ML assigns is")
        print("driven by the same browser/IP/behavior signals, so this is a faithful proxy.")
        return 0
    print("RESULT: no valid scores obtained -- see token errors above.")
    return 1


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure our reCAPTCHA Enterprise score (safe).")
    ap.add_argument("--trials", type=int, default=3, help="number of tokens to mint+assess")
    ap.add_argument("--action", default="apply", help="reCAPTCHA action name (mint + expected)")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args.trials, args.action)))


if __name__ == "__main__":
    main()
