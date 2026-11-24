// Writing sheets: the app picks lines to copy by hand; an uploaded sheet page is labelled from the
// known text (matched line by line), so it needs no recognition and very little checking.
import { api, upload } from '../api.js';
import { h, mount } from '../dom.js';
import { hasMath, renderMath } from '../math.js';
import { icon } from '../components/icons.js';
import { callout, emptyState, errorState, loadingState } from '../components/states.js';
import { openChatPack } from './chat-packs.js';
import { trackJob } from '../jobs.js';
import { toast } from '../app.js';

/** @typedef {{id: string, number: number, created_at: string, focus: string, focus_label: string, lines: string[], pages: any[], printed?: boolean}} Sheet */

/** One sheet line, with maths rendered so you can see exactly what to write. @param {string} text */
function sheetLine(text) {
  const el = h('span', { class: 'sh-line__text' });
  if (hasMath(text)) renderMath(el, text); else el.textContent = text;
  return el;
}

/**
 * Upload written sheets in one go: a PDF (choose its pages) or several photos, the first being sheet N,
 * the next N+1, and so on. Each page is filled in from its sheet.
 * @param {() => void} done
 */
function uploadWritten(done) {
  const file = /** @type {HTMLInputElement} */ (h('input', { type: 'file', multiple: true, accept: '.pdf,application/pdf,.jpg,.jpeg,.png,.webp,.heic,.heif,image/*' }));
  const pagesIn = /** @type {HTMLInputElement} */ (h('input', { type: 'text', inputmode: 'numeric', placeholder: 'all, or e.g. 1-7', maxlength: 200 }));
  const first = /** @type {HTMLInputElement} */ (h('input', { type: 'number', inputmode: 'numeric', min: '1', value: '1001' }));
  const kind = /** @type {HTMLSelectElement} */ (h('select', {}, h('option', { value: 'ipad' }, 'iPad (e.g. GoodNotes)'), h('option', { value: 'paper' }, 'Paper photos')));
  const out = h('ul', { class: 'upload-list', role: 'list', 'aria-live': 'polite' });
  const go = h('button', { class: 'btn btn--primary', type: 'button', onClick: start }, icon('upload'), 'Upload and fill in');

  async function start() {
    const files = Array.from(file.files || []);
    const n = Number(first.value);
    if (!files.length) { toast('Choose a PDF or photos first.', 'error'); return; }
    if (!n) { toast('Give the sheet number of the first page (e.g. 1001).', 'error'); return; }
    const spec = pagesIn.value.trim();
    if (spec && !/^\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*,?\s*$/.test(spec)) { toast('Pages: use numbers and ranges, e.g. 1-7', 'error'); return; }
    go.setAttribute('disabled', '');
    let next = n;  // photos: one sheet each, in the order chosen; a PDF: its chosen pages in order
    try {
      for (const f of files) {
        const isPdf = /\.pdf$/i.test(f.name) || f.type === 'application/pdf';
        const state = h('span', { class: 'xs muted', text: 'Uploading…' });
        const detail = h('ul', { class: 'upload-entries' });
        out.prepend(h('li', { class: 'upload-item' }, h('div', { class: 'row row--between' }, h('span', { class: 'upload-item__name', text: f.name }), state), detail));
        const batch = await api('/api/batches', { body: { source_type: kind.value, note: `Writing sheets from ${next}`, pages: isPdf ? (spec || null) : null, sheet_start: next } });
        const res = await upload(`/api/batches/${batch.batch_id}/files`, f, () => {});
        if (res.status === 'rejected') { state.textContent = `Rejected: ${res.detail}`; state.className = 'xs badge badge--error'; continue; }
        state.textContent = 'Filling in from the sheets…';
        const job = await trackJob(res.job_id);
        if (job.status !== 'succeeded') { state.textContent = `Failed: ${job.error || job.status}`; state.className = 'xs badge badge--error'; continue; }
        const sheetsDone = job.result.sheets || [];
        const notNew = job.result.entries.filter((/** @type {any} */ e) => e.status !== 'imported');
        state.textContent = `${sheetsDone.filter((/** @type {any} */ r) => !r.error).length} page(s) filled in`;
        state.className = 'xs badge badge--ok';
        mount(detail, sheetsDone.map((/** @type {any} */ r) => h('li', {},
          r.error ? h('span', { class: 'badge badge--error', text: r.error })
            : h('span', {}, h('strong', { text: `Sheet ${r.sheet_number}` }), h('span', { class: 'xs muted', text: ` — ${summaryText(r)} ` }),
              h('a', { class: 'xs', href: `#/check?source=${r.source_id}`, text: 'Check →' })))),
          notNew.map((/** @type {any} */ e) => h('li', { class: 'xs muted', text: `${e.status}: ${e.detail}` })));
        const numbers = sheetsDone.filter((/** @type {any} */ r) => r.sheet_number).map((/** @type {any} */ r) => r.sheet_number);
        next = numbers.length ? Math.max(...numbers) + 1 : next + (isPdf ? 0 : 1);
      }
      window.dispatchEvent(new CustomEvent('hp:changed'));
      first.value = String(next);
      done();
    } catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
    finally { go.removeAttribute('disabled'); }
  }

  return h('section', { class: 'panel sh-intro stack' },
    h('h2', { text: 'Upload written sheets' }),
    h('p', { class: 'small', text: 'One PDF of your sheet pages (choose which pages), or photos. The first page is the sheet number you give, the next page the next sheet, and so on. Each page is filled in from its sheet; then approve it in Check.' }),
    h('div', { class: 'row sh-upload' },
      h('label', { class: 'field' }, h('span', { text: 'PDF or photos' }), file),
      h('label', { class: 'field' }, h('span', { text: 'PDF pages' }), pagesIn),
      h('label', { class: 'field' }, h('span', { text: 'First page is sheet' }), first),
      h('label', { class: 'field' }, h('span', { text: 'Written on' }), kind),
      go),
    out);
}

