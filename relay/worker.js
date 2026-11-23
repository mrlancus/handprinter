// Handprinter relay: a fixed address (https://<name>.<you>.workers.dev) in front of the PC.
//
// Everything still runs on the PC. Each time Handprinter starts online, the PC tells this Worker
// its current Cloudflare quick-tunnel address (POST /__relay/register with the shared secret).
// The Worker forwards every other request there, unchanged, so the password sign-in works as
// before. When the PC is off it shows a short "not running" page instead of a broken link.
//
// Bindings (Cloudflare dashboard -> the Worker -> Settings):
//   RELAY         KV namespace (stores the current tunnel address)
//   RELAY_SECRET  secret text, the same value as HANDPRINTER_RELAY_SECRET in the PC's .env

const KEY = 'origin';
const TUNNEL = /^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/; // only ever forward to a Cloudflare quick tunnel
const CACHE_MS = 30_000;
let cached = { url: /** @type {string | null} */ (null), at: 0 };

/** Constant-time comparison, so the secret cannot be guessed from timing. */
function sameSecret(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || !b) return false;
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}

function page(status, title, body) {
  const html = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title}</title><body style="font:16px system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 16px;color:#1d1c19;background:#f4f1ea">
<h1 style="font:400 1.6rem Georgia,serif">${title}</h1><p>${body}</p></body>`;
  return new Response(html, { status, headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' } });
}

const offline = () => page(503, 'Handprinter is not running',
  'Your PC is off, asleep, or Handprinter is not started. Start it on the PC (double-click Handprinter.bat), wait a few seconds, then reload this page.');

async function register(request, env) {
  if (request.method !== 'POST') return new Response('method not allowed', { status: 405 });
  const auth = request.headers.get('authorization') || '';
  if (!sameSecret(auth.replace(/^Bearer\s+/i, ''), env.RELAY_SECRET)) return new Response('forbidden', { status: 403 });
  let url;
  try { ({ url } = await request.json()); } catch { return new Response('bad request', { status: 400 }); }
  if (typeof url !== 'string' || !TUNNEL.test(url)) return new Response('not a quick tunnel address', { status: 400 });
  await env.RELAY.put(KEY, url);
  cached = { url, at: Date.now() };
  return new Response(JSON.stringify({ ok: true }), { headers: { 'content-type': 'application/json' } });
}

async function origin(env) {
  if (cached.url && Date.now() - cached.at < CACHE_MS) return cached.url;
  const url = await env.RELAY.get(KEY);
  cached = { url, at: Date.now() };
  return url;
}

export default {
  async fetch(request, env) {
    const here = new URL(request.url);
    if (here.pathname === '/__relay/register') return register(request, env);
    const target = await origin(env);
    if (!target) return offline();
    const headers = new Headers(request.headers);
    headers.delete('host');
    headers.set('x-forwarded-host', here.host);
    headers.set('x-forwarded-proto', 'https');
    headers.set('x-relay-client-ip', request.headers.get('cf-connecting-ip') || '');
    headers.set('x-relay-key', env.RELAY_SECRET || '');
    let res;
    try {
      res = await fetch(new URL(here.pathname + here.search, target), {
        method: request.method, headers, redirect: 'manual',
        body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
      });
    } catch {
      cached.at = 0; // the PC may have restarted with a new address: look it up again next time
      return offline();
    }
    if (res.status === 530 || res.status === 502) { cached.at = 0; return offline(); } // Cloudflare: tunnel down
    const out = new Response(res.body, res);
    const loc = out.headers.get('location');
    if (loc && loc.startsWith(target)) out.headers.set('location', loc.slice(target.length) || '/');
    return out;
  },
};
