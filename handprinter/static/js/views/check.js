// Check: the fast way through recognised pages. One card per line: the line's photo with its word
// boxes outlined, the words read underneath as editable chips, anything doubtful in amber, and one
// tap to approve the whole line. Box surgery (resize, split, merge) stays in Review.
import { api } from '../api.js';
import { h, mount } from '../dom.js';
import { hasMath, renderMath } from '../math.js';
import { icon } from '../components/icons.js';
import { emptyState, errorState, loadingState } from '../components/states.js';
import { toast } from '../app.js';
import { trackJob } from '../jobs.js';

/** @typedef {{id: string, source_id: string, kind: string, cls: string, x: number, y: number, w: number, h: number, status: string,
 *  text: string|null, flags: string[], review_reason: string|null, revision: number}} Region */
/** @typedef {{line: Region, words: Region[]}} LineItem */

/** Plain words for every flag a box can carry. */
const FLAG_TEXT = /** @type {Record<string, string>} */ ({
  uncertain_span: 'unsure about some letters',
  alignment_ambiguous: 'box and word may not match',
  runs_off_edge: 'touches the photo edge: is the whole word there?',
  lines_touching: 'touches the next line',
  may_be_several_lines: 'may hold two lines',
  unreadable: 'could not be read',
  disagreement: 'differs from what you typed before',
  contains_math: 'maths: check the LaTeX',
  other_author_suspected: 'might be someone else’s writing',
  possible_blur: 'blurry', possible_glare: 'glare', clipped: 'cut off', low_contrast: 'faint',
  printed_text_present: 'printed text nearby', ruling_overlap: 'crosses a printed line', partial_word: 'part of a word',
  geometry_stale: 'box moved by a page correction', machine_mark: 'mark found by the recogniser',
});
const flagText = (/** @type {string} */ f) => FLAG_TEXT[f] || (f.startsWith('recogniser_type_') ? `looks like ${f.slice(16).replaceAll('_', ' ')}` : f.replaceAll('_', ' '));

/** @param {Region} w */
function wordIssues(w) {
  if (w.status === 'approved') return [];
  const out = w.flags.map(flagText);
  if (!w.text) out.push('no text yet');
  return out;
}

/** @param {LineItem} item */
function lineIssues(item) {
  const out = item.line.flags.filter((f) => f !== 'alignment_ambiguous' || !item.words.length).map(flagText);
  // A note on a line that needs no decision (e.g. "word boxes re-cut", cleanly) is information, not a warning.
  if (item.line.review_reason && item.line.status === 'needs_review' && !/^omitted:/.test(item.line.review_reason)) out.push(item.line.review_reason);
  return [...new Set(out)];
}

/** @param {LineItem} item */
const isClean = (item) => !lineIssues(item).length && (item.words.length ? item.words.every((w) => w.status === 'excluded' || (w.text && !wordIssues(w).length)) : !!item.line.text);