/** @param {any} summary */
function summaryText(summary) {
  const parts = [`${summary.matched} of ${summary.sheet_lines} lines matched`];
  if (summary.missing_sheet_lines?.length) parts.push(`not found: line ${summary.missing_sheet_lines.join(', ')}`);
  if (summary.unmatched_lines) parts.push(`${summary.unmatched_lines} extra written line(s) flagged`);
  return parts.join(' · ');
}

/** The Writing sheets tab of Sources. @param {URLSearchParams} params */
export function sheetsTab(params) {
  const body = h('div', { class: 'page-body sh' }, loadingState(2));
  const open = params.get('sheet');
  if (open) showSheet(open); else showList();

  const uploader = uploadWritten(() => showList());  // kept across refreshes so its results stay on screen
  const refill = refillAll();

  async function showList() {
    mount(body, loadingState(2));
    try {
      const data = await api('/api/sheets');
      const made = data.sheets.filter((/** @type {Sheet} */ s) => !s.printed);
      const printed = data.sheets.filter((/** @type {Sheet} */ s) => s.printed);
      const focus = /** @type {HTMLSelectElement} */ (h('select', { 'aria-label': 'What the sheet should practise' },
        Object.entries(data.focuses).map(([v, label]) => h('option', { value: v }, /** @type {string} */ (label)))));
      const create = h('button', { class: 'btn btn--primary', type: 'button', onClick: async () => {
        create.setAttribute('disabled', '');
        try {
          const sheet = await api('/api/sheets', { body: { focus: focus.value } });
          location.hash = `#/sources?tab=sheets&sheet=${sheet.id}`;
        } catch (err) { create.removeAttribute('disabled'); toast(err instanceof Error ? err.message : String(err), 'error'); }
      } }, icon('plus'), 'New sheet');
      mount(body,
        uploader,
        refill,
        h('section', { class: 'panel sh-intro stack' },
          h('h2', { text: 'Write out a sheet' }),
          h('p', { class: 'small', text: 'The app picks real words and sentences that cover what your notes are short of: rare letters and letter pairs, capitals, digits, maths, Greek letters and chemistry, plus words you often write. You copy them by hand; because the text is known, nothing needs reading and checking is quick.' }),
          h('p', { class: 'small muted', text: 'Write at your normal speed, the way you would in notes. Messy is fine and useful. Your notes stay the main source; sheets top up the rare things (keep them to about a third of the data).' }),
          h('div', { class: 'row' }, h('label', { class: 'field field--inline' }, h('span', { text: 'Practise' }), focus), create)),
        made.length
          ? cards(made, true)
          : emptyState({ title: 'No sheets made here yet', icon: 'draw', body: 'Make one above, or use the printed pack below.' }),
        h('section', { class: 'panel sh-intro stack' },
          h('div', { class: 'row row--between' }, h('h2', { text: `Printed pack (${printed.length} sheets)` }),
            h('a', { class: 'btn', href: '/static/sheets/writing-sheets-pack-1.pdf', download: 'handprinter-writing-sheets-pack-1.pdf' }, icon('download'), 'Download PDF')),
          h('p', { class: 'small', text: 'Fixed sheets numbered from 1001, for writing when the app is not running. Print them or open the PDF on the iPad, copy the lines onto your own paper, and upload the photos here later on the sheet with the same number. Their text never changes, so old pages still match.' })),
        cards(printed, false));
    } catch (err) { mount(body, errorState(err, showList)); }
  }

  /** @param {Sheet[]} list @param {boolean} dated @returns {HTMLElement} */
  function cards(list, dated) {
    return h('ul', { class: 'sh-list', role: 'list' }, list.map((s) => h('li', {},
      h('a', { class: 'sh-card panel', href: `#/sources?tab=sheets&sheet=${s.id}` },
        h('strong', { text: `Sheet ${s.number}` }),
        h('span', { class: 'xs muted', text: dated ? `${s.focus_label} · ${new Date(s.created_at).toLocaleDateString()}` : s.focus_label }),
        h('span', { class: 'small sh-card__first', text: s.lines[0] || '' }),
        s.pages.length ? h('span', { class: 'badge badge--ok' }, icon('check'), `${s.pages.length} page${s.pages.length === 1 ? '' : 's'} uploaded`)
          : h('span', { class: 'badge', text: 'Not written yet' })))));
  }

  /** @param {string} id */
  async function showSheet(id) {
    mount(body, loadingState(2));
    try {
      /** @type {Sheet} */
      const sheet = await api(`/api/sheets/${id}`);
      const result = h('div', { class: 'stack' });
      const kind = /** @type {HTMLSelectElement} */ (h('select', { 'aria-label': 'Where you wrote it' },
        h('option', { value: 'ipad' }, 'iPad'), h('option', { value: 'paper' }, 'Paper')));
      const file = /** @type {HTMLInputElement} */ (h('input', { type: 'file', accept: 'image/*,.heic,.pdf', class: 'sr-only', id: 'sh-file',
        onChange: () => send(file.files) }));
      const pickBtn = h('label', { class: 'btn btn--primary', for: 'sh-file' }, icon('upload'), 'Upload the written page');

      /** @param {FileList | null} files */
      async function send(files) {
        if (!files || !files.length) return;
        mount(result, loadingState(1), h('p', { class: 'small muted', text: 'Uploading, finding your lines and matching them to the sheet…' }));
        try {
          const batch = await api('/api/batches', { body: { source_type: kind.value, sheet_id: sheet.id, note: `Writing sheet ${sheet.number}` } });
          for (const f of Array.from(files)) {
            const res = await upload(`/api/batches/${batch.batch_id}/files`, f, () => {});
            if (res.status === 'rejected') throw new Error(res.detail);
            const job = await trackJob(res.job_id);
            if (job.status !== 'succeeded') throw new Error(job.error || 'import failed');
            const entry = job.result.entries[0];
            if (entry && entry.status === 'rejected') throw new Error(entry.detail || 'rejected');
            for (const r of job.result.sheets || []) {
              mount(result, callout(`${summaryText(r)}. The text is filled in: check the boxes and approve in Check.`, r.missing_sheet_lines.length || r.unmatched_lines ? 'review' : 'info'),
                h('div', { class: 'row' }, h('a', { class: 'btn btn--primary', href: `#/check?source=${r.source_id}` }, icon('check'), 'Check this page'),
                  h('a', { class: 'btn', href: `#/sources/${r.source_id}` }, 'See what it found')));
            }
            if (!(job.result.sheets || []).length) mount(result, callout('This photo was already imported before, so it was not matched again. Open it from Pages and use “This page is a writing sheet”.', 'review'));
          }
        } catch (err) { mount(result, callout(err instanceof Error ? err.message : String(err), 'error')); }
        file.value = '';
      }

      mount(body,
        h('nav', { class: 'row small' }, h('a', { href: '#/sources?tab=sheets' }, '← All sheets')),
        h('section', { class: 'panel stack sh-sheet' },
          h('div', { class: 'row row--between' }, h('h2', { text: `Sheet ${sheet.number}` }), h('span', { class: 'xs muted', text: sheet.focus_label })),
          h('p', { class: 'small', text: 'Write each line once, in this order, one written line for each line here. Start each on a new line; if a line does not fit, write it smaller rather than wrapping. Skip one if you like, it will be noticed. Maths: write it the way you normally would (the preview shows what is meant).' }),
          h('ol', { class: 'sh-lines' }, sheet.lines.map((ln) => h('li', { class: 'sh-line' }, sheetLine(ln))))),
        h('section', { class: 'panel stack' },
          h('h3', { text: 'Written it?' }),
          h('div', { class: 'row' }, h('label', { class: 'field field--inline' }, h('span', { text: 'Written on' }), kind), file, pickBtn),
          result,
          sheet.pages.length ? h('div', { class: 'stack' }, h('h4', { text: 'Pages from this sheet' }),
            h('ul', { class: 'plain-list' }, sheet.pages.map((p) => h('li', { class: 'row row--between' },
              h('span', { class: 'small' }, h('span', { text: p.original_filename }), h('span', { class: 'xs muted', text: ` · ${summaryText(p.summary)}` })),
              h('a', { class: 'btn btn--sm', href: `#/check?source=${p.source_id}` }, 'Check'))))) : null));
    } catch (err) { mount(body, errorState(err, () => showSheet(id))); }
  }

  return body;
}

