// Playwright coverage: every screen in populated / empty / loading / error states at several viewports,
// plus keyboard checks. Uses synthetic, non-personal fixture data only.
// Usage: node scripts/screenshots.js <populatedBaseUrl> <emptyBaseUrl> <outDir>
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

const [POP, EMPTY, OUT] = process.argv.slice(2);
const VIEWPORTS = { '1440': [1440, 900], '1280': [1280, 800], '834': [834, 1194], '390': [390, 844] };

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const report = { shots: [], consoleErrors: [], keyboard: {} };
  const src = await (await fetch(`${POP}/api/sources?limit=60`)).json();
  const firstPage = src.items[src.items.length - 1].id;
  const screens = {
    sources: '#/sources', 'sources-page': `#/sources/${firstPage}`, 'sources-recognition': '#/sources?tab=recognition',
    review: '#/review?status=', dataset: '#/dataset', experiments: '#/experiments', studio: '#/studio', settings: '#/settings',
  };

  async function shot(name, base, hash, vp, setup) {
    const [w, h] = VIEWPORTS[vp];
    const page = await browser.newPage({ viewport: { width: w, height: h } });
    page.on('pageerror', (e) => report.consoleErrors.push(`${name}@${vp}: ${e}`));
    page.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) report.consoleErrors.push(`${name}@${vp}: ${m.text()}`); });
    if (setup) await setup(page);
    await page.goto(`${base}/${hash}`);
    await page.waitForTimeout(setup ? 900 : 1300);
    const file = `${name}-${vp}.jpg`;
    await page.screenshot({ path: path.join(OUT, file), type: 'jpeg', quality: 78 });
    report.shots.push(file);
    await page.close();
  }

  for (const [name, hash] of Object.entries(screens)) {
    for (const vp of Object.keys(VIEWPORTS)) await shot(`${name}-populated`, POP, hash, vp);
  }
  for (const name of ['sources', 'review', 'dataset', 'studio']) {
    for (const vp of ['1440', '390']) await shot(`${name}-empty`, EMPTY, screens[name], vp);
  }
  const slow = async (page) => page.route('**/api/**', async (route) => { await new Promise((r) => setTimeout(r, 8000)); route.continue().catch(() => {}); });
  const fail = async (page) => page.route('**/api/**', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'Simulated server error for the screenshot harness' }) }));
  const offline = async (page) => page.route('**/api/**', (route) => route.abort('connectionrefused'));
  for (const name of ['sources', 'review', 'dataset']) await shot(`${name}-loading`, POP, screens[name], '1440', slow);
  for (const name of ['sources', 'dataset', 'experiments']) await shot(`${name}-error`, POP, screens[name], '1440', fail);
  await shot('review-offline', POP, screens.review, '1440', offline);

  // ---------- keyboard checks ----------
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(`${POP}/#/review?status=`);
  await page.waitForTimeout(1300);
  const selectedText = async () => page.$eval('.rv-item.is-selected .rv-item__text', (e) => e.textContent);
  const first = await selectedText();
  await page.keyboard.press('j'); await page.waitForTimeout(400);
  const afterJ = await selectedText();
  report.keyboard.j_moves_to_next = first !== afterJ;
  await page.keyboard.press('k'); await page.waitForTimeout(400);
  report.keyboard.k_moves_back = (await selectedText()) === first;
  await page.keyboard.press('e');
  report.keyboard.e_focuses_transcription = await page.evaluate(() => document.activeElement?.id === 'rv-text');
  const before = await selectedText();
  await page.keyboard.type('jjax');
  await page.waitForTimeout(300);
  report.keyboard.shortcuts_do_not_fire_while_typing = (await selectedText()) === before && (await page.$eval('#rv-text', (e) => e.value)).includes('jjax');
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');
  await page.evaluate(() => (document.activeElement instanceof HTMLElement) && document.activeElement.blur());
  await page.keyboard.press('?'); await page.waitForTimeout(200);
  report.keyboard.help_dialog_opens = await page.$('dialog[open]') !== null;
  await page.keyboard.press('Escape');
  // Tab order reaches the rail and shows a visible focus ring
  await page.goto(`${POP}/#/dataset`); await page.waitForTimeout(900);
  await page.keyboard.press('Tab'); await page.keyboard.press('Tab');
  report.keyboard.focus_ring_visible = await page.evaluate(() => {
    const el = document.activeElement; if (!el) return false;
    const s = getComputedStyle(el); return s.boxShadow !== 'none' || s.outlineStyle !== 'none';
  });
  await page.screenshot({ path: path.join(OUT, 'keyboard-focus-1440.jpg'), type: 'jpeg', quality: 78 });
  report.shots.push('keyboard-focus-1440.jpg');
  // Reduced motion honoured
  const rm = await browser.newPage({ reducedMotion: 'reduce' });
  await rm.goto(`${POP}/#/dataset`);
  report.keyboard.reduced_motion_duration = await rm.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--dur-fast').trim());
  await browser.close();
  fs.writeFileSync(path.join(OUT, 'coverage-report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ shots: report.shots.length, consoleErrors: report.consoleErrors, keyboard: report.keyboard }, null, 2));
})();
