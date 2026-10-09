// One more place that checks the website.
//
// The status page's own monitor sits on one connection. From there it cannot
// tell "yourbot.gg is down" from "my route to yourbot.gg is down". This
// Worker loads the same address once a minute from Cloudflare's network and
// sends what it saw to the status service, which calls the website down only
// when most of the places that looked could not reach it.
//
// It never decides anything itself and holds no state: one check, one signed
// report (POST /ingest/vantage on the status service).
//
// Settings (wrangler.toml [vars], and one secret):
//   TARGET_URL             what to load, e.g. https://yourbot.gg/readiness
//   STATUS_URL             the status service, e.g. https://status.yourbot.work
//   VANTAGE_NAME           short id for this place: a-z, 0-9, dashes (e.g. cloudflare)
//   VANTAGE_LABEL          how the page names it (e.g. Cloudflare)
//   INGEST_VANTAGE_SECRET  secret, the same value as on the status service

const ATTEMPTS = 2;            // a failed check is retried once before it is reported
const RETRY_DELAY_MS = 2000;
const CHECK_TIMEOUT_MS = 8000;
const REPORT_TIMEOUT_MS = 8000;
const USER_AGENT = "yourbot-status-vantage/1.0 (+https://status.yourbot.work)";

async function checkOnce(url) {
  const started = Date.now();
  try {
    const response = await fetch(url, {
      method: "GET",
      headers: { "User-Agent": USER_AGENT, "Cache-Control": "no-store" },
      redirect: "follow",
      cf: { cacheTtl: 0, cacheEverything: false },
      signal: AbortSignal.timeout(CHECK_TIMEOUT_MS),
    });
    await response.arrayBuffer();           // the whole answer has to arrive, not only the headers
    const elapsed = Date.now() - started;
    if (response.status === 200) {
      return { status: "operational", http_status: 200, response_ms: elapsed, error: null };
    }
    return { status: "down", http_status: response.status, response_ms: elapsed, error: `HTTP ${response.status}` };
  } catch (err) {
    const name = err && err.name === "TimeoutError" ? "timeout" : String((err && err.message) || err).slice(0, 160);
    return { status: "down", http_status: null, response_ms: Date.now() - started, error: name };
  }
}

export async function check(url) {
  let result = await checkOnce(url);
  for (let attempt = 1; attempt < ATTEMPTS && result.status === "down"; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, RETRY_DELAY_MS));
    result = await checkOnce(url);
  }
  return result;
}

function hex(buffer) {
  return [...new Uint8Array(buffer)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

// The scheme the status service verifies: HMAC-SHA256 over "<unix seconds>." + body.
export async function sign(secret, body, timestamp) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return hex(await crypto.subtle.sign("HMAC", key, encoder.encode(`${timestamp}.${body}`)));
}

export async function report(env, result) {
  const body = JSON.stringify({
    vantage: env.VANTAGE_NAME,
    label: env.VANTAGE_LABEL || env.VANTAGE_NAME,
    status: result.status,
    http_status: result.http_status,
    response_ms: result.response_ms,
    error: result.error,
  });
  const timestamp = String(Math.floor(Date.now() / 1000));
  const response = await fetch(`${String(env.STATUS_URL).replace(/\/+$/, "")}/ingest/vantage`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "User-Agent": USER_AGENT,
      "X-Status-Timestamp": timestamp,
      "X-Status-Signature": await sign(env.INGEST_VANTAGE_SECRET, body, timestamp),
    },
    body,
    signal: AbortSignal.timeout(REPORT_TIMEOUT_MS),
  });
  return response.status;
}

export async function run(env) {
  for (const name of ["TARGET_URL", "STATUS_URL", "VANTAGE_NAME", "INGEST_VANTAGE_SECRET"]) {
    if (!env[name]) throw new Error(`${name} is not set`);
  }
  const result = await check(env.TARGET_URL);
  const accepted = await report(env, result);
  // One line per run in the Worker's log. No secret, no body.
  console.log(JSON.stringify({ vantage: env.VANTAGE_NAME, saw: result.status, http: result.http_status, ms: result.response_ms, reported: accepted }));
  return { result, accepted };
}

export default {
  // Cron trigger: once a minute (wrangler.toml).
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env).catch((err) => console.log(JSON.stringify({ vantage: env.VANTAGE_NAME, failed: String(err).slice(0, 200) }))));
  },
  // Nothing to browse here, and nothing a visitor can trigger.
  async fetch() {
    return new Response("yourbot status vantage point\n", { status: 200, headers: { "Content-Type": "text/plain" } });
  },
};