/** Redo every uploaded sheet page with the current word cutting (after an update). */
function refillAll() {
  const status = h('p', { class: 'small', 'aria-live': 'polite' });
  const btn = h('button', { class: 'btn', type: 'button', onClick: async () => {
    btn.setAttribute('disabled', '');
    status.textContent = 'Working…';
    try {
      const { job_id } = await api('/api/sheets/refill', { body: {} });
      const job = await trackJob(job_id);
      if (job.status !== 'succeeded') throw new Error(job.error || 'failed');
      const r = job.result;
      status.textContent = `Done: ${r.pages} page(s) re-cut, ${r.lines_matched} line(s) filled in from their sheets${r.failed.length ? `; ${r.failed.length} page(s) failed` : ''}. Check them in Check.`;
    } catch (err) { status.textContent = ''; toast(err instanceof Error ? err.message : String(err), 'error'); }
    btn.removeAttribute('disabled');
  } }, icon('refresh'), 'Re-cut all sheet pages');
  return h('section', { class: 'panel stack' },
    h('div', { class: 'row row--between' }, h('h2', { text: 'Already uploaded sheets' }), btn),
    h('p', { class: 'small muted', text: 'After an update that improves word cutting, this fills every uploaded sheet page in again. Boxes and text you typed, moved or approved stay as they are.' }),
    status,
    haikuCuts(null));
}

