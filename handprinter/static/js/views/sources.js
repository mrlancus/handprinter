// Sources: bulk import, page library, per-page preprocessing, and recognition packages.
import { api, upload } from '../api.js';
import { debounce, fmt, h, mount, svg } from '../dom.js';
import { icon } from '../components/icons.js';
import { callout, emptyState, errorState, flagBadge, loadingState, setupState } from '../components/states.js';
import { jobLabel, trackJob } from '../jobs.js';
import { toast } from '../app.js';
import { haikuCuts, sheetPicker, sheetsTab } from './sheets.js';

const PAGE_SIZE = 48;

/** @param {HTMLElement} root @param {URLSearchParams} params @param {string[]} path */
export async function render(root, params, path) {
  if (path[0]) return renderPage(root, path[0]);
  const tab = ['recognition', 'sheets'].includes(params.get('tab') || '') ? /** @type {string} */ (params.get('tab')) : 'pages';
  /** @type {(() => void)[]} */
  const cleanups = [];
  const status = await api('/api/status');
  mount(root,
    h('header', { class: 'page-head' },
      h('div', {}, h('h1', { text: 'Sources' }), h('p', { class: 'muted', text: 'Photographs, scans and iPad exports. Originals are kept byte-for-byte.' }))),
    h('nav', { class: 'tabs', 'aria-label': 'Sources sections' },
      h('a', { href: '#/sources', 'aria-current': tab === 'pages' ? 'page' : undefined }, 'Pages'),
      h('a', { href: '#/sources?tab=recognition', 'aria-current': tab === 'recognition' ? 'page' : undefined }, 'Recognition packages'),
      h('a', { href: '#/sources?tab=sheets', 'aria-current': tab === 'sheets' ? 'page' : undefined }, 'Writing sheets')),
    tab === 'pages' ? pagesTab(status, params, cleanups) : tab === 'sheets' ? sheetsTab(params) : recognitionTab(status));
  return () => cleanups.forEach((fn) => fn());
}

/** @param {any} status @param {URLSearchParams} params @param {(() => void)[]} cleanups */
function pagesTab(status, params, cleanups) {
  const body = h('div', { class: 'page-body' });
  const grid = h('div', { class: 'stack' });
  const selected = new Set();
  const selectionBar = h('div', { class: 'selection-bar', hidden: true });
  let offset = Number(params.get('offset') || 0);
  let filterType = params.get('source_type') || '';
  let filterSession = params.get('session') || '';

  async function load() {
    mount(grid, loadingState(2));
    try {
      const q = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
      if (filterType) q.set('source_type', filterType);
      if (filterSession) q.set('session', filterSession);
      const data = await api(`/api/sources?${q}`);
      if (data.total === 0 && !filterType && !filterSession) {
        mount(grid, emptyState({
          title: 'No pages yet', icon: 'sources',
          body: 'Drop photos, scans or a ZIP of them above. Nothing leaves this computer when you import.',
        }));
        return;
      }
      const filters = h('div', { class: 'row' },
        h('label', { class: 'field field--inline' }, h('span', { text: 'Source' }),
          selectEl(['', 'paper', 'ipad'], filterType, (v) => { filterType = v; offset = 0; load(); }, { '': 'All', paper: 'Paper', ipad: 'iPad' })),
        h('label', { class: 'field field--inline' }, h('span', { text: 'Session' }),
          selectEl(['', ...data.sessions], filterSession, (v) => { filterSession = v; offset = 0; load(); }, { '': 'All sessions' })),
        h('span', { class: 'spacer' }),
        h('span', { class: 'small muted', text: `${fmt(data.total)} page${data.total === 1 ? '' : 's'}` }));
      const cards = data.items.length
        ? h('ul', { class: 'page-grid', role: 'list' }, data.items.map((/** @type {any} */ s) => pageCard(s, selected, updateSelection)))
        : emptyState({ title: 'No pages match these filters', icon: 'sources' });
      const pager = h('div', { class: 'row' },
        h('button', { class: 'btn btn--sm', type: 'button', disabled: offset === 0, onClick: () => { offset = Math.max(0, offset - PAGE_SIZE); load(); } }, icon('left'), 'Previous'),
        h('span', { class: 'small muted', text: `${offset + 1}–${Math.min(offset + PAGE_SIZE, data.total)} of ${fmt(data.total)}` }),
        h('button', { class: 'btn btn--sm', type: 'button', disabled: offset + PAGE_SIZE >= data.total, onClick: () => { offset += PAGE_SIZE; load(); } }, 'Next', icon('right')));
      mount(grid, filters, cards, data.total > PAGE_SIZE ? pager : null);
    } catch (err) {
      mount(grid, errorState(err, load));
    }
  }

  function updateSelection() {
    selectionBar.hidden = selected.size === 0;
    mount(selectionBar,
      h('span', { text: `${selected.size} page${selected.size === 1 ? '' : 's'} selected` }),
      h('span', { class: 'spacer' }),
      h('button', { class: 'btn btn--sm btn--quiet', type: 'button', onClick: () => { selected.clear(); load(); updateSelection(); } }, 'Clear'),
      h('button', { class: 'btn btn--sm btn--danger', type: 'button', onClick: async () => {
        if (!await deletePages([...selected])) return;
        selected.clear(); updateSelection(); load();
      } }, icon('trash'), 'Delete…'),
      h('button', { class: 'btn btn--sm btn--primary', type: 'button', onClick: () => recognitionDialog([...selected], status) }, icon('send'), 'Prepare recognition…'));
  }

  mount(body, importPanel(status, load), selectionBar, grid);
  load();
  window.addEventListener('hp:jobs-finished', load);
  cleanups.push(() => window.removeEventListener('hp:jobs-finished', load));
  return body;
}

/** Delete pages after confirming. @param {string[]} ids @returns {Promise<boolean>} */
async function deletePages(ids) {
  const n = ids.length;
  if (!n || !confirm(`Delete ${n} page${n === 1 ? '' : 's'}? ${n === 1 ? 'It' : 'They'} and every box and label on ${n === 1 ? 'it' : 'them'} leave Sources, Check, Review and future datasets. Snapshots already exported are not changed. This cannot be undone in the app.`)) return false;
  let done = 0;
  for (const id of ids) {
    try { await api(`/api/sources/${id}`, { method: 'DELETE' }); done += 1; }
    catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
  }
  toast(`${done} page${done === 1 ? '' : 's'} deleted.`);
  window.dispatchEvent(new CustomEvent('hp:changed'));
  return done > 0;
}

