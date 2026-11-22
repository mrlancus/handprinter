// Browser-side budget checks (headless Chromium in whatever machine runs this).
// Usage: node scripts/perf_ui.js http://127.0.0.1:8792
const { chromium } = require('playwright');
(async () => {
  const base = process.argv[2];
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1440, height: 900 } });
  await p.goto(`${base}/#/review?status=unprocessed`); await p.waitForSelector('.rv-item.is-selected'); // cold-ish load, primes cache
  const t0 = Date.now();
  await p.goto(`${base}/#/review?status=unprocessed`, { waitUntil: 'commit' });
  await p.waitForSelector('.rv-item.is-selected');
  await p.waitForSelector('.ws__overlay rect');
  const warm = Date.now() - t0;
  // review action acknowledged: press A (approve) and time until the inspector shows Approved (optimistic update)
  await p.click('.ws'); await p.keyboard.press('Escape');
  await p.evaluate(() => (document.activeElement instanceof HTMLElement) && document.activeElement.blur());
  await p.fill('#rv-text', 'perf'); await p.press('#rv-text', 'Enter'); await p.waitForTimeout(300);
  await p.evaluate(() => (document.activeElement instanceof HTMLElement) && document.activeElement.blur());
  const ack = await p.evaluate(() => new Promise((resolve) => {
    const start = performance.now();
    const obs = new MutationObserver(() => {
      if (document.querySelector('.rv-insp .badge--ok')) { obs.disconnect(); resolve(performance.now() - start); }
    });
    obs.observe(document.body, { subtree: true, childList: true });
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
  }));
  // pan/zoom frame rate: drive wheel zoom + drag for ~2 s and count animation frames
  const box = await p.$eval('.ws', (e) => { const r = e.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 }; });
  await p.evaluate(() => { window.__frames = 0; const tick = () => { window.__frames++; window.__raf = requestAnimationFrame(tick); }; tick(); });
  const tStart = Date.now();
  await p.mouse.move(box.x, box.y);
  for (let i = 0; i < 60; i++) { await p.mouse.wheel(0, i % 20 < 10 ? -40 : 40); }
  await p.mouse.down();
  for (let i = 0; i < 60; i++) { await p.mouse.move(box.x + Math.sin(i / 5) * 200, box.y + Math.cos(i / 5) * 120); }
  await p.mouse.up();
  const secs = (Date.now() - tStart) / 1000;
  const frames = await p.evaluate(() => { cancelAnimationFrame(window.__raf); return window.__frames; });
  console.log(JSON.stringify({ warm_review_usable_ms: warm, review_action_ack_ms: Math.round(ack * 10) / 10, panzoom_fps: Math.round(frames / secs) }, null, 2));
  await b.close();
})();