/**
 * Ask Haiku where the words separate: the app numbers every gap in a line's ink, Haiku picks the gaps
 * between words, the app cuts there. For every page (`sourceIds` null) or the given pages.
 * @param {string[] | null} sourceIds
 */
export function haikuCuts(sourceIds) {
  const scope = /** @type {HTMLSelectElement} */ (h('select', { 'aria-label': 'Which lines' },
    h('option', { value: 'amber' }, 'Amber lines only'), h('option', { value: 'all' }, 'All unapproved lines')));
  const status = h('p', { class: 'small', 'aria-live': 'polite' });
  const btn = h('button', { class: 'btn btn--primary', type: 'button', onClick: async () => {
    btn.setAttribute('disabled', '');
    status.textContent = 'Haiku is looking at the lines… progress shows in Background jobs (a few minutes for a hundred lines; you can keep working). It uses your Claude plan: start with amber lines if your usage is tight.';
    try {
      const { job_id } = await api('/api/word-cuts', { body: { scope: scope.value, source_ids: sourceIds } });
      const job = await trackJob(job_id);
      if (job.status !== 'succeeded') throw new Error(job.error || 'failed');
      const r = job.result;
      status.textContent = r.waiting
        ? `${r.lines} line(s) are ready for Haiku. ${r.note}.`
        : r.lines === 0 ? `Nothing new to send: ${r.note}.${r.lines_cut ? ` ${r.lines_cut} line(s) from the earlier round were cut.` : ''}`
          : `Done: ${r.lines_cut} line(s) cut where Haiku chose${r.unsure ? `; ${r.unsure} it was unsure of stay amber` : ''}${r.failed.length ? `; ${r.failed.length} could not be used` : ''}.${r.note ? ` Note: ${r.note}.` : ''} Check them in Check.`;
    } catch (err) { status.textContent = ''; toast(err instanceof Error ? err.message : String(err), 'error'); }
    btn.removeAttribute('disabled');
  } }, icon('review'), 'Ask Haiku to place word boxes');
  const manual = h('button', { class: 'btn', type: 'button' }, 'Build word-box chat pack');
  manual.onclick = async () => {
    manual.disabled = true;
    status.textContent = 'Preparing numbered line images…';
    try {
      const { job_id } = await api('/api/chat-packs/word-cuts', { body: { scope: scope.value, source_ids: sourceIds } });
      const job = await trackJob(job_id);
      if (job.status !== 'succeeded') throw new Error(job.error || job.status);
      if (job.result.items) {
        status.replaceChildren(h('a', { href: '#/sources?tab=recognition' }, 'Reopen saved packs in Recognition packages'));
        openChatPack(job.result.package_id);
      } else status.textContent = 'No eligible lines for this selection.';
    } catch (err) { status.textContent = err instanceof Error ? err.message : String(err); }
    finally { manual.disabled = false; }
  };
  return h('div', { class: 'stack' },
    h('div', { class: 'row' }, scope, manual, btn),
    h('p', { class: 'small muted', text: 'Choose a chat pack for ChatGPT, Gemini, Claude or another vision chat, or ask Haiku through Claude Code. Each image shows numbered gaps between letters. Imported word boxes stay open for review.' }),
    status);
}

