# Vendored front-end assets

Third-party files served from our own static dir instead of a CDN.

## Why vendored

Alpine.js drives every interactive element on every page (the topbar, the pipeline view, the
review queue, the In Progress page, the footer's report button). Loading it from
`cdn.jsdelivr.net` meant a blocked or unreachable CDN didn't *degrade* the dashboard — it
killed it outright, with no error a non-technical user could act on.

That is a real failure mode for this audience: a tester on a locked-down work laptop, behind an
ad-blocker, or simply offline. It also contradicted the local-first promise the footer makes on
every page.

`build.py` already bundles `auto_applier/web/static`, so vendoring means the packaged app
carries its own UI runtime with no network dependency at page load.

## Contents

| File | Version | License | Source |
|---|---|---|---|
| `alpine-3.14.1.min.js` | 3.14.1 | MIT | `https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js` |

Pinned to the version the CDN tag previously requested, so this was a like-for-like swap.

## Upgrading

Download the new `cdn.min.js`, save it here as `alpine-<version>.min.js`, update the `<script>`
tag in `templates/base.html`, and update the table above. `tests/test_web_assets.py` fails if a
template starts pulling a script or stylesheet from an external origin again.