/** @param {string[]} options @param {string} value @param {(v: string) => void} onChange @param {Record<string,string>} [labels] */
function selectEl(options, value, onChange, labels = {}) {
  return h('select', { onChange: (/** @type {Event} */ e) => onChange(/** @type {HTMLSelectElement} */ (e.target).value) },
    options.map((o) => h('option', { value: o, selected: o === value ? true : undefined }, labels[o] ?? o)));
}

/** @param {any} s @param {Set<string>} selected @param {() => void} onSelect */
function pageCard(s, selected, onSelect) {
  const box = h('input', { type: 'checkbox', 'aria-label': `Select ${s.original_filename}`, checked: selected.has(s.id) ? true : undefined,
    onChange: (/** @type {Event} */ e) => { /** @type {HTMLInputElement} */ (e.target).checked ? selected.add(s.id) : selected.delete(s.id); onSelect(); } });
  const flags = [...s.quality_flags, ...(s.near_duplicates.length ? ['near_duplicate'] : [])];
  return h('li', { class: 'page-card' },
    h('a', { href: `#/sources/${s.id}`, class: 'page-card__img' },
      h('img', { src: `/api/sources/${s.id}/image?variant=thumb&rev=${s.processing_rev}`, alt: `Page ${s.original_filename}`, loading: 'lazy', width: 240, height: 180 })),
    h('div', { class: 'page-card__meta' },
      h('div', { class: 'row row--between' },
        h('a', { href: `#/sources/${s.id}`, class: 'page-card__name', title: s.original_filename }, s.original_filename),
        h('label', { class: 'check check--compact' }, box)),
      h('div', { class: 'row xs muted' },
        h('span', { text: s.source_type === 'ipad' ? 'iPad' : 'Paper' }), s.session ? h('span', { text: `· ${s.session}` }) : null,
        h('span', { text: `· ${s.n_regions} regions` })),
      h('div', { class: 'row' },
        s.n_review ? h('span', { class: 'badge badge--review' }, icon('flag'), `${s.n_review} to review`) : null,
        s.n_approved ? h('span', { class: 'badge badge--ok' }, `${s.n_approved} approved`) : null,
        flags.map(flagBadge))));
}

/** @param {any} status @param {() => void} onImported */
function importPanel(status, onImported) {
  const list = h('ul', { class: 'upload-list', role: 'list', 'aria-live': 'polite' });
  const fileInput = h('input', { type: 'file', multiple: true, class: 'sr-only', id: 'file-input',
    accept: '.jpg,.jpeg,.png,.webp,.zip,.heic,.heif,.pdf,image/*,application/zip,application/pdf',
    onChange: (/** @type {Event} */ e) => start(/** @type {HTMLInputElement} */ (e.target).files) });
  const sourceType = h('div', { class: 'segmented', role: 'radiogroup', 'aria-label': 'Source type' },
    ['paper', 'ipad'].map((v, i) => h('label', {}, h('input', { type: 'radio', name: 'source_type', value: v, checked: i === 0 ? true : undefined }), h('span', { text: v === 'ipad' ? 'iPad export' : 'Paper' }))));
  const session = h('input', { type: 'text', placeholder: 'e.g. 2026-11 notebook', maxlength: 80 });
  const date = h('input', { type: 'date' });
  const era = selectEl(['current', 'older'], 'current', () => {}, { current: 'Current style', older: 'Older style' });
  const pdfPages = /** @type {HTMLInputElement} */ (h('input', { type: 'text', inputmode: 'numeric', placeholder: 'all, or e.g. 1-7, 9', maxlength: 200 }));
  const drop = h('label', { class: 'dropzone', for: 'file-input',
    onDragover: (/** @type {DragEvent} */ e) => { e.preventDefault(); drop.classList.add('is-over'); },
    onDragleave: () => drop.classList.remove('is-over'),
    onDrop: (/** @type {DragEvent} */ e) => { e.preventDefault(); drop.classList.remove('is-over'); start(e.dataTransfer?.files || null); } },
    icon('upload', { class: 'dropzone__icon' }),
    h('strong', { text: 'Drop photos, scans or ZIP archives' }),
    h('span', { class: 'small muted', text: 'JPEG, PNG, WebP, PDF and ZIP. Or click to choose files.' }),
    fileInput);
  const extras = [];
  if (!status.decoders.heic) extras.push('HEIC needs the optional decoder (pip install -e ".[heic]")');
  if (!status.decoders.pdf) extras.push('PDF pages need the optional renderer (pip install -e ".[pdf]")');

  /** @param {FileList | null} files */
  async function start(files) {
    if (!files || !files.length) return;
    const type = /** @type {HTMLInputElement} */ (sourceType.querySelector('input:checked')).value;
    const pagesSpec = pdfPages.value.trim();
    if (pagesSpec && !/^\s*\d+(\s*-\s*\d+)?(\s*,\s*\d+(\s*-\s*\d+)?)*\s*,?\s*$/.test(pagesSpec)) {
      toast('PDF pages: use numbers and ranges, e.g. 1-7, 9', 'error'); return;
    }
    let batch;
    try {
      batch = await api('/api/batches', { body: {
        source_type: type, session: session.value.trim() || null, captured_date: date.value || null,
        style_era: /** @type {HTMLSelectElement} */ (era).value,
        pages: pagesSpec || null,
      } });
    } catch (err) { toast(String(err instanceof Error ? err.message : err), 'error'); return; }
    for (const file of Array.from(files)) {
      const bar = h('span', { style: { width: '0%' } });
      const state = h('span', { class: 'xs muted', text: 'Uploading…' });
      const detail = h('ul', { class: 'upload-entries' });
      list.prepend(h('li', { class: 'upload-item' },
        h('div', { class: 'row row--between' }, h('span', { class: 'upload-item__name', text: file.name }), state),
        h('div', { class: 'progress' }, bar), detail));
      try {
        const res = await upload(`/api/batches/${batch.batch_id}/files`, file, (f) => { bar.style.width = `${Math.round(f * 100)}%`; });
        if (res.status === 'rejected') { state.textContent = `Rejected: ${res.detail}`; state.className = 'xs badge badge--error'; continue; }
        state.textContent = 'Queued for import…';
        trackJob(res.job_id).then((job) => {
          if (job.status !== 'succeeded') { state.textContent = `Failed: ${job.error || job.status}`; state.className = 'xs badge badge--error'; return; }
          const entries = job.result.entries;
          const ok = entries.filter((/** @type {any} */ e) => e.status === 'imported').length;
          state.textContent = `${ok} of ${entries.length} imported`;
          state.className = ok === entries.length ? 'xs badge badge--ok' : 'xs badge badge--review';
          const sheetRes = job.result.sheets || [];
          if (sheetRes.length && sheetRes[0].sheet_number !== undefined) {
            const bad = sheetRes.filter((/** @type {any} */ r) => r.error);
            toast(`Filled in ${sheetRes.length - bad.length} page(s) from sheets ${sheetRes.find((/** @type {any} */ r) => !r.error)?.sheet_number ?? ''} onward${bad.length ? `; ${bad.map((/** @type {any} */ r) => r.error).join(', ')}` : ''}. Check them in Check.`, bad.length ? 'error' : 'info');
          }
          mount(detail, entries.map((/** @type {any} */ e) =>
            h('li', {}, h('span', { class: `badge${e.status === 'rejected' ? ' badge--error' : e.status === 'imported' ? (e.detail ? ' badge--review' : ' badge--ok') : ''}`, text: e.status }), ' ', h('span', { class: 'mono xs', text: e.name }),
              e.detail ? h('span', { class: 'xs muted', text: ` — ${e.detail}` }) : null,
              ...(e.source_ids || []).map((/** @type {string} */ id, /** @type {number} */ i) => h('a', { class: 'xs upload-check', href: `#/sources/${id}` }, (e.source_ids.length > 1 ? `Check page ${i + 1}` : 'Check what it found'), icon('right'))))));
          onImported();
          window.dispatchEvent(new CustomEvent('hp:changed'));
        });
      } catch (err) {
        state.textContent = err instanceof Error ? err.message : String(err);
        state.className = 'xs badge badge--error';
      }
    }
    fileInput.value = '';
  }

  return h('section', { class: 'panel import' },
    h('div', { class: 'import__grid' },
      drop,
      h('div', { class: 'stack import__opts' },
        h('h3', { text: 'This import' }),
        h('div', { class: 'field' }, h('span', { text: 'Source type' }), sourceType),
        h('label', { class: 'field' }, h('span', { text: 'Session (optional)' }), session),
        h('div', { class: 'row import__pair' },
          h('label', { class: 'field' }, h('span', { text: 'Date (optional)' }), date),
          h('label', { class: 'field' }, h('span', { text: 'Style' }), era)),
        h('label', { class: 'field' }, h('span', { text: 'PDF pages (optional)' }), pdfPages),
        h('p', { class: 'xs muted' }, 'Written-out writing sheets go in ', h('a', { href: '#/sources?tab=sheets', text: 'Writing sheets' }), ' instead, so they are filled in from the sheet.'),
        extras.length ? h('p', { class: 'xs muted', text: extras.join(' · ') }) : null)),
    list);
}