/** On a page's own view: mark an already-uploaded page as a written-out sheet. @param {string} sourceId @param {() => void} done */
export function sheetPicker(sourceId, done) {
  const wrap = h('div', { class: 'row sh-picker' });
  api('/api/sheets').then((data) => {
    if (!data.sheets.length) return;
    const sel = /** @type {HTMLSelectElement} */ (h('select', { 'aria-label': 'Which writing sheet this page is' },
      h('option', { value: '' }, 'This page is a writing sheet…'),
      h('optgroup', { label: 'Made in the app' }, data.sheets.filter((/** @type {Sheet} */ s) => !s.printed).map((/** @type {Sheet} */ s) => h('option', { value: s.id }, `Sheet ${s.number} (${s.focus_label})`))),
      h('optgroup', { label: 'Printed pack' }, data.sheets.filter((/** @type {Sheet} */ s) => s.printed).map((/** @type {Sheet} */ s) => h('option', { value: s.id }, `Sheet ${s.number} (${s.focus_label})`)))));
    const go = h('button', { class: 'btn btn--sm', type: 'button', onClick: async () => {
      if (!sel.value) return;
      try {
        const { job_id } = await api(`/api/sources/${sourceId}/sheet`, { body: { sheet_id: sel.value } });
        const job = await trackJob(job_id);
        if (job.status !== 'succeeded') throw new Error(job.error || 'failed');
        toast(`${summaryText(job.result)}. Text filled in from the sheet.`);
        done();
      } catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
    } }, 'Fill in from sheet');
    mount(wrap, sel, go);
  }).catch(() => {});
  return wrap;
}
