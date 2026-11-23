// node --test relay/worker.test.mjs   (no Cloudflare needed: KV and the network are stand-ins)
import test from 'node:test';
import assert from 'node:assert/strict';

async function fresh() {
  return (await import(`./worker.js?${Math.random()}`)).default; // new module = empty cache
}
function env() {
  const store = new Map();
  return { RELAY_SECRET: 's3cret-value', RELAY: { get: async (k) => store.get(k) ?? null, put: async (k, v) => { store.set(k, v); } } };
}
const register = (url, secret) => new Request('https://hp.me.workers.dev/__relay/register',
  { method: 'POST', headers: { authorization: `Bearer ${secret}`, 'content-type': 'application/json' }, body: JSON.stringify({ url }) });

test('shows a not-running page before the PC has registered', async () => {
  const w = await fresh();
  const res = await w.fetch(new Request('https://hp.me.workers.dev/'), env());
  assert.equal(res.status, 503);
  assert.match(await res.text(), /not running/);
});

test('register needs the secret and only accepts a quick tunnel address', async () => {
  const w = await fresh(); const e = env();
  assert.equal((await w.fetch(register('https://abc-def.trycloudflare.com', 'wrong'), e)).status, 403);
  assert.equal((await w.fetch(register('https://evil.example.com', 's3cret-value'), e)).status, 400);
  assert.equal((await w.fetch(register('https://abc-def.trycloudflare.com', 's3cret-value'), e)).status, 200);
});

test('forwards requests to the registered tunnel with the visitor and host headers', async () => {
  const w = await fresh(); const e = env();
  await w.fetch(register('https://abc-def.trycloudflare.com', 's3cret-value'), e);
  let seen;
  globalThis.fetch = async (url, init) => { seen = { url: String(url), init }; return new Response('ok', { status: 200, headers: { 'set-cookie': 'hp=1; Secure' } }); };
  const res = await w.fetch(new Request('https://hp.me.workers.dev/api/status?x=1', { headers: { 'cf-connecting-ip': '1.2.3.4' } }), e);
  assert.equal(res.status, 200);
  assert.equal(seen.url, 'https://abc-def.trycloudflare.com/api/status?x=1');
  assert.equal(seen.init.headers.get('x-forwarded-host'), 'hp.me.workers.dev');
  assert.equal(seen.init.headers.get('x-relay-client-ip'), '1.2.3.4');
  assert.equal(seen.init.headers.get('x-relay-key'), 's3cret-value');
  assert.equal(res.headers.get('set-cookie'), 'hp=1; Secure');
});

test('a PC that went away shows the not-running page', async () => {
  const w = await fresh(); const e = env();
  await w.fetch(register('https://abc-def.trycloudflare.com', 's3cret-value'), e);
  globalThis.fetch = async () => new Response('tunnel gone', { status: 530 });
  assert.equal((await w.fetch(new Request('https://hp.me.workers.dev/'), e)).status, 503);
  globalThis.fetch = async () => { throw new TypeError('network'); };
  assert.equal((await w.fetch(new Request('https://hp.me.workers.dev/'), e)).status, 503);
});

test('redirects to the tunnel address are rewritten to stay on the relay', async () => {
  const w = await fresh(); const e = env();
  await w.fetch(register('https://abc-def.trycloudflare.com', 's3cret-value'), e);
  globalThis.fetch = async () => new Response(null, { status: 303, headers: { location: 'https://abc-def.trycloudflare.com/login' } });
  const res = await w.fetch(new Request('https://hp.me.workers.dev/'), e);
  assert.equal(res.headers.get('location'), '/login');
});