/** @param {string[]} sourceIds @param {any} status */
async function recognitionDialog(sourceIds, status) {
  const dialog = /** @type {HTMLDialogElement} */ (h('dialog', { class: 'dialog', 'aria-labelledby': 'rec-title' }));
  const content = h('div', { class: 'stack' }, loadingState(2));
  const overview = h('input', { type: 'checkbox' });
  mount(dialog, h('div', { class: 'dialog__head' }, h('h2', { id: 'rec-title', text: 'Prepare a recognition package' }),
    h('button', { class: 'btn btn--quiet btn--sm', type: 'button', 'aria-label': 'Close', onClick: () => dialog.close() }, icon('x'))), content);
  document.body.appendChild(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.showModal();

  async function preview() {
    mount(content, loadingState(2));
    try {
      const p = await api('/api/recognition/preview', { body: { source_ids: sourceIds, include_overview: overview.checked } });
      /** @type {Record<string, {page: number, line: number}>} */
      const per = {};
      for (const it of p.items) {
        per[it.source_id] ||= { page: 0, line: 0 };
        per[it.source_id][it.context_type === 'page_overview' ? 'page' : 'line'] += 1;
      }
      mount(content,
        callout(`These images will leave this computer: they are sent to Anthropic through Claude Code (${p.model}). ${p.usage_note}`, 'review'),
        h('dl', { class: 'facts' },
          h('dt', { text: 'Items' }), h('dd', { text: fmt(p.item_count) }),
          h('dt', { text: 'Images' }), h('dd', { text: fmt(p.image_count) }),
          h('dt', { text: 'Total pixels' }), h('dd', { text: `${fmt(p.total_pixels)} (${(p.total_pixels / 1e6).toFixed(1)} MP)` }),
          h('dt', { text: 'Model' }), h('dd', { class: 'mono', text: p.model })),
        h('table', { class: 'table' }, h('thead', {}, h('tr', {}, h('th', { text: 'Page' }), h('th', { class: 'num', text: 'Overview' }), h('th', { class: 'num', text: 'Line crops' }))),
          h('tbody', {}, Object.entries(per).map(([sid, c]) => h('tr', {},
            h('td', {}, h('img', { class: 'thumb-xs', src: `/api/sources/${sid}/image?variant=thumb`, alt: '' }), h('span', { class: 'mono xs', text: sid })),
            h('td', { class: 'num', text: String(c.page) }), h('td', { class: 'num', text: String(c.line) }))))),
        h('label', { class: 'check' }, overview, 'Also look at each whole page and suggest areas to leave out (diagrams, printed text). One extra read per page.'),
        h('p', { class: 'xs muted', text: 'Already-recognised crops with the same image, model and prompt version are skipped (cache). Only open regions without human text are included.' }),
        h('div', { class: 'row' }, h('span', { class: 'spacer' }),
          h('button', { class: 'btn', type: 'button', onClick: () => dialog.close() }, 'Cancel'),
          h('button', { class: 'btn btn--primary', type: 'button', disabled: p.item_count === 0, onClick: build }, 'Build package')));
    } catch (err) { mount(content, errorState(err, preview)); }
  }

  async function build() {
    mount(content, loadingState(1), h('p', { class: 'small muted', text: 'Building package in the worker…' }));
    const { job_id } = await api('/api/recognition/packages', { body: { source_ids: sourceIds, include_overview: overview.checked } });
    const job = await trackJob(job_id);
    if (job.status !== 'succeeded') { mount(content, errorState(new Error(job.error || job.status))); return; }
    const r = job.result;
    mount(content,
      h('p', {}, `Package `, h('code', { text: r.package_id }), ` written: ${r.to_read ?? r.items} line(s) to read`,
        r.reused ? `, ${r.reused} filled in from earlier readings (no new read needed)` : '',
        r.cached_skipped ? `, ${r.cached_skipped} already done` : '', '.'),
      r.to_read === 0 ? callout(r.reused ? 'Nothing new to read: every line was filled in from an earlier reading. Go to Check.' : 'Nothing new to read: every line on these pages has already been read. Go to Check.') : null,
      h('p', { class: 'mono xs', text: r.path }),
      h('ol', { class: 'steps' },
        h('li', {}, 'Open Claude Code in the Handprinter repository folder.'),
        h('li', {}, 'Run ', h('code', { text: '/recognise' }), ' — it dispatches the Haiku recogniser subagent and validates every result.'),
        h('li', {}, 'Come back to ', h('a', { href: '#/sources?tab=recognition', onClick: () => dialog.close() }, 'Recognition packages'), ' and import the results.')),
      status.headless_recognition ? callout('Headless automation is enabled: you can also run the package from the packages list.') : null,
      h('div', { class: 'row' }, h('span', { class: 'spacer' }), h('button', { class: 'btn btn--primary', type: 'button', onClick: () => dialog.close() }, 'Done')));
  }

  overview.addEventListener('change', preview);
  preview();
}

/** @param {any} status */
function recognitionTab(status) {
  const body = h('div', { class: 'page-body' });
  async function load() {
    mount(body, loadingState(2));
    try {
      const pkgs = await api('/api/recognition/packages');
      if (!pkgs.length) {
        mount(body, emptyState({ title: 'No recognition packages yet', icon: 'send',
          body: h('p', {}, 'Select pages under ', h('a', { href: '#/sources', text: 'Pages' }), ' and choose “Prepare recognition…”. Recognition never starts on its own.') }));
        return;
      }
      mount(body,
        h('p', { class: 'small muted' }, `Configured recogniser: `, h('code', { text: status.recognition_model }), ` (pinned in .env and .claude/agents/handwriting-recogniser.md). Each result records the model it reports.`),
        h('div', { class: 'panel' }, h('table', { class: 'table' },
          h('thead', {}, h('tr', {}, ['Package', 'Created', 'Model', 'Imported', 'Pending', 'Failed', ''].map((t) => h('th', { class: ['Imported', 'Pending', 'Failed'].includes(t) ? 'num' : undefined, text: t })))),
          h('tbody', {}, pkgs.map((/** @type {any} */ p) => h('tr', {},
            h('td', {}, h('button', { class: 'btn btn--sm btn--quiet', type: 'button', title: 'See what was read', onClick: () => showReadings(p.id) }, icon('review'), h('code', { text: p.id }))),
            h('td', { class: 'xs', text: new Date(p.created_at).toLocaleString() }),
            h('td', { class: 'mono xs', text: p.model_configured }),
            h('td', { class: 'num', text: fmt(p.succeeded || 0) }),
            h('td', { class: 'num' }, p.pending ? h('span', { class: 'badge', text: fmt(p.pending) }) : '0',
              p.ready_to_import ? h('div', { class: 'xs', text: `${fmt(p.ready_to_import)} read, not imported` }) : null),
            h('td', { class: 'num' }, p.failed ? h('span', { class: 'badge badge--error', text: fmt(p.failed) }) : '0'),
            h('td', {}, h('div', { class: 'row' },
              h('button', { class: `btn btn--sm${p.ready_to_import ? ' btn--primary' : ''}`, type: 'button', onClick: () => runJob(`/api/recognition/packages/${p.id}/import`) }, p.ready_to_import ? `Import ${fmt(p.ready_to_import)} results` : 'Import results'),
              p.failed ? h('button', { class: 'btn btn--sm', type: 'button', onClick: () => api(`/api/recognition/packages/${p.id}/retry-failed`, { method: 'POST' }).then(() => { toast('Failed items reset to pending; run /recognise again.'); load(); }) }, 'Retry failed') : null,
              status.headless_recognition && p.pending ? h('button', { class: 'btn btn--sm', type: 'button', onClick: () => runJob(`/api/recognition/packages/${p.id}/headless`, {}) }, icon('play'), 'Run headless') : null))))))),
        status.headless_recognition ? null : h('p', { class: 'xs muted', text: 'Headless automation (claude -p) is off. Enable it with HANDPRINTER_HEADLESS_RECOGNITION=1 once doctor shows the claude CLI as logged in.' }));
    } catch (err) { mount(body, errorState(err, load)); }
  }
  /** What the recogniser read, item by item, with links into Review. @param {string} id */
  async function showReadings(id) {
    const list = h('div', { class: 'stack' }, loadingState(2));
    const dialog = /** @type {HTMLDialogElement} */ (h('dialog', { class: 'dialog dialog--wide' },
      h('div', { class: 'dialog__head' }, h('h2', { text: 'What was read' }),
        h('button', { class: 'btn btn--sm btn--quiet', type: 'button', onClick: () => dialog.close() }, 'Close')),
      h('p', { class: 'small muted', text: 'Suggestions only: check and approve each line in Review.' }), list));
    dialog.addEventListener('close', () => dialog.remove());
    document.body.appendChild(dialog);
    dialog.showModal();
    try {
      const pkg = await api(`/api/recognition/packages/${id}`);
      const lines = pkg.items.filter((/** @type {any} */ it) => it.context_type !== 'page_overview');
      const done = lines.filter((/** @type {any} */ it) => it.status === 'succeeded').length;
      mount(list,
        h('p', { class: 'small', text: `${done} of ${lines.length} line${lines.length === 1 ? '' : 's'} read and imported.` }),
        h('table', { class: 'table' }, h('tbody', {}, lines.map((/** @type {any} */ it) => h('tr', {},
          h('td', { class: 'xs muted', text: it.page || '' }),
          h('td', {}, it.status === 'succeeded'
            ? (it.text ? h('span', { class: 'mono', text: it.text }) : h('span', { class: 'muted', text: '(could not read)' }))
            : h('span', { class: `badge${it.status === 'failed' ? ' badge--error' : ''}`, text: it.status === 'failed' ? `failed: ${it.error || ''}` : 'not read yet' }),
            it.flags && it.flags.length ? h('span', { class: 'badge badge--review', text: 'needs a look' }) : null),
          h('td', {}, it.region_id && it.status === 'succeeded' ? h('a', { href: `#/review?source=${it.source_id}&region=${it.region_id}`, onClick: () => dialog.close() }, 'Review') : null))))));
    } catch (err) { mount(list, errorState(err)); }
  }

  /** @param {string} url @param {any} [bodyJson] */
  async function runJob(url, bodyJson) {
    try {
      const { job_id } = await api(url, { method: 'POST', body: bodyJson });
      const job = await trackJob(job_id);
      if (job.status === 'succeeded') {
        const r = job.result.import || job.result;
        toast(`${jobLabel(job.kind)}: ${r.imported ?? 0} imported, ${r.pending ?? 0} pending, ${r.failed ?? 0} failed${r.lines_refit ? `, word boxes re-cut on ${r.lines_refit} line${r.lines_refit === 1 ? '' : 's'}` : ''}${r.protected_human_edits ? `, ${r.protected_human_edits} human edits kept` : ''}`);
      } else toast(`${jobLabel(job.kind)} failed: ${job.error}`, 'error');
      window.dispatchEvent(new CustomEvent('hp:changed'));
      load();
    } catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
  }
  load();
  return body;
}

// ---------- single page ----------

/** @param {HTMLElement} root @param {string} sourceId */
async function renderPage(root, sourceId) {
  mount(root, loadingState(3));
  /** @type {any} */
  let src;
  try { src = await api(`/api/sources/${sourceId}`); } catch (err) { mount(root, errorState(err)); return; }
  let view = 'analysis';
  /** @type {any} */
  let analysis = null;
  /** @type {any[]} */
  let regions = [];
  const layers = { word: true, line: false, ruling: true, omitted: true, excluded: true };
  let omitMode = false;
  const summary = h('div', { class: 'stack' }, loadingState(3));
  const legend = h('div', { class: 'an-legend' });
  /** @type {number[][] | null} */
  let corners = src.processing.corners ? src.processing.corners.map((/** @type {number[]} */ c) => [...c]) : null;
  let rotation = src.processing.rotation || 0;
  let lighting = !!src.processing.lighting;
  const stage = h('div', { class: 'compare' });
  const status = h('p', { class: 'small muted', role: 'status' });

  function draw() {
    if (view === 'corners') {
      const ww = src.working_w, wh = src.working_h;
      const pts = corners || [[0, 0], [ww, 0], [ww, wh], [0, wh]];
      const poly = svg('polygon', { points: pts.map((p) => p.join(',')).join(' '), class: 'corner-poly' });
      const handles = pts.map((p, i) => {
        const c = svg('circle', { cx: p[0], cy: p[1], r: Math.max(ww, wh) / (window.matchMedia('(pointer: coarse)').matches ? 40 : 90), class: 'corner-handle', tabindex: 0, role: 'slider',
          'aria-label': `Corner ${['top left', 'top right', 'bottom right', 'bottom left'][i]}`, 'aria-valuetext': `${Math.round(p[0])}, ${Math.round(p[1])}` });
        c.addEventListener('pointerdown', (e) => {
          const el = /** @type {SVGCircleElement} */ (e.currentTarget);
          el.setPointerCapture(e.pointerId);
          const svgEl = /** @type {SVGSVGElement} */ (el.ownerSVGElement);
          const move = (/** @type {PointerEvent} */ ev) => {
            const m = svgEl.getScreenCTM();
            if (!m) return;
            const pt = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(m.inverse());
            pts[i] = [Math.max(0, Math.min(ww, pt.x)), Math.max(0, Math.min(wh, pt.y))];
            corners = pts;
            el.setAttribute('cx', String(pts[i][0])); el.setAttribute('cy', String(pts[i][1]));
            poly.setAttribute('points', pts.map((p) => p.join(',')).join(' '));
          };
          el.addEventListener('pointermove', move);
          el.addEventListener('pointerup', () => el.removeEventListener('pointermove', move), { once: true });
        });
        c.addEventListener('keydown', (e) => {
          const step = (e.shiftKey ? 50 : 5) * Math.max(1, ww / 1000);
          const d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
          if (!d) return;
          e.preventDefault();
          pts[i] = [Math.max(0, Math.min(ww, pts[i][0] + d[0])), Math.max(0, Math.min(wh, pts[i][1] + d[1]))];
          corners = pts;
          draw();
          /** @type {HTMLElement | null} */ (stage.querySelectorAll('.corner-handle')[i])?.focus();
        });
        return c;
      });
      mount(stage, h('div', { class: 'compare__frame' },
        h('img', { src: `/api/sources/${sourceId}/image?variant=working-display`, alt: 'Original orientation-corrected photo' }),
        svg('svg', { viewBox: `0 0 ${ww} ${wh}`, class: 'compare__overlay', preserveAspectRatio: 'none' }, poly, ...handles)));
      return;
    }
    const variant = view === 'original' ? 'working-display' : lighting ? 'display-norm' : 'display';
    const img = h('img', { src: `/api/sources/${sourceId}/image?variant=${variant}&rev=${src.processing_rev}`, alt: view === 'original' ? 'Original photo' : 'Corrected page with what the app found drawn on top' });
    if (view !== 'analysis') { mount(stage, h('div', { class: 'compare__frame' }, img)); return; }
    mount(stage, h('div', { class: `compare__frame${omitMode ? ' is-omitting' : ''}` }, img, analysisOverlay()));
  }

  /** The detected boxes and omitted areas over the corrected page, plus drawing of new omitted areas. */
  function analysisOverlay() {
    const pw = src.processed_w, ph = src.processed_h;
    const label = Math.max(pw, ph) / 55;
    const defs = svg('defs', {}, svg('pattern', { id: 'omit-hatch', patternUnits: 'userSpaceOnUse', width: label, height: label, patternTransform: 'rotate(45)' },
      svg('rect', { width: label, height: label, class: 'an-hatch-bg' }), svg('line', { x1: 0, y1: 0, x2: 0, y2: label, class: 'an-hatch-line', 'stroke-width': label / 3 })));
    const nodes = [];
    // Lines first so the words inside them sit on top and get the tap.
    for (const r of [...regions].sort((a, b) => (a.kind === 'line' ? 0 : 1) - (b.kind === 'line' ? 0 : 1))) {
      if (r.cls === 'omitted') continue;
      const isRuling = r.cls === 'printed_ruling';
      const excluded = r.status === 'excluded';
      if (isRuling ? !layers.ruling : excluded ? !layers.excluded : !(r.kind === 'word' ? layers.word : r.kind === 'line' ? layers.line : true)) continue;
      const cls = ['an-box', isRuling ? 'an-box--ruling' : excluded ? 'an-box--excluded' : `an-box--${r.kind}`, r.status === 'approved' ? 'an-box--approved' : ''];
      const box = svg('rect', { x: r.x, y: r.y, width: r.w, height: r.h, class: [...cls, isRuling ? '' : 'an-box--link'].join(' ') },
        svg('title', { text: `${r.kind} · ${r.cls} · ${r.status}${r.text ? ` · “${r.text}”` : ''}${isRuling ? '' : ' · tap to fix in Review'}` }));
      // Boxes are changed in Review: tapping one opens it there, selected and zoomed.
      if (!isRuling) box.addEventListener('click', () => { if (!omitMode) location.hash = `#/review?source=${sourceId}&region=${r.id}`; });
      nodes.push(box);
    }
    if (analysis && analysis.suggestions) {
      for (const sg of analysis.suggestions) {
        nodes.push(svg('g', { class: 'an-suggest-box' },
          svg('rect', { x: sg.x, y: sg.y, width: sg.w, height: sg.h, class: 'an-suggest__rect' }),
          svg('text', { x: sg.x + label * 0.4, y: sg.y + label * 1.2, 'font-size': label, class: 'an-omit__label', text: `Suggested: ${sg.label}` })));
      }
    }
    if (layers.omitted && analysis) {
      for (const a of analysis.omitted_areas) {
        const g = svg('g', { class: 'an-omit', tabindex: 0, role: 'button', 'aria-label': `Omitted area: ${a.label}. Press Enter to restore it.` },
          svg('rect', { x: a.x, y: a.y, width: a.w, height: a.h, fill: 'url(#omit-hatch)', class: 'an-omit__rect' }),
          svg('text', { x: a.x + label * 0.4, y: a.y + label * 1.2, 'font-size': label, class: 'an-omit__label', text: `Omitted · ${a.label}` }));
        const ask = () => restoreArea(a);
        g.addEventListener('click', (e) => { if (!omitMode) { e.stopPropagation(); ask(); } });
        g.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ask(); } });
        nodes.push(g);
      }
    }
    const overlay = /** @type {SVGSVGElement} */ (svg('svg', { viewBox: `0 0 ${pw} ${ph}`, class: 'compare__overlay', preserveAspectRatio: 'none' }, defs, ...nodes));
    /** @param {PointerEvent} ev */
    const toFrame = (ev) => {
      const m = overlay.getScreenCTM();
      const pt = m ? new DOMPoint(ev.clientX, ev.clientY).matrixTransform(m.inverse()) : { x: 0, y: 0 };
      return { x: Math.max(0, Math.min(pw, pt.x)), y: Math.max(0, Math.min(ph, pt.y)) };
    };
    overlay.addEventListener('pointerdown', (e) => {
      if (!omitMode) return;
      e.preventDefault();
      overlay.setPointerCapture(e.pointerId);
      const start = toFrame(e);
      const rect = svg('rect', { class: 'an-drawing', x: start.x, y: start.y, width: 0, height: 0 });
      overlay.appendChild(rect);
      /** @param {PointerEvent} ev */
      const box = (ev) => { const p = toFrame(ev); return { x: Math.min(start.x, p.x), y: Math.min(start.y, p.y), w: Math.abs(p.x - start.x), h: Math.abs(p.y - start.y) }; };
      const move = (/** @type {PointerEvent} */ ev) => { const b = box(ev); rect.setAttribute('x', String(b.x)); rect.setAttribute('y', String(b.y)); rect.setAttribute('width', String(b.w)); rect.setAttribute('height', String(b.h)); };
      overlay.addEventListener('pointermove', move);
      overlay.addEventListener('pointerup', (ev) => {
        overlay.removeEventListener('pointermove', move);
        const b = box(ev);
        if (b.w < pw / 200 || b.h < ph / 200) { rect.remove(); return; }
        omitDialog(b, () => rect.remove());
      }, { once: true });
    });
    return overlay;
  }

  async function loadAnalysis() {
    try {
      const [a, r] = await Promise.all([api(`/api/sources/${sourceId}/analysis`), api(`/api/sources/${sourceId}/regions`)]);
      analysis = a; regions = r.regions;
      renderSummary();
      if (view === 'analysis') draw();
    } catch (err) { mount(summary, errorState(err, loadAnalysis)); }
  }

  function renderSummary() {
    const a = analysis;
    const stateIcon = { done: 'check', warn: 'alert', info: 'info', todo: 'info' };
    mount(summary,
      h('ol', { class: 'an-steps' }, a.steps.map((/** @type {any} */ st) => h('li', { class: `an-step an-step--${st.state}` },
        h('span', { class: 'an-step__icon', 'aria-hidden': 'true' }, icon(/** @type {any} */ (stateIcon)[st.state] || 'info')),
        h('div', {}, h('strong', { text: st.label }), h('span', { class: 'sr-only', text: ` (${st.state === 'warn' ? 'check this' : st.state === 'todo' ? 'not yet' : 'ok'})` }),
          h('p', { class: 'xs muted', text: st.detail }))))),
      a.suggestions && a.suggestions.length ? h('div', { class: 'stack an-suggest' },
        h('h4', { text: `Suggested to leave out (${a.suggestions.length})` }),
        h('p', { class: 'xs muted', text: 'The page overview thinks these are not your handwriting. Nothing is left out until you tap Omit.' }),
        h('ul', { class: 'plain-list' }, a.suggestions.map((/** @type {any} */ sg) => h('li', { class: 'row row--between' },
          h('span', { class: 'small', text: sg.label }),
          h('span', { class: 'row' },
            h('button', { class: 'btn btn--sm', type: 'button', onClick: () => decide(sg, 'accept') }, icon('x'), 'Omit'),
            h('button', { class: 'btn btn--sm btn--quiet', type: 'button', onClick: () => decide(sg, 'dismiss') }, 'Dismiss'))))),
        a.suggestions.length > 1 ? h('button', { class: 'btn btn--sm', type: 'button', onClick: async () => {
          for (const sg of a.suggestions) await decide(sg, 'accept', true);
          toast(`${a.suggestions.length} areas omitted.`); loadAnalysis();
        } }, 'Omit all suggested') : null) : null,
      a.omitted_areas.length ? h('div', { class: 'stack' }, h('h4', { text: 'Omitted areas' }),
        h('ul', { class: 'plain-list' }, a.omitted_areas.map((/** @type {any} */ o) => h('li', { class: 'row row--between' },
          h('span', { class: 'small', text: o.label }),
          h('button', { class: 'btn btn--sm btn--quiet', type: 'button', onClick: () => restoreArea(o) }, icon('undo'), 'Restore')))))
        : null);
    mount(legend,
      ...[['word', 'Words', 'an-key--word'], ['line', 'Lines', 'an-key--line'], ['ruling', 'Printed ruling (ignored)', 'an-key--ruling'],
        ['omitted', 'Omitted areas', 'an-key--omitted'], ['excluded', 'Excluded', 'an-key--excluded']].map(([k, text, cls]) =>
        h('label', { class: 'check check--compact' },
          h('input', { type: 'checkbox', checked: /** @type {any} */ (layers)[k] ? true : undefined, onChange: (/** @type {Event} */ e) => { /** @type {any} */ (layers)[k] = /** @type {HTMLInputElement} */ (e.target).checked; draw(); } }),
          h('span', { class: `an-key ${cls}`, 'aria-hidden': 'true' }), text)));
  }

  /** @param {{x:number,y:number,w:number,h:number}} box @param {() => void} onCancel */
  function omitDialog(box, onCancel) {
    const dialog = /** @type {HTMLDialogElement} */ (h('dialog', { class: 'dialog', 'aria-labelledby': 'omit-title' }));
    const reasons = analysis?.omit_reasons || { diagram: 'Diagram or drawing', other: 'Other' };
    const picker = h('div', { class: 'stack', role: 'radiogroup', 'aria-label': 'What is in this area?' },
      Object.entries(reasons).map(([v, text], i) => h('label', { class: 'check' }, h('input', { type: 'radio', name: 'omit-reason', value: v, checked: i === 0 ? true : undefined }), text)));
    let done = false;
    mount(dialog, h('div', { class: 'dialog__head' }, h('h2', { id: 'omit-title', text: 'Leave this area out?' }),
      h('button', { class: 'btn btn--quiet btn--sm', type: 'button', 'aria-label': 'Close', onClick: () => dialog.close() }, icon('x'))),
      h('div', { class: 'stack' },
        h('p', { class: 'small', text: 'Nothing inside it will be used: detection ignores it, it is blanked out before any image is sent for recognition, and boxes mostly inside it are excluded from training. Approved words are kept. You can restore it at any time.' }),
        picker,
        h('label', { class: 'check' }, h('input', { type: 'checkbox', name: 'omit-all' }),
          'Also leave this out on all my other pages of the same size (for an app’s toolbar on screenshots)'),
        h('div', { class: 'row' }, h('span', { class: 'spacer' }),
          h('button', { class: 'btn', type: 'button', onClick: () => dialog.close() }, 'Cancel'),
          h('button', { class: 'btn btn--primary', type: 'button', onClick: async () => {
            const reason = /** @type {HTMLInputElement} */ (picker.querySelector('input:checked')).value;
            try {
              const all = /** @type {HTMLInputElement} */ (dialog.querySelector('input[name=omit-all]')).checked;
              const r = await api(`/api/sources/${sourceId}/omit`, { body: { processing_rev: src.processing_rev, box, reason, same_size_pages: all } });
              done = true; dialog.close();
              toast(`Area omitted. ${r.excluded} box(es) inside it excluded${r.kept_approved ? `; ${r.kept_approved} approved word(s) kept as you decided` : ''}${all ? `; also omitted on ${r.other_pages} other page${r.other_pages === 1 ? '' : 's'}` : ''}.`);
              loadAnalysis();
            } catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
          } }, 'Omit area'))));
    document.body.appendChild(dialog);
    dialog.addEventListener('close', () => { dialog.remove(); if (!done) onCancel(); });
    dialog.showModal();
  }

  /** @param {any} sg @param {'accept'|'dismiss'} what @param {boolean} [quiet] */
  async function decide(sg, what, quiet = false) {
    try {
      await api(`/api/sources/${sourceId}/suggestions/${sg.id}/${what}`, { method: 'POST' });
      if (!quiet) { toast(what === 'accept' ? `${sg.label}: left out.` : 'Suggestion dismissed.'); loadAnalysis(); }
    } catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
  }

  /** @param {any} area */
  async function restoreArea(area) {
    if (!window.confirm(`Restore “${area.label}”? Boxes it excluded go back to how they were.`)) return;
    try {
      const r = await api(`/api/sources/${sourceId}/omit/${area.id}`, { method: 'DELETE' });
      toast(`Area restored. ${r.restored} box(es) returned to review.`);
      loadAnalysis();
    } catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
  }

  const omitBtn = h('button', { class: 'btn btn--sm', type: 'button', 'aria-pressed': 'false', onClick: () => {
    omitMode = !omitMode;
    omitBtn.setAttribute('aria-pressed', String(omitMode));
    omitBtn.classList.toggle('btn--primary', omitMode);
    omitHint.hidden = !omitMode;
    if (view !== 'analysis') { view = 'analysis'; /** @type {HTMLInputElement} */ (views.querySelector('input[value=analysis]')).checked = true; }
    draw();
  } }, icon('draw'), 'Omit an area');
  const omitHint = h('p', { class: 'small an-hint', hidden: true, role: 'status', text: 'Drag a box over a diagram, printed text or anything that is not your handwriting. Click “Omit an area” again to stop.' });

  async function apply() {
    status.textContent = 'Applying in the worker…';
    try {
      const { job_id } = await api(`/api/sources/${sourceId}/processing`, { body: { corners, rotation, lighting } });
      const job = await trackJob(job_id);
      if (job.status !== 'succeeded') throw new Error(job.error);
      const moved = job.result.regions_remapped;
      toast(moved ? `Processing applied. ${moved} region(s) were moved and flagged as stale for review.` : 'Processing applied.');
      renderPage(root, sourceId);
    } catch (err) { status.textContent = err instanceof Error ? err.message : String(err); }
  }

  const views = h('div', { class: 'segmented', role: 'radiogroup', 'aria-label': 'Image shown' },
    [['analysis', 'What it found'], ['original', 'Original photo'], ['corners', 'Adjust corners']].map(([v, label]) =>
      h('label', {}, h('input', { type: 'radio', name: 'view', value: v, checked: v === view ? true : undefined, onChange: () => { view = v; draw(); } }), h('span', { text: label }))));
  const rot = h('input', { type: 'number', step: '0.5', min: '-180', max: '360', value: String(rotation), 'aria-label': 'Rotation in degrees', class: 'input-narrow',
    onInput: (/** @type {Event} */ e) => { rotation = Number(/** @type {HTMLInputElement} */ (e.target).value) || 0; } });
  const meta = metadataForm(src, () => renderPage(root, sourceId));

  mount(root,
    h('header', { class: 'page-head' },
      h('div', {}, h('nav', { class: 'row small page-nav', 'aria-label': 'Pages' }, h('a', { href: '#/sources' }, '← Sources'),
          src.prev_id ? h('a', { href: `#/sources/${src.prev_id}` }, icon('left'), 'Previous page') : null,
          src.next_id ? h('a', { href: `#/sources/${src.next_id}` }, 'Next page', icon('right')) : null),
        h('h1', { text: src.original_filename }),
        h('p', { class: 'small muted' }, `${src.working_w} × ${src.working_h} px · imported ${new Date(src.created_at).toLocaleDateString()} · `, h('code', { text: src.id }))),
      h('div', { class: 'row' },
        h('button', { class: 'btn', type: 'button', onClick: async () => {
          const { job_id } = await api(`/api/sources/${sourceId}/detect`, { body: { replace: true } });
          const job = await trackJob(job_id);
          toast(job.status === 'succeeded' ? `Detection: ${job.result.lines} lines, ${job.result.words} words proposed (untouched proposals replaced; edited ones kept; omitted areas ignored)${job.result.sheet ? `. Text filled in again from sheet ${job.result.sheet.sheet_number}` : ''}.` : `Detection failed: ${job.error}`, job.status === 'succeeded' ? 'info' : 'error');
          loadAnalysis();
        } }, icon('refresh'), 'Re-detect regions'),
        h('a', { class: 'btn', href: `#/review?source=${sourceId}` }, icon('review'), 'Review this page'),
        sheetPicker(sourceId, () => loadAnalysis()),
        h('button', { class: 'btn btn--danger', type: 'button', onClick: async () => {
          if (await deletePages([sourceId])) location.hash = src.next_id ? `#/sources/${src.next_id}` : '#/sources';
        } }, icon('trash'), 'Delete page'))),
    h('div', { class: 'page-body page-detail' },
      h('section', { class: 'stack' }, h('div', { class: 'row row--between' }, views, h('div', { class: 'row' }, status, omitBtn)), omitHint, stage, legend,
        h('p', { class: 'small muted', text: 'To fix a box, tap it: it opens in Review, where you drag its corners, split, merge or delete it.' })),
      h('aside', { class: 'stack' },
        h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h3', { text: 'What the app did with this page' })),
          h('div', { class: 'panel__body' }, summary)),
        h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h3', { text: 'Word boxes by Haiku' })),
          h('div', { class: 'panel__body' }, haikuCuts([sourceId]))),
        h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h3', { text: 'Photographic correction' })),
          h('div', { class: 'panel__body stack' },
            h('p', { class: 'xs muted', text: 'Corrects the camera only. Slant, spacing and baseline drift in the writing are never changed.' }),
            h('div', { class: 'row' },
              h('button', { class: 'btn btn--sm', type: 'button', onClick: async () => {
                const r = await api(`/api/sources/${sourceId}/suggest-corners`);
                if (r.corners) { corners = r.corners; view = 'corners'; /** @type {HTMLInputElement} */ (views.querySelector('input[value=corners]')).checked = true; draw(); }
                else toast(r.note);
              } }, 'Suggest page corners'),
              h('button', { class: 'btn btn--sm btn--quiet', type: 'button', onClick: () => { corners = null; draw(); } }, 'Clear corners')),
            h('label', { class: 'field' }, h('span', { text: 'Rotation (degrees, counter-clockwise)' }),
              h('div', { class: 'row' }, rot,
                h('button', { class: 'btn btn--sm', type: 'button', onClick: () => { rotation = (rotation + 90) % 360; /** @type {HTMLInputElement} */ (rot).value = String(rotation); } }, '+90°'))),
            h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: lighting ? true : undefined, onChange: (/** @type {Event} */ e) => { lighting = /** @type {HTMLInputElement} */ (e.target).checked; } }),
              'Even out lighting (display and detection only; training crops stay unaltered)'),
            h('button', { class: 'btn btn--primary', type: 'button', onClick: apply }, 'Apply correction'))),
        src.quality_flags.length || src.near_duplicates.length ? h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h3', { text: 'Signals to check' })),
          h('div', { class: 'panel__body stack' },
            h('div', { class: 'row' }, src.quality_flags.map(flagBadge)),
            src.near_duplicates.length ? h('p', { class: 'small' }, 'Looks like a near-duplicate of ', src.near_duplicates.map((/** @type {string} */ id) => h('a', { href: `#/sources/${id}`, class: 'mono xs' }, id, ' ')), '. Kept, never deleted automatically; duplicates always share a split.') : null,
            h('p', { class: 'xs muted', text: 'Automatic signals are fallible. They prompt a look; they prove nothing.' }))) : null,
        meta,
        src.proposals.length ? h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h3', { text: 'Page overview proposals' })),
          h('div', { class: 'panel__body stack' }, src.proposals.map((/** @type {any} */ p) => h('div', { class: 'small' },
            h('span', { class: 'badge badge--outline', text: p.payload.raw.region_type }), ' ', h('span', { class: 'xs muted', text: `${p.model_reported} · prompt ${p.prompt_version}` }),
            p.payload.reasons.length ? h('p', { class: 'xs', text: p.payload.reasons.join('; ') }) : null)))) : null)));
  draw();
  loadAnalysis();
}

