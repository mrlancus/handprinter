// Review: queue, zoomable page workspace with editable regions, and the inspector.
import { api } from '../api.js';
import { fmt, h, isTyping, mount, svg } from '../dom.js';
import { Viewport, clampBox, rectFrom } from '../geometry.js';
import { icon } from '../components/icons.js';
import { emptyState, errorState, flagBadge, loadingState, statusBadge } from '../components/states.js';
import { toast } from '../app.js';

const CLASSES = ['handwriting', 'printed_text', 'printed_ruling', 'underline', 'strike_through', 'overwrite', 'insertion', 'diagram', 'equation', 'uncertain'];
const CLASS_LABEL = /** @type {Record<string,string>} */ ({
  handwriting: 'Handwriting', printed_text: 'Printed text', printed_ruling: 'Printed ruling', underline: 'Handwritten underline',
  strike_through: 'Strike-through', overwrite: 'Overwrite', insertion: 'Insertion', diagram: 'Diagram', equation: 'Equation', uncertain: 'Uncertain / other',
});
const QUEUE_SIZE = 50;

/** @typedef {{id: string, source_id: string, parent_id: string|null, kind: string, cls: string, x: number, y: number, w: number, h: number,
 *  status: string, deferred: number, text: string|null, text_origin: string|null, flags: string[], resolved_flags: string[], review_reason: string|null,
 *  other_author: number, revision: number, created_by: string, has_unapplied_proposal?: boolean, original_filename?: string, deleted?: number}} Region */

