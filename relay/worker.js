/**
 * Auto Applier v3 — telemetry relay (Cloudflare Worker).  Spec §9, Phase 5 (4/M).
 *
 * The thin owner-hosted endpoint between the client drainer (av3 mirror drain)
 * and the shared Turso (libSQL) database. Its whole job:
 *
 *   1. Hold the Turso WRITE TOKEN in env — it never ships in the client app, so
 *      a compromised client has no credential to steal (spec §9).
 *   2. Re-scrub every row as a SECOND line of defence — the client already
 *      scrubbed at enqueue (av3/telemetry/scrub.py), but we never trust the wire.
 *   3. Rate-limit by user_id so one misbehaving client can't flood the DB.
 *   4. Reject malformed rows before they reach Turso.
 *
 * Routes:
 *   GET  /health   → 200 "ok"   (doctor.check_relay_reachable pings this)
 *   POST /ingest   → { category, payload, schema }  →  202 on accept
 *
 * Deploy:  see relay/README.md  (wrangler deploy; set secrets with `wrangler secret put`).
 *
 * NOTE: this is a TEMPLATE. It is owner-hosted infra deployed ONCE, independent
 * of the client installer (spec §11a). It is not exercised by the Python test
 * suite; the client side is tested with a mocked transport (test_mirror_client.py).
 */

const MAX_LEN = 500;
const EMAIL_RE = /\b[\w.+-]+@[\w-]+\.[\w.-]+\b/g;
const PHONE_RE = /\b(?:\+?\d[\d\s().-]{7,}\d)\b/g;
// Windows or POSIX path that can embed a username.
const PATH_RE = /(?:[A-Za-z]:\\|\/)(?:[^\s\\/]+[\\/])+[^\s\\/]+/g;

// Mirror of av3/telemetry/scrub.py allow-lists. Keep in sync if §9 schema changes.
const ERROR_FIELDS = new Set([
  "user_id", "app_version", "stage", "platform",
  "error_type", "scrubbed_error_msg", "ts",
]);
const INFERRED_FIELDS = new Set([
  "user_id", "question_text", "category", "confidence", "outcome", "ts",
]);

function scrubText(s) {
  if (typeof s !== "string" || !s) return s;
  let out = s.replace(EMAIL_RE, "[email]").replace(PHONE_RE, "[phone]").replace(PATH_RE, "[path]");
  if (out.length > MAX_LEN) out = out.slice(0, MAX_LEN) + "…[truncated]";
  return out;
}

function allowlist(obj, allowed) {
  const out = {};
  for (const [k, v] of Object.entries(obj || {})) {
    if (allowed.has(k) && v !== null && v !== undefined) out[k] = v;
  }
  return out;
}

/** Re-scrub a payload by category. Returns null if the row must be dropped. */
function rescrub(category, payload) {
  if (category === "error") {
    const out = allowlist(payload, ERROR_FIELDS);
    if (typeof out.scrubbed_error_msg === "string") {
      out.scrubbed_error_msg = scrubText(out.scrubbed_error_msg);
    }
    if (typeof out.error_type === "string" && out.error_type.length > MAX_LEN) {
      out.error_type = out.error_type.slice(0, MAX_LEN) + "…[truncated]";
    }
    return out;
  }
  if (category === "inferred_answer") {
    // §8d: EEO rows must never land in Turso — defence in depth (client drops too).
    if (payload && payload.category === "eeo") return null;
    const out = allowlist(payload, INFERRED_FIELDS);
    // There is NO `answer` field in the allow-list — the answer value can never
    // reach the DB even if a buggy/hostile client puts one on the wire.
    if (typeof out.question_text === "string") out.question_text = scrubText(out.question_text);
    return out;
  }
  return null; // unknown category → drop
}

// --- Turso (libSQL) HTTP write -------------------------------------------------
async function insertRow(env, category, row) {
  // libSQL HTTP pipeline API. The write token is scoped to this DB and lives only
  // here (Worker secret), never in the client.
  const sql =
    "INSERT INTO mirror_events (received_at, category, user_id, payload_json) VALUES (?, ?, ?, ?)";
  const args = [
    { type: "text", value: new Date().toISOString() },
    { type: "text", value: category },
    { type: "text", value: String(row.user_id ?? "anonymous") },
    { type: "text", value: JSON.stringify(row) },
  ];
  const resp = await fetch(env.TURSO_DATABASE_URL.replace(/\/$/, "") + "/v2/pipeline", {
    method: "POST",
    headers: {
      "Authorization": "Bearer " + env.TURSO_AUTH_TOKEN,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      requests: [
        { type: "execute", stmt: { sql, args } },
        { type: "close" },
      ],
    }),
  });
  if (!resp.ok) throw new Error("turso write failed: " + resp.status);
}

// --- Rate limit (per user_id, fixed window) -----------------------------------
// Uses a KV namespace bound as RATE_LIMIT. If unbound (template not fully
// configured), rate limiting soft-fails OPEN so the relay still works in dev.
async function rateLimited(env, userId) {
  if (!env.RATE_LIMIT) return false;
  const windowMin = Number(env.RATE_LIMIT_PER_MIN || "120");
  const key = `rl:${userId}:${Math.floor(Date.now() / 60000)}`;
  const n = Number((await env.RATE_LIMIT.get(key)) || "0");
  if (n >= windowMin) return true;
  // 90s TTL so the per-minute counter self-expires.
  await env.RATE_LIMIT.put(key, String(n + 1), { expirationTtl: 90 });
  return false;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/health") {
      return new Response("ok", { status: 200 });
    }

    if (request.method === "POST" && url.pathname === "/ingest") {
      let body;
      try {
        body = await request.json();
      } catch {
        return new Response("bad json", { status: 400 });
      }
      const { category, payload } = body || {};
      if (typeof category !== "string" || typeof payload !== "object" || payload === null) {
        return new Response("missing category/payload", { status: 400 });
      }
      const clean = rescrub(category, payload);
      if (clean === null) {
        // Dropped on purpose (EEO / unknown category). 202 so the client marks it
        // delivered and stops retrying — it did its job; we chose not to store it.
        return new Response("dropped", { status: 202 });
      }
      const userId = String(clean.user_id ?? "anonymous");
      if (await rateLimited(env, userId)) {
        return new Response("rate limited", { status: 429 }); // client will retry w/ backoff
      }
      try {
        await insertRow(env, category, clean);
      } catch (e) {
        return new Response("upstream error", { status: 502 }); // client retries
      }
      return new Response("accepted", { status: 202 });
    }

    return new Response("not found", { status: 404 });
  },
};
