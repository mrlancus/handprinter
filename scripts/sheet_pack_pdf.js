// Render handprinter/sheet_pack.json to a printable A4 PDF (maths typeset with the vendored MathJax).
//   node scripts/sheet_pack_pdf.js [out.pdf]
// Needs Playwright with Chromium (dev only). Output: handprinter/static/sheets/writing-sheets-pack-1.pdf
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '..');
const pack = JSON.parse(fs.readFileSync(path.join(root, 'handprinter', 'sheet_pack.json'), 'utf8'));
const out = process.argv[2] || path.join(root, 'handprinter', 'static', 'sheets', `writing-sheets-pack-${pack.pack}.pdf`);
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

const intro = `<section class="page intro">
<h1>Handprinter writing sheets</h1>
<p class="sub">Pack ${pack.pack} · sheets ${pack.sheets[0].number}–${pack.sheets[pack.sheets.length - 1].number} · works without the app running</p>
<h2>How to write them</h2>
<ol>
<li>Copy each line onto your own blank or lined paper (or a blank page on the iPad), <b>one sheet line per written line, in the same order</b>. Do not write on this printout: printed text in the photo confuses the line finder.</li>
<li>Write at your normal speed, the way you write notes. Messy is fine and useful; do not neaten it up.</li>
<li>Skipping a line is fine. If you get a line wrong, cross it out and write it again on the next line.</li>
<li>Leave a clear gap between lines so they do not touch. Long lines may run onto a second row; that is fine.</li>
<li>Maths: write it the way you would by hand (fractions stacked, powers raised). Do not write the dollar signs.</li>
<li>Use one page per sheet. Remember which sheet each page is: write the number small in a far corner, away from the text, or keep the photos in order.</li>
</ol>
<h2>Uploading later</h2>
<ol>
<li>Start Handprinter and open <b>Sources → Writing sheets</b>. The printed sheets are listed there by number.</li>
<li>Open the sheet with the same number, and upload the photo on its page. The text is filled in from the sheet, with no recognition needed.</li>
<li>Already uploaded it as an ordinary page? Open it from Pages and choose “This page is a writing sheet”, then pick the number.</li>
<li>Check the result in <b>Check</b>, then approve.</li>
</ol>
<p class="note">Keep sheets to about a third of your data; ordinary notes should stay the main part. These printed sheets never change, so a page written months from now still matches.</p>
</section>`;

const pages = pack.sheets.map((s) => `<section class="page">
<header><span class="num">Sheet ${s.number}</span><span class="focus">${esc(s.focus_label)}</span></header>
<p class="how">Copy onto your own paper, one line each, in this order. Normal speed. Skipping is fine.</p>
<ol class="lines">${s.lines.map((l) => `<li><span class="t">${esc(l)}</span></li>`).join('')}</ol>
<footer>Handprinter pack ${pack.pack} · sheet ${s.number} · upload in Sources → Writing sheets → Sheet ${s.number}</footer>
</section>`).join('');

const html = `<!doctype html><html><head><meta charset="utf-8"><title>Handprinter writing sheets</title>
<style>
@page { size: A4; margin: 16mm 16mm 14mm; }
body { font: 12pt Georgia, 'DejaVu Serif', serif; color: #1d1c19; margin: 0; }
.page { page-break-after: always; break-after: page; }
.page:last-child { page-break-after: auto; }
h1 { font-weight: 400; font-size: 24pt; margin: 0 0 4pt; }
h2 { font: 600 12pt system-ui, sans-serif; margin: 16pt 0 6pt; }
.sub, .how, footer, .note { font: 10pt system-ui, sans-serif; color: #5c5a55; }
.intro li { margin: 0 0 6pt; line-height: 1.4; }
header { display: flex; justify-content: space-between; align-items: baseline; border-bottom: 1.5pt solid #2f5d3a; padding-bottom: 4pt; }
.num { font-size: 22pt; }
.focus { font: 11pt system-ui, sans-serif; color: #2f5d3a; }
.lines { margin: 10pt 0 0; padding-left: 22pt; }
.lines li { font-size: 14pt; line-height: 1.35; padding: 7.5pt 0; border-bottom: 0.5pt solid #ddd8cc; }
.lines li::marker { font: 10pt system-ui, sans-serif; color: #8a877f; }
.t { font-family: 'DejaVu Sans Mono', monospace; font-size: 12.5pt; }
.t.prose { font-family: Georgia, 'DejaVu Serif', serif; font-size: 14pt; }
footer { margin-top: 10pt; }
mjx-container { font-size: 112% !important; }
</style>
<script>
window.MathJax = { tex: { inlineMath: [['$', '$']], displayMath: [] }, svg: { fontCache: 'global' },
  options: { enableMenu: false, enableAssistiveMml: false },
  startup: { ready: () => { MathJax.startup.defaultReady(); MathJax.startup.promise.then(() => { document.body.dataset.ready = '1'; }); } } };
</script>
<script src="file://${path.join(root, 'handprinter', 'static', 'vendor', 'mathjax', 'tex-svg-full.js')}"></script>
</head><body>${intro}${pages}</body></html>`;

(async () => {
  const tmp = path.join(require('os').tmpdir(), `sheet-pack-${process.pid}.html`);
  fs.writeFileSync(tmp, html);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.goto('file://' + tmp);
  await page.waitForSelector('body[data-ready="1"]', { timeout: 60000 });
  // Prose in a serif; code (Python) in monospace so its punctuation is unmistakable.
  await page.evaluate((focus) => {
    document.querySelectorAll('section.page:not(.intro)').forEach((sec, i) => {
      if (focus[i] !== 'python') sec.querySelectorAll('.t').forEach((t) => t.classList.add('prose'));
    });
  }, pack.sheets.map((s) => s.focus));
  const errors = await page.$$eval('mjx-merror, [data-mjx-error]', (els) => els.map((e) => e.textContent));
  if (errors.length) { console.error('MathJax errors:', errors); process.exit(1); }
  fs.mkdirSync(path.dirname(out), { recursive: true });
  await page.pdf({ path: out, format: 'A4', printBackground: true, preferCSSPageSize: true });
  await browser.close();
  fs.unlinkSync(tmp);
  console.log('wrote', out, fs.statSync(out).size, 'bytes');
})();