/** @param {HTMLElement} root @param {URLSearchParams} params */
export async function render(root, params) {
  /** @type {{pages: any[], source: any, lines: LineItem[]}} */
  let data = { pages: [], source: null, lines: [] };
  /** @type {Map<string, {card: HTMLElement, approve: (quiet?: boolean) => Promise<boolean>, focusFirst: () => void}>} */
  const cards = new Map();
  const body = h('div', { class: 'page-body ck' }, loadingState(3));
  const head = h('header', { class: 'page-head' });
  mount(root, head, body);

  async function load(sourceId = params.get('source') || undefined) {
    try {
      data = await api(sourceId ? `/api/check?source_id=${encodeURIComponent(sourceId)}` : '/api/check');
      draw();
    } catch (err) { mount(body, errorState(err, () => load(sourceId))); }
  }

  function go(/** @type {string} */ id) { history.replaceState(null, '', `#/check?source=${id}`); load(id); }

  function draw() {
    cards.clear();
    const pageIdx = data.pages.findIndex((p) => p.id === data.source?.id);
    const prev = pageIdx > 0 ? data.pages[pageIdx - 1] : null;
    const next = pageIdx >= 0 && pageIdx < data.pages.length - 1 ? data.pages[pageIdx + 1] : null;
    const clean = data.lines.filter(isClean);
    mount(head,
      h('div', {},
        h('h1', { text: 'Check' }),
        h('p', { class: 'small muted' }, 'Each line with what was read. ', h('span', { class: 'ck-key ck-key--warn' }), ' Amber = worth a look. Tap a word to fix it, clear a word to leave it out, then approve the line.')),
      data.source ? h('div', { class: 'row ck-head__nav' },
        h('button', { class: 'btn btn--sm', type: 'button', disabled: !prev, 'aria-label': 'Previous page', onClick: () => prev && go(prev.id) }, icon('left')),
        h('label', { class: 'field ck-head__page' }, h('span', { class: 'sr-only', text: 'Page' }),
          h('select', { onChange: (/** @type {Event} */ e) => go(/** @type {HTMLSelectElement} */ (e.target).value) },
            data.pages.map((p) => h('option', { value: p.id, selected: p.id === data.source.id ? true : undefined }, `${p.original_filename} · ${p.open_lines} to check`)))),
        h('button', { class: 'btn btn--sm', type: 'button', disabled: !next, 'aria-label': 'Next page', onClick: () => next && go(next.id) }, icon('right')),
        clean.length ? h('button', { class: 'btn btn--sm', type: 'button', title: 'Approve every line on this page with nothing amber', onClick: () => approveClean(clean) },
          icon('check'), `Approve ${clean.length} line${clean.length === 1 ? '' : 's'} with nothing amber`) : null) : null);

    if (!data.source || !data.lines.length) {
      mount(body, emptyState({ title: 'Nothing to check', icon: 'check', center: true,
        body: data.pages.length ? 'This page is done. Pick another page above.' : 'All lines are approved or left out. Upload more pages, or run recognition on new ones.' }));
      return;
    }
    const unread = data.lines.filter((it) => !it.line.text && !it.words.some((w) => w.text)).length;
    mount(body,
      unread ? h('p', { class: 'callout callout--review small' }, icon('info'), `${unread} line${unread === 1 ? ' has' : 's have'} not been read yet. You can type them, or run recognition on this page first.`) : null,
      h('ol', { class: 'ck-list' }, data.lines.map((item, i) => card(item, i))),
      h('div', { class: 'row ck-foot' }, next ? h('button', { class: 'btn', type: 'button', onClick: () => go(next.id) }, 'Next page', icon('right')) : null));
    const first = data.lines.find((it) => !isClean(it));
    if (first) requestAnimationFrame(() => cards.get(first.line.id)?.focusFirst());
  }

  /** @param {LineItem} item @param {number} index */
  function card(item, index) {
    const { line } = item;
    const words = item.words.filter((w) => w.status !== 'excluded');
    const isMaths = line.cls === 'equation';
    /** @type {Map<string, {input: HTMLInputElement, box: HTMLElement}>} */
    const parts = new Map();
    const note = h('p', { class: 'xs ck-note', role: 'status' });
    const preview = h('div', { class: 'ck-preview' });

    // the line photo with each word box outlined
    const frame = h('div', { class: 'ck-frame' });
    const img = h('img', { class: 'ck-img', src: `/api/regions/${line.id}/crop?rev=${line.revision}`, alt: `Line ${index + 1}`, draggable: 'false' });
    frame.appendChild(img);
    // Fit the whole line to the card where that keeps the writing a readable size; very long lines scroll sideways.
    const scroller = h('div', { class: 'ck-scroll' }, frame);
    const fitImg = () => {
      const iw = /** @type {HTMLImageElement} */ (img).naturalWidth, ih = /** @type {HTMLImageElement} */ (img).naturalHeight;
      if (!iw || !ih) return;
      const avail = scroller.clientWidth || 600;
      const max = matchMedia('(pointer: coarse)').matches ? 120 : 104;
      img.style.height = `${Math.round(Math.max(56, Math.min(max, ih, avail * ih / iw)))}px`;
    };
    img.addEventListener('load', fitImg);
    const pct = (/** @type {number} */ v) => `${Math.max(0, Math.min(100, v)).toFixed(3)}%`;
    for (const w of words) {
      const issues = wordIssues(w);
      const left = (w.x - line.x) / line.w * 100, top = (w.y - line.y) / line.h * 100;
      const box = h('button', {
        class: `ck-box${issues.length ? ' ck-box--warn' : ''}${!w.text ? ' ck-box--empty' : ''}`, type: 'button', tabindex: -1,
        'aria-label': `Word box: ${w.text || 'no text'}`, title: issues.join('; ') || w.text || '',
        style: { left: pct(left), top: pct(top), width: pct(Math.min(100 - Math.max(0, left), w.w / line.w * 100 + Math.min(0, left))), height: pct(Math.min(100 - Math.max(0, top), w.h / line.h * 100 + Math.min(0, top))) },
        onClick: () => parts.get(w.id)?.input.focus(),
      });
      frame.appendChild(box);
      parts.set(w.id, { input: /** @type {any} */ (null), box });
    }

    // the words read, as editable chips in reading order
    const chips = h('div', { class: 'ck-words' });
    for (const w of words) {
      const issues = wordIssues(w);
      const input = /** @type {HTMLInputElement} */ (h('input', {
        class: `ck-word${issues.length ? ' ck-word--warn' : ''}${w.status === 'approved' ? ' ck-word--ok' : ''}`, value: w.text || '', placeholder: '?',
        'aria-label': `Word ${[...parts.keys()].indexOf(w.id) + 1}${issues.length ? ` (${issues.join('; ')})` : ''}`, autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false',
        size: Math.max(2, (w.text || '').length + 1),
      }));
      const part = /** @type {{input: HTMLInputElement, box: HTMLElement}} */ (parts.get(w.id));
      part.input = input;
      input.addEventListener('input', () => {
        input.size = Math.max(2, input.value.length + 1);
        input.classList.toggle('ck-word--edited', input.value !== (w.text || ''));
        input.classList.toggle('ck-word--out', !input.value.trim());
        part.box.classList.toggle('ck-box--out', !input.value.trim());
        drawPreview();
      });
      input.addEventListener('focus', () => {
        part.box.classList.add('is-active');
        part.box.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
        note.textContent = issues.length ? `This word: ${issues.join('; ')}.` : '';
      });
      input.addEventListener('blur', () => { part.box.classList.remove('is-active'); });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); approve(); }
      });
      chips.appendChild(input);
    }
    /** @type {HTMLInputElement | null} */
    let whole = null;
    if (!words.length) {
      whole = /** @type {HTMLInputElement} */ (h('input', { class: `ck-word ck-word--whole${line.text ? '' : ' ck-word--warn'}`, value: line.text || '',
        placeholder: isMaths ? 'LaTeX, e.g. \\frac{1}{2} + x = 3' : 'Type the line exactly as written', 'aria-label': 'Line text', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false' }));
      whole.addEventListener('input', drawPreview);
      whole.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); approve(); } });
      chips.appendChild(whole);
    }

    function currentText() {
      return whole ? whole.value.trim() : [...parts.values()].map((p) => p.input.value.trim()).filter(Boolean).join(' ');
    }
    function drawPreview() {
      const t = currentText();
      if (isMaths || hasMath(t)) renderMath(preview, t, { whole: isMaths });
      else preview.replaceChildren();
    }
    drawPreview();

    const issues = lineIssues(item);
    const approveBtn = h('button', { class: 'btn btn--primary', type: 'button', onClick: () => { approve(); } }, icon('check'), 'Approve line');
    const el = h('li', { class: `ck-card${isClean(item) ? '' : ' ck-card--warn'}`, dataset: { line: line.id } },
      scroller,
      issues.length ? h('p', { class: 'xs ck-issues' }, icon('flag'), issues.join(' · ')) : null,
      chips, preview, note,
      h('div', { class: 'row ck-actions' },
        approveBtn,
        h('button', { class: 'btn btn--quiet', type: 'button', title: 'Not usable: cut off, scribble, someone else’s writing', onClick: () => leaveOut() }, icon('x'), 'Leave line out'),
        h('a', { class: 'btn btn--quiet', href: `#/review?source=${line.source_id}&region=${line.id}`, title: 'Resize, split or merge boxes' }, icon('draw'), 'Fix boxes'),
        h('span', { class: 'xs muted ck-actions__hint', text: 'Enter approves' })));

    /** @param {boolean} [quiet] approving several at once: do not move the focus */
    async function approve(quiet = false) {
      /** @type {Record<string, string>} */
      const texts = {};
      for (const [id, p] of parts) texts[id] = p.input.value;
      const lineText = whole ? whole.value : undefined;
      // A word typed as two ("itwas" -> "it was") means the boxes are wrong too: re-cut them first.
      if (!whole && [...parts.values()].some((p) => /\S\s+\S/.test(p.input.value.trim()))) {
        if (quiet) return false;
        await recut();
        return false;
      }
      approveBtn.setAttribute('disabled', '');
      try {
        const out = await api(`/api/lines/${line.id}/approve`, { body: { words: texts, text: lineText, expected_revision: line.revision } });
        done(el, `Approved: ${out.line.text || ''}`, line);
        if (!quiet) focusNext(line.id);
        return true;
      } catch (err) {
        approveBtn.removeAttribute('disabled');
        toast(err instanceof Error ? err.message : String(err), 'error');
        return false;
      }
    }
    async function recut() {
      approveBtn.setAttribute('disabled', '');
      note.textContent = 'You split a word: re-cutting the boxes to match…';
      try {
        const { job_id } = await api(`/api/lines/${line.id}/recut`, { body: { text: currentText() } });
        const job = await trackJob(job_id);
        if (job.status !== 'succeeded') throw new Error(job.error || 'could not re-cut the boxes');
        const fresh = await api(`/api/check?source_id=${encodeURIComponent(line.source_id)}`);
        const updated = fresh.lines.find((/** @type {LineItem} */ it) => it.line.id === line.id);
        if (!updated) return;
        data.lines = data.lines.map((it) => (it.line.id === line.id ? updated : it));
        const replacement = card(updated, index);
        el.replaceWith(replacement);
        cards.get(line.id)?.focusFirst();
        toast(job.result.clear ? 'Boxes re-cut to your words. Check them, then approve.' : 'Boxes re-cut; the edges are a guess, so check the amber ones.');
      } catch (err) {
        approveBtn.removeAttribute('disabled');
        note.textContent = err instanceof Error ? err.message : String(err);
      }
    }
    async function leaveOut() {
      try {
        await api(`/api/lines/${line.id}/exclude`, { body: {} });
        done(el, 'Left out', line);
        focusNext(line.id);
      } catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
    }
    const focusFirst = () => {
      const target = [...parts.values()].find((p) => p.input.classList.contains('ck-word--warn'))?.input || whole || approveBtn;
      target.focus({ preventScroll: true });
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    };
    cards.set(line.id, { card: el, approve, focusFirst });
    return el;
  }

  /** @param {HTMLElement} el @param {string} message @param {Region} line */
  function done(el, message, line) {
    el.classList.add('ck-card--done');
    mount(el, h('div', { class: 'row ck-done' }, icon('check'), h('span', { class: 'ck-done__text', text: message }),
      h('a', { class: 'btn btn--sm btn--quiet', href: `#/review?source=${line.source_id}&region=${line.id}` }, 'Open in Review')));
    data.lines = data.lines.filter((it) => it.line.id !== line.id);
    const page = data.pages.find((p) => p.id === data.source?.id);
    if (page) page.open_lines = Math.max(0, page.open_lines - 1);
  }

  /** @param {string} afterId */
  function focusNext(afterId) {
    const ids = [...cards.keys()];
    const nextId = ids.slice(ids.indexOf(afterId) + 1).find((id) => data.lines.some((it) => it.line.id === id));
    if (nextId) cards.get(nextId)?.focusFirst();
    else if (!data.lines.length) toast('Page checked. Use Next page to carry on.');
  }

  /** @param {LineItem[]} clean */
  async function approveClean(clean) {
    if (!confirm(`Approve ${clean.length} line${clean.length === 1 ? '' : 's'} on this page that have nothing amber? Only do this if you have looked them over.`)) return;
    let ok = 0;
    for (const item of clean) {
      const c = cards.get(item.line.id);
      if (!c) continue;
      if (await c.approve(true)) ok += 1;
    }
    toast(`${ok} line${ok === 1 ? '' : 's'} approved.`);
    const left = data.lines[0];
    if (left) cards.get(left.line.id)?.focusFirst();
  }

  await load();
}
