# Auto Applier v3 — telemetry relay (owner-hosted)

The thin serverless endpoint that sits between every user's `av3 mirror drain`
and one shared **Turso** (libSQL) database. It exists so the **Turso write token
never ships in the client app** (spec §9): a compromised client has no
credential to steal, and the owner can drop an abusive caller at the relay.

This is **owner-hosted infra, deployed once** — independent of the client
installer (spec §11a). Users never touch it; they just point
`av3 telemetry on --relay-url https://<your-relay>` at it.

## What it does

| Route | Behaviour |
|---|---|
| `GET /health` | Returns `200 ok`. `av3 doctor`'s `relay` check pings this when telemetry is on. |
| `POST /ingest` | Body `{category, payload, schema}`. **Re-scrubs** (2nd line of defence), **rate-limits** by `user_id`, **rejects** malformed rows, then inserts into Turso. `202` on accept; `202 dropped` for EEO/unknown rows; `429` when rate-limited; `400` malformed; `502` on a Turso error. The client retries `429`/`5xx` via its backoff ladder. |

The re-scrub mirrors `av3/telemetry/scrub.py`'s allow-lists exactly — including
the load-bearing invariant that **there is no `answer` field**, so an
inferred-answer value can never reach the DB even from a buggy/hostile client,
and **EEO rows are dropped** (spec §8d). Keep `worker.js`'s `ERROR_FIELDS` /
`INFERRED_FIELDS` in sync if the §9 payload schema ever changes.

## One-time setup

Prereqs: a [Cloudflare account](https://workers.cloudflare.com/) + `wrangler`
(`npm i -g wrangler`), and a [Turso](https://turso.tech) database.

```bash
# 1. Create the Turso DB + a WRITE-scoped token (keep this token secret).
turso db create auto-applier-telemetry
turso db tokens create auto-applier-telemetry            # → TURSO_AUTH_TOKEN
turso db show auto-applier-telemetry --url               # → TURSO_DATABASE_URL (https form)

# 2. Create the table the relay writes to.
turso db shell auto-applier-telemetry < schema.sql

# 3. (recommended) Create the rate-limit KV namespace, paste its id into
#    wrangler.toml's [[kv_namespaces]] block.
wrangler kv namespace create RATE_LIMIT

# 4. Set the secrets (never committed) and deploy.
wrangler secret put TURSO_DATABASE_URL
wrangler secret put TURSO_AUTH_TOKEN
wrangler deploy
```

`wrangler deploy` prints the relay URL — that's what users pass to
`av3 telemetry on --relay-url`.

## Triage (the "remote debugging without log files" goal, spec §9)

Query everyone's scrubbed failures centrally, grouped by the `user_id`
pseudonym (you keep the small `hash → person` mapping locally):

```bash
turso db shell auto-applier-telemetry \
  "SELECT user_id, json_extract(payload_json,'$.error_type') AS err, COUNT(*) n
   FROM mirror_events WHERE category='error'
   GROUP BY user_id, err ORDER BY n DESC LIMIT 20;"
```
