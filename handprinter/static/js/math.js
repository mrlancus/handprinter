// LaTeX maths rendering with a locally vendored MathJax (static/vendor/mathjax, Apache-2.0).
// Loaded lazily on first use; nothing is fetched from the internet.

const MATH_SPAN = /\$[^$]+\$/;

/** True when the text contains `$...$` maths. @param {string | null | undefined} text */
export function hasMath(text) {
  return !!text && MATH_SPAN.test(text);
}

/** @type {Promise<any> | null} */
let loading = null;

/** @returns {Promise<any>} */
export function loadMathJax() {
  const w = /** @type {any} */ (window);
  if (w.MathJax?.tex2svgPromise) return Promise.resolve(w.MathJax);
  if (loading) return loading;
  loading = new Promise((resolve, reject) => {
    w.MathJax = {
      tex: { inlineMath: [['$', '$']], displayMath: [] },
      svg: { fontCache: 'local' },
      options: { enableMenu: false, enableAssistiveMml: false },
      startup: { typeset: false, ready: () => { w.MathJax.startup.defaultReady(); w.MathJax.startup.promise.then(() => resolve(w.MathJax)); } },
    };
    const s = document.createElement('script');
    s.src = '/static/vendor/mathjax/tex-svg-full.js';
    s.async = true;
    s.onerror = () => { loading = null; reject(new Error('MathJax could not be loaded')); };
    document.head.appendChild(s);
  });
  return loading;
}

/**
 * Split text into plain and maths parts. With `whole`, text without `$` is treated as one LaTeX expression.
 * @param {string} text @param {boolean} whole
 * @returns {{math: boolean, src: string}[]}
 */
export function segments(text, whole) {
  if (whole && !text.includes('$')) return [{ math: true, src: text }];
  /** @type {{math: boolean, src: string}[]} */
  const out = [];
  let pos = 0;
  for (const m of text.matchAll(/\$([^$]+)\$/g)) {
    if (m.index > pos) out.push({ math: false, src: text.slice(pos, m.index) });
    out.push({ math: true, src: m[1] });
    pos = m.index + m[0].length;
  }
  if (pos < text.length) out.push({ math: false, src: text.slice(pos) });
  return out;
}

/**
 * Render text with LaTeX maths into `el`. Errors are shown in place, never thrown.
 * @param {HTMLElement} el @param {string} text @param {{whole?: boolean}} [opts]
 */
export async function renderMath(el, text, opts = {}) {
  const token = Symbol('render');
  /** @type {any} */ (el)._mathToken = token;
  if (!text.trim()) { el.replaceChildren(); return; }
  let mj;
  try { mj = await loadMathJax(); } catch (err) {
    el.textContent = err instanceof Error ? err.message : String(err);
    return;
  }
  /** @type {Node[]} */
  const nodes = [];
  /** @type {string[]} */
  const errors = [];
  for (const seg of segments(text, !!opts.whole)) {
    if (!seg.math) { nodes.push(document.createTextNode(seg.src)); continue; }
    try {
      const out = await mj.tex2svgPromise(seg.src, { display: false });
      const err = out.querySelector('[data-mjx-error]');
      if (err) errors.push(err.getAttribute('data-mjx-error') || 'LaTeX error');
      const svgEl = out.querySelector('svg');
      if (svgEl) { svgEl.classList.add('math-svg'); svgEl.setAttribute('role', 'img'); svgEl.setAttribute('aria-label', seg.src); nodes.push(svgEl); }
    } catch (e) { errors.push(e instanceof Error ? e.message : String(e)); }
  }
  if (/** @type {any} */ (el)._mathToken !== token) return; // a newer render won
  el.replaceChildren(...nodes);
  if (errors.length) {
    const msg = document.createElement('span');
    msg.className = 'math-error';
    msg.textContent = `LaTeX: ${errors.join('; ')}`;
    el.appendChild(msg);
  }
}

/**
 * Typeset LaTeX into a transparent PNG (black ink) for Studio. This is typeset maths, not your handwriting.
 * @param {string} latex @param {number} [emPx] pixels per em
 * @returns {Promise<Blob>}
 */
export async function latexToPng(latex, emPx = 120) {
  const mj = await loadMathJax();
  const out = await mj.tex2svgPromise(latex, { display: true });
  if (out.querySelector('[data-mjx-error]')) throw new Error(`LaTeX: ${out.querySelector('[data-mjx-error]').getAttribute('data-mjx-error')}`);
  const svgEl = /** @type {SVGSVGElement} */ (out.querySelector('svg'));
  const vb = (svgEl.getAttribute('viewBox') || '0 0 1000 1000').split(/\s+/).map(Number);
  const k = emPx / 1000; // MathJax SVG units are 1/1000 em
  const w = Math.max(1, Math.ceil(vb[2] * k)), hgt = Math.max(1, Math.ceil(vb[3] * k));
  svgEl.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
  svgEl.setAttribute('width', String(w));
  svgEl.setAttribute('height', String(hgt));
  svgEl.setAttribute('color', '#000');
  svgEl.removeAttribute('style');
  const blobUrl = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(svgEl)], { type: 'image/svg+xml' }));
  try {
    const img = new Image();
    img.src = blobUrl;
    await img.decode();
    const pad = Math.round(emPx * 0.15);
    const canvas = document.createElement('canvas');
    canvas.width = w + 2 * pad; canvas.height = hgt + 2 * pad;
    const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext('2d'));
    ctx.drawImage(img, pad, pad, w, hgt);
    return await new Promise((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('could not render PNG'))), 'image/png'));
  } finally { URL.revokeObjectURL(blobUrl); }
}