/** @param {any} src @param {() => void} onSaved */
function metadataForm(src, onSaved) {
  const session = h('input', { type: 'text', value: src.session || '', maxlength: 80 });
  const date = h('input', { type: 'date', value: src.captured_date || '' });
  const type = selectEl(['paper', 'ipad'], src.source_type, () => {}, { paper: 'Paper', ipad: 'iPad export' });
  const era = selectEl(['current', 'older'], src.style_era, () => {}, { current: 'Current style', older: 'Older style' });
  const group = h('input', { type: 'text', placeholder: 'src_… of a related page', pattern: 'src_[0-9a-f]{16}' });
  const save = debounce(async () => {
    try {
      await api(`/api/sources/${src.id}`, { method: 'PATCH', body: {
        session: /** @type {HTMLInputElement} */ (session).value || null, captured_date: /** @type {HTMLInputElement} */ (date).value || null,
        source_type: /** @type {HTMLSelectElement} */ (type).value, style_era: /** @type {HTMLSelectElement} */ (era).value,
        ...(/** @type {HTMLInputElement} */ (group).value ? { group_with: /** @type {HTMLInputElement} */ (group).value } : {}),
      } });
      toast('Page details saved');
      if (/** @type {HTMLInputElement} */ (group).value) onSaved();
    } catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
  }, 500);
  for (const el of [session, date, type, era]) el.addEventListener('change', save);
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h3', { text: 'Page details' })),
    h('div', { class: 'panel__body stack' },
      h('label', { class: 'field' }, h('span', { text: 'Source type' }), type),
      h('label', { class: 'field' }, h('span', { text: 'Session' }), session),
      h('label', { class: 'field' }, h('span', { text: 'Date' }), date),
      h('label', { class: 'field' }, h('span', { text: 'Style era' }), era),
      h('label', { class: 'field' }, h('span', { text: `Source group (${src.siblings.length} related page${src.siblings.length === 1 ? '' : 's'})` }),
        h('div', { class: 'row' }, group, h('button', { class: 'btn btn--sm', type: 'button', onClick: save }, icon('link'), 'Group'))),
      h('p', { class: 'xs muted', text: 'Grouped pages always fall in the same train/validation/test split.' })));
}