/** @param {HTMLElement} root @param {URLSearchParams} params */
export async function render(root, params) {
  const state = {
    filter: {
      status: params.get('status') ?? (params.get('source') ? '' : 'needs_review'),
      flag: params.get('flag') || '', kind: params.get('kind') || '',
      source_type: params.get('source_type') || '', session: params.get('session') || '', source_id: params.get('source') || '',
    },
    audit: params.get('audit') === '1',
    offset: 0,
    /** @type {Region[]} */ queue: [],
    queueTotal: 0,
    /** @type {string[]} */ flags: [],
    /** @type {any} */ page: null,
    /** @type {Region[]} */ regions: [],
    /** @type {any[]} */ links: [],
    /** @type {string|null} */ selected: params.get('region'),
    /** @type {Set<string>} */ multi: new Set(),
    tool: 'select',
    drawKind: 'word',
    showOriginal: false,
    /** @type {Record<string, boolean>} */ show: { line: true, word: true, mark: true, block: true },
    saving: /** @type {'idle'|'saving'|'saved'|'error'} */ ('idle'),
    saveMessage: '',
  };

  root.classList.add('view--fill');
  const queueEl = h('aside', { class: 'rv-queue', id: 'rv-queue', 'aria-label': 'Review queue' });
  const wsEl = h('section', { class: 'rv-ws', 'aria-label': 'Page workspace' });
  const inspEl = h('aside', { class: 'rv-insp', 'aria-label': 'Inspector' });
  const saveEl = h('span', { class: 'rv-save xs', role: 'status', 'aria-live': 'polite' });
  mount(root,
    h('header', { class: 'page-head page-head--compact' },
      h('div', {}, h('h1', { text: 'Review' }), h('p', { class: 'small muted', text: 'Machine proposals are suggestions. Your corrections are kept unless you explicitly apply a proposal.' })),
      h('div', { class: 'row' }, saveEl,
        h('a', { class: 'btn btn--sm rv-jump', href: '#rv-queue', onClick: (/** @type {Event} */ e) => { e.preventDefault(); queueEl.scrollIntoView({ behavior: 'smooth' }); } }, icon('review'), 'Queue'),
        h('button', { class: 'btn btn--quiet btn--sm rv-help', type: 'button', onClick: showHelp }, h('kbd', { text: '?' }), 'Shortcuts'))),
    h('div', { class: 'rv' }, queueEl, wsEl, inspEl));

  // ---------- data ----------
  async function loadQueue() {
    mount(queueEl, queueControls(), loadingState(4));
    if (!state.page) { mount(wsEl, h('div', { class: 'rv-ws__loading' }, loadingState(1))); mount(inspEl, loadingState(5)); }
    try {
      if (state.audit) {
        const data = await api(`/api/audit?n=30`);
        state.queue = data.items; state.queueTotal = data.population;
      } else {
        const q = new URLSearchParams({ limit: String(QUEUE_SIZE), offset: String(state.offset) });
        for (const [k, v] of Object.entries(state.filter)) if (v) q.set(k, v);
        const data = await api(`/api/regions?${q}`);
        state.queue = data.items; state.queueTotal = data.total; state.flags = data.flags;
      }
      drawQueue();
      if (!state.selected && state.queue.length) await select(state.queue[0].id, state.queue[0].source_id);
      else if (!state.page && state.filter.source_id) await loadPage(state.filter.source_id);
      else if (!state.page && !state.queue.length) drawWorkspace();
    } catch (err) { mount(queueEl, queueControls(), errorState(err, loadQueue)); }
  }

  /** @param {string} sourceId */
  async function loadPage(sourceId) {
    if (state.page && state.page.id === sourceId) return;
    mount(wsEl, loadingState(1));
    const [page, regions] = await Promise.all([api(`/api/sources/${sourceId}`), api(`/api/sources/${sourceId}/regions`)]);
    state.page = page; state.regions = regions.regions; state.links = regions.links;
    state.showOriginal = false;
    workspace.load();
  }

  /** @param {string} regionId @param {string} [sourceId] */
  async function select(regionId, sourceId) {
    state.selected = regionId;
    state.multi.clear();
    try {
      if (sourceId) await loadPage(sourceId);
    } catch (err) { mount(wsEl, errorState(err)); return; }
    const r = current();
    if (r) workspace.focus(r);
    workspace.draw();
    drawQueue();
    drawInspector();
    history.replaceState(null, '', `#/review?${new URLSearchParams({ ...Object.fromEntries(Object.entries(state.filter).filter(([, v]) => v)), region: regionId })}`);
  }

  const current = () => state.regions.find((r) => r.id === state.selected) || null;

  /** Replace a region locally (optimistic) and in the queue. @param {Region} r */
  function upsert(r) {
    const i = state.regions.findIndex((x) => x.id === r.id);
    if (r.deleted) { if (i >= 0) state.regions.splice(i, 1); }
    else if (i >= 0) state.regions[i] = { ...state.regions[i], ...r };
    else state.regions.push(r);
    const qi = state.queue.findIndex((x) => x.id === r.id);
    if (qi >= 0) state.queue[qi] = { ...state.queue[qi], ...r };
  }

  /** @param {'idle'|'saving'|'saved'|'error'} s @param {string} [msg] */
  function setSave(s, msg = '') {
    state.saving = s; state.saveMessage = msg;
    saveEl.className = `rv-save xs${s === 'error' ? ' badge badge--error' : ''}`;
    saveEl.textContent = s === 'saving' ? 'Saving…' : s === 'saved' ? 'All changes saved' : s === 'error' ? `Not saved: ${msg}` : '';
  }

  /**
   * Send a change; the UI is updated before the request (acknowledged instantly) and reverted on failure.
   * @param {Region} region @param {Record<string, any>} body @param {Partial<Region>} optimistic
   */
  async function patch(region, body, optimistic = {}) {
    const before = { ...region };
    upsert({ ...region, ...optimistic });
    workspace.draw(); drawQueue(); drawInspector();
    setSave('saving');
    try {
      const updated = await api(`/api/regions/${region.id}`, { method: 'PATCH', body: { expected_revision: region.revision, processing_rev: state.page?.processing_rev, ...body } });
      upsert(updated);
      setSave('saved');
      window.dispatchEvent(new CustomEvent('hp:changed'));
      return updated;
    } catch (err) {
      upsert(before);
      setSave('error', err instanceof Error ? err.message : String(err));
      if (err instanceof Error && 'status' in err && err.status === 409) reloadPage();
      return null;
    } finally {
      workspace.draw(); drawQueue(); drawInspector();
    }
  }

  async function reloadPage() {
    if (!state.page) return;
    const regions = await api(`/api/sources/${state.page.id}/regions`);
    state.regions = regions.regions; state.links = regions.links;
    workspace.draw(); drawInspector();
  }

  // ---------- queue ----------
  function queueControls() {
    const sel = (/** @type {string} */ key, /** @type {[string,string][]} */ opts, /** @type {string} */ label) => h('label', { class: 'field' }, h('span', { text: label }),
      h('select', { onChange: (/** @type {Event} */ e) => { /** @type {any} */ (state.filter)[key] = /** @type {HTMLSelectElement} */ (e.target).value; state.offset = 0; state.audit = false; state.selected = null; loadQueue(); } },
        opts.map(([v, l]) => h('option', { value: v, selected: /** @type {any} */ (state.filter)[key] === v ? true : undefined }, l))));
    return h('div', { class: 'rv-queue__filters' },
      h('div', { class: 'row row--between' }, h('h2', { text: state.audit ? 'Random audit' : 'Queue' }),
        h('button', { class: `btn btn--sm${state.audit ? ' btn--primary' : ''}`, type: 'button', 'aria-pressed': String(state.audit),
          title: 'Sample unflagged, unreviewed machine proposals: confidently wrong labels exist',
          onClick: () => { state.audit = !state.audit; state.selected = null; loadQueue(); } }, 'Audit sample')),
      state.audit ? h('p', { class: 'xs muted', text: 'A random sample of unflagged machine proposals nobody has reviewed yet.' }) : h('div', { class: 'rv-queue__grid' },
        sel('status', [['needs_review', 'Needs review'], ['proposed', 'Proposed'], ['unprocessed', 'Unprocessed'], ['approved', 'Approved'], ['excluded', 'Excluded'], ['', 'Any status']], 'Status'),
        sel('kind', [['', 'Any kind'], ['word', 'Words'], ['line', 'Lines'], ['mark', 'Marks'], ['block', 'Blocks']], 'Kind'),
        sel('flag', [['', 'Any flag'], ...state.flags.map((f) => /** @type {[string,string]} */ ([f, f.replaceAll('_', ' ')]))], 'Flag'),
        sel('source_type', [['', 'Paper + iPad'], ['paper', 'Paper'], ['ipad', 'iPad']], 'Source')),
      state.filter.source_id ? h('div', { class: 'row xs' }, h('span', { class: 'muted', text: 'Page filter:' }), h('code', { text: state.filter.source_id }),
        h('button', { class: 'btn btn--quiet btn--sm', type: 'button', onClick: () => { state.filter.source_id = ''; state.selected = null; loadQueue(); } }, icon('x'), 'Clear')) : null);
  }

  function drawQueue() {
    const items = state.queue;
    const list = items.length
      ? h('ol', { class: 'rv-list', role: 'listbox', 'aria-label': 'Regions to review' }, items.map((r) => h('li', {
          class: `rv-item${r.id === state.selected ? ' is-selected' : ''}`, role: 'option', 'aria-selected': String(r.id === state.selected), tabindex: r.id === state.selected ? 0 : -1,
          onClick: () => select(r.id, r.source_id), onKeydown: (/** @type {KeyboardEvent} */ e) => { if (e.key === 'Enter') select(r.id, r.source_id); } },
        h('img', { class: 'rv-item__crop', src: `/api/regions/${r.id}/crop?rev=${r.revision}`, alt: '', loading: 'lazy' }),
        h('div', { class: 'rv-item__meta' },
          h('span', { class: `rv-item__text${r.text ? '' : ' faint'}`, text: r.text ?? 'No transcription' }),
          h('div', { class: 'row' }, statusBadge(r.status), h('span', { class: 'xs muted', text: r.kind }), r.deferred ? h('span', { class: 'badge', text: 'deferred' }) : null,
            r.flags.length ? h('span', { class: 'badge badge--review', text: `${r.flags.length} flag${r.flags.length > 1 ? 's' : ''}` }) : null)))))
      : emptyState({ title: state.audit ? 'Nothing to audit' : 'Queue is clear', icon: 'check',
          body: state.audit ? 'There are no unflagged, unreviewed proposals right now.' : 'No regions match these filters. Try another status or import more pages.' });
    const pager = !state.audit && state.queueTotal > QUEUE_SIZE ? h('div', { class: 'row rv-queue__pager' },
      h('button', { class: 'btn btn--sm', type: 'button', disabled: state.offset === 0, onClick: () => { state.offset -= QUEUE_SIZE; loadQueue(); } }, icon('left')),
      h('span', { class: 'xs muted', text: `${state.offset + 1}–${Math.min(state.offset + QUEUE_SIZE, state.queueTotal)} of ${fmt(state.queueTotal)}` }),
      h('button', { class: 'btn btn--sm', type: 'button', disabled: state.offset + QUEUE_SIZE >= state.queueTotal, onClick: () => { state.offset += QUEUE_SIZE; loadQueue(); } }, icon('right'))) : null;
    mount(queueEl, queueControls(), h('p', { class: 'xs muted rv-queue__count', text: state.audit ? `${fmt(state.queueTotal)} unflagged proposals in the population` : `${fmt(state.queueTotal)} region${state.queueTotal === 1 ? '' : 's'}` }), list, pager);
  }

  /** @param {number} delta */
  function step(delta) {
    const i = state.queue.findIndex((r) => r.id === state.selected);
    const next = state.queue[Math.max(0, Math.min(state.queue.length - 1, (i < 0 ? 0 : i + delta)))];
    if (next && next.id !== state.selected) select(next.id, next.source_id);
  }

  // ---------- workspace ----------
  const workspace = (() => {
    const container = h('div', { class: 'ws', tabindex: 0, 'aria-label': 'Page image. Drag to pan, scroll to zoom.' });
    const stage = h('div', { class: 'ws__stage' });
    const img = /** @type {HTMLImageElement} */ (h('img', { class: 'ws__img', alt: '', draggable: 'false' }));
    const overlay = /** @type {SVGSVGElement} */ (svg('svg', { class: 'ws__overlay' }));
    const toolbar = h('div', { class: 'ws__toolbar', role: 'toolbar', 'aria-label': 'Workspace tools' });
    const note = h('div', { class: 'ws__note xs', role: 'status' });
    stage.append(img, overlay);
    container.append(stage);
    /** @type {Viewport | null} */
    let vp = null;
    let fullRes = false;
    /** @type {{mode: string, start: {x:number,y:number}, origin?: any, handle?: string, rect?: SVGRectElement} | null} */
    let drag = null;

    function apply() {
      if (!vp) return;
      stage.style.transform = vp.css();
      if (!fullRes && state.page && !state.showOriginal && vp.scale * state.page.processed_w > (img.naturalWidth || 2000) * 1.3) {
        fullRes = true;
        img.src = `/api/sources/${state.page.id}/image?variant=full&rev=${state.page.processing_rev}`;
      }
      drawHandles();
    }

    function load() {
      const p = state.page;
      fullRes = false;
      vp = new Viewport(p.processed_w, p.processed_h);
      stage.style.width = `${p.processed_w}px`;
      stage.style.height = `${p.processed_h}px`;
      overlay.setAttribute('viewBox', `0 0 ${p.processed_w} ${p.processed_h}`);
      overlay.setAttribute('width', String(p.processed_w));
      overlay.setAttribute('height', String(p.processed_h));
      setImage();
      mount(wsEl, toolbar, container, note);
      drawToolbar();
      requestAnimationFrame(() => { fit(); draw(); });
    }

    function setImage() {
      const p = state.page;
      img.src = state.showOriginal ? `/api/sources/${p.id}/image?variant=working-display` : `/api/sources/${p.id}/image?variant=display&rev=${p.processing_rev}`;
      overlay.style.display = state.showOriginal && p.processing_rev > 0 ? 'none' : '';
      note.textContent = state.showOriginal
        ? (p.processing_rev > 0 ? 'Showing the original photo. Region outlines are hidden because they belong to the corrected frame.' : 'Showing the original photo (no correction applied, so it matches).')
        : '';
    }

    function fit() {
      if (!vp) return;
      const rect = container.getBoundingClientRect();
      vp.fit(rect.width, rect.height);
      apply();
    }

    /** @param {Region} r */
    function focus(r) {
      if (!vp) return;
      const rect = container.getBoundingClientRect();
      if (rect.width === 0) return;
      vp.focus(r, rect.width, rect.height);
      apply();
    }

    function drawToolbar() {
      const btn = (/** @type {string} */ ic, /** @type {string} */ label, /** @type {() => void} */ fn, /** @type {string} */ key, pressed = /** @type {boolean|undefined} */ (undefined)) =>
        h('button', { class: `btn btn--sm${pressed ? ' is-on' : ''}`, type: 'button', title: `${label} (${key})`, 'aria-label': label, 'aria-pressed': pressed === undefined ? undefined : String(pressed), onClick: fn }, icon(ic));
      const kinds = h('div', { class: 'row ws__kinds', role: 'group', 'aria-label': 'Show region kinds' },
        Object.keys(state.show).map((k) => h('label', { class: 'check xs' }, h('input', { type: 'checkbox', checked: state.show[k] ? true : undefined,
          onChange: (/** @type {Event} */ e) => { state.show[k] = /** @type {HTMLInputElement} */ (e.target).checked; draw(); } }), `${k}s`)));
      mount(toolbar,
        btn('zoomOut', 'Zoom out', () => zoomCentre(1 / 1.25), '−'), btn('zoomIn', 'Zoom in', () => zoomCentre(1.25), '+'), btn('fit', 'Fit page', fit, '0'),
        h('span', { class: 'ws__sep' }),
        btn('move', 'Select and move', () => setTool('select'), 'V', state.tool === 'select'),
        btn('draw', 'Draw a new region', () => setTool('draw'), 'N', state.tool === 'draw'),
        state.tool === 'draw' ? h('select', { class: 'ws__drawkind', 'aria-label': 'Kind of region to draw', onChange: (/** @type {Event} */ e) => { state.drawKind = /** @type {HTMLSelectElement} */ (e.target).value; } },
          ['word', 'line', 'mark', 'block'].map((k) => h('option', { value: k, selected: k === state.drawKind ? true : undefined }, k))) : null,
        h('span', { class: 'ws__sep' }),
        h('button', { class: `btn btn--sm${state.showOriginal ? ' is-on' : ''}`, type: 'button', 'aria-pressed': String(state.showOriginal), title: 'Original / processed (O)',
          onClick: toggleOriginal }, icon('eye'), state.showOriginal ? 'Original' : 'Processed'),
        h('span', { class: 'spacer' }), kinds,
        state.page ? h('a', { class: 'btn btn--sm btn--quiet', href: `#/sources/${state.page.id}`, title: 'Page correction and details' }, state.page.original_filename) : null);
    }

    /** @param {string} t */
    function setTool(t) { state.tool = t; container.classList.toggle('is-drawing', t === 'draw'); drawToolbar(); }
    function toggleOriginal() { state.showOriginal = !state.showOriginal; fullRes = false; setImage(); drawToolbar(); }
    /** @param {number} f */
    function zoomCentre(f) { if (!vp) return; const r = container.getBoundingClientRect(); vp.zoomAt(f, r.width / 2, r.height / 2); apply(); }

    function draw() {
      if (!state.page || !vp) {
        if (!state.page) mount(wsEl, emptyState({ title: 'Nothing selected', icon: 'review', center: true, body: 'Pick a region from the queue, or open a page from Sources.' }));
        return;
      }
      const nodes = [];
      const visible = state.regions.filter((r) => state.show[r.kind]);
      visible.sort((a, b) => (a.kind === 'line' ? -1 : 0) - (b.kind === 'line' ? -1 : 0));
      for (const r of visible) {
        const cls = ['rg', `rg--${r.kind}`, `rg--${r.status}`];
        if (r.flags.length) cls.push('rg--flagged');
        if (r.id === state.selected) cls.push('is-selected');
        if (state.multi.has(r.id)) cls.push('is-multi');
        if (r.cls !== 'handwriting') cls.push('rg--nonhand');
        const rect = svg('rect', { x: r.x, y: r.y, width: r.w, height: r.h, class: cls.join(' '), 'data-id': r.id });
        nodes.push(rect);
      }
      mount(overlay, ...nodes);
      drawHandles();
    }

    function drawHandles() {
      overlay.querySelectorAll('.rg-handle').forEach((n) => n.remove());
      const r = current();
      if (!r || !vp || state.tool !== 'select') return;
      const s = 9 / vp.scale;
      const pts = { nw: [r.x, r.y], ne: [r.x + r.w, r.y], se: [r.x + r.w, r.y + r.h], sw: [r.x, r.y + r.h] };
      for (const [name, [x, y]] of Object.entries(pts)) {
        overlay.appendChild(svg('rect', { x: x - s / 2, y: y - s / 2, width: s, height: s, class: `rg-handle rg-handle--${name}`, 'data-handle': name }));
      }
    }

    container.addEventListener('wheel', (e) => {
      if (!vp) return;
      e.preventDefault();
      const r = container.getBoundingClientRect();
      vp.zoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - r.left, e.clientY - r.top);
      apply();
    }, { passive: false });

    container.addEventListener('pointerdown', (e) => {
      if (!vp || e.button !== 0) return;
      const r = container.getBoundingClientRect();
      const v = { x: e.clientX - r.left, y: e.clientY - r.top };
      const f = vp.toFrame(v.x, v.y);
      const target = /** @type {Element} */ (e.target);
      container.setPointerCapture(e.pointerId);
      if (state.tool === 'draw' && !state.showOriginal) {
        const rect = /** @type {SVGRectElement} */ (svg('rect', { class: 'rg rg--drawing', x: f.x, y: f.y, width: 0, height: 0 }));
        overlay.appendChild(rect);
        drag = { mode: 'draw', start: f, rect };
      } else if (target.classList.contains('rg-handle')) {
        const cur = current();
        drag = { mode: 'resize', start: f, origin: cur && { ...cur }, handle: /** @type {HTMLElement} */ (/** @type {unknown} */ (target)).dataset.handle };
      } else if (target.classList.contains('rg')) {
        const id = /** @type {HTMLElement} */ (/** @type {unknown} */ (target)).dataset.id || '';
        if (e.shiftKey) {
          state.multi.has(id) ? state.multi.delete(id) : state.multi.add(id);
          if (state.selected) state.multi.add(state.selected);
          draw(); drawInspector();
          drag = null;
          return;
        }
        if (id !== state.selected) { select(id); drag = null; return; }
        const cur = current();
        drag = { mode: 'move', start: f, origin: cur && { ...cur } };
      } else {
        drag = { mode: 'pan', start: v, origin: { tx: vp.tx, ty: vp.ty } };
        container.classList.add('is-panning');
      }
    });

    container.addEventListener('pointermove', (e) => {
      if (!drag || !vp) return;
      const r = container.getBoundingClientRect();
      const v = { x: e.clientX - r.left, y: e.clientY - r.top };
      const f = vp.toFrame(v.x, v.y);
      if (drag.mode === 'pan') {
        vp.tx = drag.origin.tx + v.x - drag.start.x; vp.ty = drag.origin.ty + v.y - drag.start.y;
        apply();
      } else if (drag.mode === 'draw' && drag.rect) {
        const b = rectFrom(drag.start, f);
        drag.rect.setAttribute('x', String(b.x)); drag.rect.setAttribute('y', String(b.y));
        drag.rect.setAttribute('width', String(b.w)); drag.rect.setAttribute('height', String(b.h));
      } else if ((drag.mode === 'move' || drag.mode === 'resize') && drag.origin) {
        const o = drag.origin, dx = f.x - drag.start.x, dy = f.y - drag.start.y;
        let b = { x: o.x, y: o.y, w: o.w, h: o.h };
        if (drag.mode === 'move') b = { ...b, x: o.x + dx, y: o.y + dy };
        else {
          const hd = drag.handle || '';
          if (hd.includes('w')) { b.x = o.x + dx; b.w = o.w - dx; }
          if (hd.includes('e')) b.w = o.w + dx;
          if (hd.includes('n')) { b.y = o.y + dy; b.h = o.h - dy; }
          if (hd.includes('s')) b.h = o.h + dy;
          b = rectFrom({ x: b.x, y: b.y }, { x: b.x + b.w, y: b.y + b.h });
        }
        const el = overlay.querySelector(`[data-id="${o.id}"]`);
        if (el) { el.setAttribute('x', String(b.x)); el.setAttribute('y', String(b.y)); el.setAttribute('width', String(b.w)); el.setAttribute('height', String(b.h)); }
        drag.origin.next = b;
      }
    });

    container.addEventListener('pointerup', async (e) => {
      const d = drag;
      drag = null;
      container.classList.remove('is-panning');
      if (!d || !vp || !state.page) return;
      if (d.mode === 'draw' && d.rect) {
        const r = container.getBoundingClientRect();
        const b = clampBox(rectFrom(d.start, vp.toFrame(e.clientX - r.left, e.clientY - r.top)), state.page.processed_w, state.page.processed_h);
        d.rect.remove();
        if (b.w * vp.scale < 4 || b.h * vp.scale < 4) return;
        const parent = state.drawKind === 'word' || state.drawKind === 'mark' ? enclosingLine(b) : null;
        try {
          setSave('saving');
          const created = await api(`/api/sources/${state.page.id}/regions`, { body: { processing_rev: state.page.processing_rev, kind: state.drawKind, cls: state.drawKind === 'mark' ? 'underline' : state.drawKind === 'block' ? 'uncertain' : 'handwriting', box: b, parent_id: parent } });
          upsert(created); setSave('saved');
          select(created.id);
        } catch (err) { setSave('error', err instanceof Error ? err.message : String(err)); draw(); }
      } else if ((d.mode === 'move' || d.mode === 'resize') && d.origin && d.origin.next) {
        const b = clampBox(d.origin.next, state.page.processed_w, state.page.processed_h);
        const region = state.regions.find((x) => x.id === d.origin.id);
        if (region) patch(region, { box: b }, { ...b });
      }
    });

    /** @param {{x:number,y:number,w:number,h:number}} b */
    function enclosingLine(b) {
      let best = null, bestArea = 0;
      for (const r of state.regions) {
        if (r.kind !== 'line') continue;
        const w = Math.min(r.x + r.w, b.x + b.w) - Math.max(r.x, b.x), hh = Math.min(r.y + r.h, b.y + b.h) - Math.max(r.y, b.y);
        const a = Math.max(0, w) * Math.max(0, hh);
        if (a > bestArea) { bestArea = a; best = r.id; }
      }
      return best;
    }

    const ro = new ResizeObserver(() => { if (vp && state.page) apply(); });
    ro.observe(container);

    return { load, draw, focus, fit, zoomCentre, setTool, toggleOriginal,
      pan: (/** @type {number} */ dx, /** @type {number} */ dy) => { if (vp) { vp.tx += dx; vp.ty += dy; apply(); } },
      destroy: () => ro.disconnect() };
  })();

  function drawWorkspace() { workspace.draw(); }

  // ---------- inspector ----------
  function drawInspector() {
    const r = current();
    if (!r) {
      mount(inspEl, emptyState({ title: state.page ? 'Select a region' : 'Inspector', icon: 'review',
        body: state.page ? h('p', {}, 'Click an outline on the page, or press ', h('kbd', { text: 'J' }), ' for the next item in the queue.')
          : 'Transcription, decisions and provenance for the selected region appear here.' }));
      return;
    }
    const textInput = /** @type {HTMLInputElement} */ (h('input', { type: 'text', class: 'input-text-large', value: r.text ?? '', id: 'rv-text', spellcheck: 'false', autocomplete: 'off',
      'aria-label': 'Exact transcription', placeholder: 'Type exactly what is written' }));
    textInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const text = textInput.value;
        if (e.ctrlKey || e.metaKey) approve(text);
        else if (text !== (r.text ?? '')) patch(r, { text }, { text, text_origin: 'human' });
      } else if (e.key === 'Escape') { textInput.value = r.text ?? ''; textInput.blur(); }
    });
    textInput.addEventListener('blur', () => { const text = textInput.value; if (text !== (r.text ?? '') && current()?.id === r.id) patch(r, { text }, { text, text_origin: 'human' }); });
    const reason = /** @type {HTMLInputElement} */ (h('input', { type: 'text', placeholder: 'Optional reason (kept with the decision)', maxlength: 300, 'aria-label': 'Reason', value: r.review_reason ?? '' }));
    const isMark = r.kind === 'mark' || ['underline', 'strike_through', 'overwrite', 'insertion'].includes(r.cls);
    const proposals = h('div', { class: 'stack' });
    if (r.has_unapplied_proposal || r.flags.includes('disagreement')) loadProposals(r, proposals);

    mount(inspEl,
      h('div', { class: 'rv-insp__head' },
        h('div', { class: 'row' }, statusBadge(r.status), r.deferred ? h('span', { class: 'badge', text: 'deferred' }) : null,
          h('span', { class: 'badge badge--outline', text: r.kind }), r.cls !== 'handwriting' ? h('span', { class: 'badge badge--outline', text: CLASS_LABEL[r.cls] }) : null),
        h('p', { class: 'xs muted' }, `Revision ${r.revision} · text by ${r.text_origin ?? 'nobody yet'} · created by ${r.created_by}`)),
      h('figure', { class: 'rv-crop' }, h('img', { src: `/api/regions/${r.id}/crop?rev=${r.revision}`, alt: `Crop of ${r.kind}` }),
        h('figcaption', { class: 'xs muted', text: `${Math.round(r.w)} × ${Math.round(r.h)} px, cut from the full-resolution page` })),
      h('label', { class: 'field' }, h('span', {}, 'Transcription ', h('span', { class: 'faint', text: '— exactly as written, mistakes included' })), textInput,
        h('span', { class: 'xs muted' }, h('kbd', { text: 'Enter' }), ' save · ', h('kbd', { text: 'Ctrl' }), '+', h('kbd', { text: 'Enter' }), ' save and approve')),
      r.flags.length ? h('div', { class: 'callout callout--review' }, icon('flag'), h('div', { class: 'stack' },
        h('strong', { text: 'Needs a decision' }), h('div', { class: 'row' }, r.flags.map(flagBadge)),
        r.review_reason ? h('p', { class: 'small', text: r.review_reason }) : null,
        h('p', { class: 'xs muted', text: 'Approving resolves these flags; flagged examples are never exported.' }))) : null,
      h('div', { class: 'rv-actions' },
        h('button', { class: 'btn btn--primary', type: 'button', onClick: () => approve(textInput.value) }, icon('check'), 'Approve', h('kbd', { text: 'A' })),
        h('button', { class: 'btn', type: 'button', onClick: () => decide('excluded') }, icon('x'), 'Exclude', h('kbd', { text: 'X' })),
        h('button', { class: 'btn', type: 'button', onClick: defer }, icon('defer'), 'Defer', h('kbd', { text: 'D' })),
        h('button', { class: 'btn btn--quiet', type: 'button', onClick: undo, title: 'Undo the last change to this region' }, icon('undo'), 'Undo', h('kbd', { text: 'Ctrl Z' }))),
      reason,
      h('div', { class: 'rv-grid2' },
        h('label', { class: 'field' }, h('span', { text: 'Class' }), h('select', { onChange: (/** @type {Event} */ e) => { const cls = /** @type {HTMLSelectElement} */ (e.target).value; patch(r, { cls }, { cls }); } },
          CLASSES.map((c) => h('option', { value: c, selected: c === r.cls ? true : undefined }, CLASS_LABEL[c])))),
        h('label', { class: 'field' }, h('span', { text: 'Kind' }), h('select', { onChange: (/** @type {Event} */ e) => { const kind = /** @type {HTMLSelectElement} */ (e.target).value; patch(r, { kind }, { kind }); } },
          ['word', 'line', 'mark', 'block'].map((k) => h('option', { value: k, selected: k === r.kind ? true : undefined }, k))))),
      h('label', { class: 'check' }, h('input', { type: 'checkbox', checked: r.other_author ? true : undefined,
        onChange: (/** @type {Event} */ e) => { const v = /** @type {HTMLInputElement} */ (e.target).checked; patch(r, { other_author: v }, { other_author: v ? 1 : 0 }); } }),
        icon('person'), 'Someone else’s handwriting (never enters your dataset)'),
      h('div', { class: 'row' },
        h('button', { class: 'btn btn--sm', type: 'button', onClick: () => split(r) }, icon('split'), 'Split in half'),
        h('button', { class: 'btn btn--sm', type: 'button', disabled: state.multi.size < 2, title: 'Shift-click outlines to select several', onClick: merge }, icon('merge'), state.multi.size >= 2 ? `Merge ${state.multi.size}` : 'Merge (shift-click)'),
        h('button', { class: 'btn btn--sm btn--quiet btn--danger', type: 'button', onClick: () => remove(r) }, icon('trash'), 'Delete')),
      isMark ? markPanel(r) : null,
      proposals,
      historyPanel(r));
  }

  /** @param {Region} r @param {HTMLElement} el */
  async function loadProposals(r, el) {
    try {
      const props = await api(`/api/regions/${r.id}/proposals`);
      const open = props.filter((/** @type {any} */ p) => !p.applied && !p.stale);
      if (!open.length) return;
      mount(el, h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h3', { text: 'Machine proposal not applied' })),
        h('div', { class: 'panel__body stack' },
          h('p', { class: 'xs muted', text: 'Recognition ran again but your text was kept. Apply only if the proposal is right.' }),
          open.slice(0, 3).map((/** @type {any} */ p) => h('div', { class: 'rv-proposal' },
            h('div', { class: 'mono', text: p.payload.text ?? '(unreadable)' }),
            h('div', { class: 'xs muted', text: `${p.model_reported} · prompt ${p.prompt_version} · ${new Date(p.created_at).toLocaleString()}` }),
            h('button', { class: 'btn btn--sm', type: 'button', onClick: async () => {
              try { upsert(await api(`/api/regions/${r.id}/apply-proposal`, { body: { proposal_id: p.id, expected_revision: r.revision } })); setSave('saved'); drawInspector(); workspace.draw(); }
              catch (err) { setSave('error', err instanceof Error ? err.message : String(err)); }
            } }, 'Apply this proposal'))))));
    } catch { /* inspector still usable */ }
  }

  /** @param {Region} r */
  function markPanel(r) {
    const link = state.links.find((l) => l.kind === 'mark_target' && l.a_id === r.id);
    const targets = state.regions.filter((x) => (x.kind === 'word' || x.kind === 'line') && x.id !== r.id);
    const select = /** @type {HTMLSelectElement} */ (h('select', { 'aria-label': 'Affected word or line' },
      h('option', { value: '' }, 'Unknown target'),
      targets.map((t) => h('option', { value: t.id, selected: link && link.b_id === t.id ? true : undefined }, `${t.kind}: ${t.text ?? t.id}`))));
    const unknownOriginal = /** @type {HTMLInputElement} */ (h('input', { type: 'checkbox', checked: !link || link.unknown_original ? true : undefined }));
    const start = /** @type {HTMLInputElement} */ (h('input', { type: 'number', min: 0, placeholder: 'from', value: link?.span_start ?? '', class: 'input-narrow', 'aria-label': 'Span start (character)' }));
    const end = /** @type {HTMLInputElement} */ (h('input', { type: 'number', min: 1, placeholder: 'to', value: link?.span_end ?? '', class: 'input-narrow', 'aria-label': 'Span end (character)' }));
    return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h3', { text: 'Mark' }), h('span', { class: 'xs muted', text: 'kept out of the clean training set' })),
      h('div', { class: 'panel__body stack' },
        h('label', { class: 'field' }, h('span', { text: 'Affects' }), select),
        h('div', { class: 'row' }, h('span', { class: 'small muted', text: 'Characters' }), start, end),
        h('label', { class: 'check small' }, unknownOriginal, 'Original text under the mark is unknown'),
        h('button', { class: 'btn btn--sm', type: 'button', onClick: async () => {
          try {
            await api('/api/links/mark', { body: { mark_id: r.id, target_id: select.value || null, unknown_target: !select.value, unknown_original: unknownOriginal.checked,
              span: start.value && end.value ? [Number(start.value), Number(end.value)] : null } });
            setSave('saved'); reloadPage();
          } catch (err) { setSave('error', err instanceof Error ? err.message : String(err)); }
        } }, icon('link'), link ? 'Update link' : 'Save link')));
  }

  /** @param {Region} r */
  function historyPanel(r) {
    const body = h('div', { class: 'stack xs' });
    const det = h('details', { class: 'rv-history', onToggle: async () => {
      if (!(/** @type {HTMLDetailsElement} */ (det)).open) return;
      mount(body, loadingState(1));
      try {
        const items = await api(`/api/regions/${r.id}/history`);
        mount(body, h('ol', { class: 'rv-history__list' }, items.map((/** @type {any} */ i) => h('li', {},
          h('span', { class: 'mono', text: `r${i.revision}` }), ' ', h('span', { class: `badge${i.actor === 'human' ? ' badge--ok' : ''}`, text: i.actor }), ' ',
          h('span', { text: i.action.replaceAll('_', ' ') }), ' ', h('span', { class: 'faint', text: `“${i.data.text ?? ''}” · ${new Date(i.created_at).toLocaleString()}` })))));
      } catch (err) { mount(body, errorState(err)); }
    } }, h('summary', { text: 'History and provenance' }), body);
    return det;
  }

  // ---------- actions ----------
  /** @param {string} text */
  async function approve(text) {
    const r = current();
    if (!r) return;
    const body = { status: 'approved', ...(text !== (r.text ?? '') ? { text } : {}), reason: (/** @type {HTMLInputElement|null} */ (inspEl.querySelector('input[aria-label=Reason]')))?.value || undefined };
    const ok = await patch(r, body, { status: 'approved', text: text || r.text, flags: [], deferred: 0 });
    if (ok && state.filter.status !== 'approved') step(1);
  }

  /** @param {string} status */
  async function decide(status) {
    const r = current();
    if (!r) return;
    const reason = (/** @type {HTMLInputElement|null} */ (inspEl.querySelector('input[aria-label=Reason]')))?.value || undefined;
    const ok = await patch(r, { status, reason }, { status });
    if (ok && state.filter.status && state.filter.status !== status) step(1);
  }

  async function defer() {
    const r = current();
    if (!r) return;
    const ok = await patch(r, { deferred: !r.deferred }, { deferred: r.deferred ? 0 : 1, status: 'needs_review' });
    if (ok && !r.deferred) step(1);
  }

  async function undo() {
    const r = current();
    if (!r) return;
    setSave('saving');
    try { upsert(await api(`/api/regions/${r.id}/undo`, { body: { expected_revision: r.revision } })); setSave('saved'); }
    catch (err) { setSave('error', err instanceof Error ? err.message : String(err)); }
    workspace.draw(); drawQueue(); drawInspector();
  }

  /** @param {Region} r */
  async function split(r) {
    try {
      const parts = await api(`/api/regions/${r.id}/split`, { body: { at_x: r.x + r.w / 2 } });
      upsert({ ...r, deleted: 1 }); parts.forEach(upsert);
      setSave('saved'); select(parts[0].id);
      toast('Split in half. Drag the edges to fit each part.');
    } catch (err) { setSave('error', err instanceof Error ? err.message : String(err)); }
  }

  async function merge() {
    try {
      const ids = [...state.multi];
      const merged = await api('/api/regions/merge', { body: { ids } });
      ids.forEach((id) => { const x = state.regions.find((y) => y.id === id); if (x) upsert({ ...x, deleted: 1 }); });
      upsert(merged); state.multi.clear(); setSave('saved'); select(merged.id);
    } catch (err) { setSave('error', err instanceof Error ? err.message : String(err)); }
  }

  /** @param {Region} r */
  async function remove(r) {
    if (!confirm('Delete this region? Its history is kept and it can be recreated.')) return;
    try { await api(`/api/regions/${r.id}`, { method: 'DELETE' }); upsert({ ...r, deleted: 1 }); state.selected = null; setSave('saved'); workspace.draw(); drawInspector(); }
    catch (err) { setSave('error', err instanceof Error ? err.message : String(err)); }
  }

  function showHelp() {
    const dialog = /** @type {HTMLDialogElement} */ (h('dialog', { class: 'dialog', 'aria-labelledby': 'kb-title' }));
    const rows = [['J / K', 'Next / previous in queue'], ['A', 'Approve'], ['X', 'Exclude'], ['D', 'Defer'], ['E or Enter', 'Edit transcription'],
      ['Ctrl+Enter', 'Save text and approve'], ['Ctrl+Z', 'Undo last change to region'], ['N / V', 'Draw tool / select tool'], ['Shift+click', 'Select several (merge)'],
      ['+ / − / 0', 'Zoom in / out / fit'], ['Arrows', 'Pan'], ['O', 'Original / processed'], ['Esc', 'Leave text field'], ['?', 'This help']];
    mount(dialog, h('div', { class: 'dialog__head' }, h('h2', { id: 'kb-title', text: 'Keyboard shortcuts' }),
      h('button', { class: 'btn btn--quiet btn--sm', type: 'button', 'aria-label': 'Close', onClick: () => dialog.close() }, icon('x'))),
      h('p', { class: 'small muted', text: 'Shortcuts never fire while you are typing in a field.' }),
      h('table', { class: 'table' }, h('tbody', {}, rows.map(([k, v]) => h('tr', {}, h('td', {}, h('kbd', { text: k })), h('td', { text: v }))))));
    document.body.appendChild(dialog);
    dialog.addEventListener('close', () => dialog.remove());
    dialog.showModal();
  }

  /** @param {KeyboardEvent} e */
  function onKey(e) {
    if (isTyping(e.target) || document.querySelector('dialog[open]')) return;
    if (e.altKey) return;
    const k = e.key;
    if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === 'z') { e.preventDefault(); undo(); return; }
    if (e.ctrlKey || e.metaKey) return;
    /** @type {Record<string, () => void>} */
    const map = {
      j: () => step(1), k: () => step(-1), a: () => approve(current()?.text ?? ''), x: () => decide('excluded'), d: defer,
      e: () => /** @type {HTMLInputElement|null} */ (document.getElementById('rv-text'))?.focus(),
      Enter: () => /** @type {HTMLInputElement|null} */ (document.getElementById('rv-text'))?.focus(),
      n: () => workspace.setTool('draw'), v: () => workspace.setTool('select'), o: () => workspace.toggleOriginal(),
      '+': () => workspace.zoomCentre(1.25), '=': () => workspace.zoomCentre(1.25), '-': () => workspace.zoomCentre(0.8), '0': () => workspace.fit(),
      ArrowLeft: () => workspace.pan(60, 0), ArrowRight: () => workspace.pan(-60, 0), ArrowUp: () => workspace.pan(0, 60), ArrowDown: () => workspace.pan(0, -60),
      '?': showHelp,
    };
    const fn = map[k] || map[k.toLowerCase()];
    if (fn) { e.preventDefault(); fn(); }
  }

  document.addEventListener('keydown', onKey);
  drawInspector();
  await loadQueue();
  return () => { document.removeEventListener('keydown', onKey); workspace.destroy(); };
}
