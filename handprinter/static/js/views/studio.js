// Studio: compose handwriting images on a page in physical units, then export at an explicit resolution.
import { api, upload } from '../api.js';
import { h, isTyping, mount } from '../dom.js';
import { icon } from '../components/icons.js';
import { emptyState, errorState, loadingState } from '../components/states.js';
import { toast } from '../app.js';

const PRESETS = /** @type {Record<string, [number, number]>} */ ({ A4: [210, 297], A5: [148, 210], Letter: [215.9, 279.4] });
const HISTORY_LIMIT = 100;

/** @typedef {{id: string, asset_id: string, x_mm: number, y_mm: number, w_mm: number, h_mm: number, keep_aspect: boolean, blend: 'multiply'|'normal'}} El */
/** @typedef {{name: string, page_w_mm: number, page_h_mm: number, margins_mm: number[], elements: El[]}} Comp */

/** @param {HTMLElement} root */
export async function render(root) {
  /** @type {Comp} */
  let comp = { name: 'Untitled page', page_w_mm: 210, page_h_mm: 297, margins_mm: [20, 20, 20, 20], elements: [] };
  /** @type {string | null} */ let compId = null;
  /** @type {number | null} */ let revision = null;
  /** @type {string | null} */ let selected = null;
  /** @type {Comp[]} */ const undoStack = [];
  /** @type {Comp[]} */ const redoStack = [];
  /** @type {Map<string, any>} */ const assets = new Map();
  /** @type {Map<string, HTMLImageElement>} */ const images = new Map();
  let zoom = 0; // px per mm; 0 = fit
  let dirty = false;

  const library = h('aside', { class: 'st-lib', 'aria-label': 'Image library' });
  const canvasWrap = h('div', { class: 'st-canvas-wrap' });
  const canvas = /** @type {HTMLCanvasElement} */ (h('canvas', { class: 'st-canvas', tabindex: 0, role: 'img', 'aria-label': 'Page preview. Use the element list and fields for precise edits; arrow keys nudge the selected element.' }));
  canvasWrap.append(canvas);
  const props = h('aside', { class: 'st-props', 'aria-label': 'Page and element settings' });
  const toolbar = h('div', { class: 'st-toolbar', role: 'toolbar', 'aria-label': 'Studio tools' });
  root.classList.add('view--fill');
  mount(root,
    h('header', { class: 'page-head page-head--compact' },
      h('div', {}, h('h1', { text: 'Studio' }), h('p', { class: 'small muted', text: 'Place generated or imported handwriting on a page. Layout is stored in millimetres.' }))),
    h('div', { class: 'st' }, library, h('section', { class: 'st-center' }, toolbar, canvasWrap), props));

  // ---------- history ----------
  const snapshot = () => /** @type {Comp} */ (JSON.parse(JSON.stringify(comp)));
  function commit() {
    undoStack.push(snapshot());
    if (undoStack.length > HISTORY_LIMIT) undoStack.shift();
    redoStack.length = 0;
    dirty = true;
  }
  function undo() { if (!undoStack.length) return; redoStack.push(snapshot()); comp = /** @type {Comp} */ (undoStack.pop()); dirty = true; refresh(); }
  function redo() { if (!redoStack.length) return; undoStack.push(snapshot()); comp = /** @type {Comp} */ (redoStack.pop()); dirty = true; refresh(); }

  // ---------- library ----------
  async function loadLibrary() {
    mount(library, loadingState(3));
    try {
      const list = await api('/api/studio/assets');
      for (const a of list) assets.set(a.id, a);
      const fileInput = /** @type {HTMLInputElement} */ (h('input', { type: 'file', multiple: true, accept: 'image/png,image/jpeg,image/webp,.heic', class: 'sr-only', id: 'st-upload',
        onChange: async () => {
          for (const f of Array.from(fileInput.files || [])) {
            try { const a = await upload('/api/studio/assets', f, () => {}); assets.set(a.id, a); }
            catch (err) { toast(`${f.name}: ${err instanceof Error ? err.message : String(err)}`, 'error'); }
          }
          loadLibrary();
        } }));
      mount(library,
        h('div', { class: 'st-lib__head' }, h('h2', { text: 'Images' }),
          h('div', { class: 'row' },
            h('label', { class: 'btn btn--sm', for: 'st-upload' }, icon('upload'), 'Import images', fileInput),
            h('button', { class: 'btn btn--sm', type: 'button', onClick: pickApproved }, icon('plus'), 'Approved crops'),
            h('button', { class: 'btn btn--sm', type: 'button', onClick: pickGenerated }, icon('plus'), 'Generated'))),
        list.length
          ? h('ul', { class: 'st-assets', role: 'list' }, list.map((/** @type {any} */ a) => h('li', {},
              h('button', { class: 'st-asset', type: 'button', title: `Add “${a.label}” to the page`, onClick: () => addElement(a) },
                h('img', { src: `/api/studio/assets/${a.id}/image`, alt: '', loading: 'lazy' }),
                h('span', { class: 'xs', text: a.label }), h('span', { class: 'xs faint', text: a.kind === 'upload' ? 'imported' : a.kind })))))
          : emptyState({ title: 'No images yet', icon: 'studio', body: 'Import handwriting images, add approved crops from Review, or use generated output once a model is set up. No model is needed.' }));
    } catch (err) { mount(library, errorState(err, loadLibrary)); }
  }

  async function pickApproved() {
    const dialog = /** @type {HTMLDialogElement} */ (h('dialog', { class: 'dialog', 'aria-labelledby': 'pick-title' }));
    const list = h('div', { class: 'stack' }, loadingState(2));
    mount(dialog, h('div', { class: 'dialog__head' }, h('h2', { id: 'pick-title', text: 'Add approved crops' }),
      h('button', { class: 'btn btn--quiet btn--sm', type: 'button', 'aria-label': 'Close', onClick: () => dialog.close() }, icon('x'))), list);
    document.body.appendChild(dialog);
    dialog.addEventListener('close', () => { dialog.remove(); loadLibrary(); });
    dialog.showModal();
    const data = await api('/api/regions?status=approved&limit=60');
    mount(list, data.items.length ? h('ul', { class: 'st-pick', role: 'list' }, data.items.map((/** @type {any} */ r) => h('li', {},
      h('button', { class: 'st-asset', type: 'button', onClick: async (/** @type {Event} */ e) => {
        const btn = /** @type {HTMLButtonElement} */ (e.currentTarget);
        btn.disabled = true;
        const a = await api('/api/studio/assets/from-region', { body: { id: r.id } });
        assets.set(a.id, a); btn.classList.add('is-added');
      } }, h('img', { src: `/api/regions/${r.id}/crop`, alt: '' }), h('span', { class: 'xs', text: r.text || '' })))))
      : emptyState({ title: 'No approved crops yet', body: 'Approve words or lines in Review first.' }));
  }

  async function pickGenerated() {
    const gens = await api('/api/generations?limit=60');
    if (!gens.length) { toast('No generated images yet. Studio works with imported images and approved crops without any model.'); return; }
    for (const g of gens.slice(0, 20)) { const a = await api('/api/studio/assets/from-generation', { body: { id: g.id } }); assets.set(a.id, a); }
    loadLibrary();
  }

  /** @param {any} a */
  function addElement(a) {
    commit();
    const inkHeight = 10;
    const w = (a.width / a.height) * inkHeight;
    const [ml, mt] = [comp.margins_mm[3], comp.margins_mm[0]];
    const offset = comp.elements.length * 4;
    const el = { id: `e${Date.now().toString(36)}`, asset_id: a.id, x_mm: ml + (offset % 60), y_mm: mt + offset, w_mm: Math.min(w, comp.page_w_mm - ml - comp.margins_mm[1]), h_mm: inkHeight, keep_aspect: true, blend: /** @type {'multiply'} */ ('multiply') };
    if (el.w_mm < w) el.h_mm = el.w_mm * (a.height / a.width);
    comp.elements.push(el);
    selected = el.id;
    refresh();
  }

  // ---------- canvas ----------
  function scale() {
    if (zoom) return zoom;
    const r = canvasWrap.getBoundingClientRect();
    return Math.max(0.5, Math.min((r.width - 48) / comp.page_w_mm, (r.height - 48) / comp.page_h_mm));
  }
  function origin() {
    const r = canvasWrap.getBoundingClientRect(), s = scale();
    return { x: Math.max(24, (r.width - comp.page_w_mm * s) / 2), y: Math.max(24, (r.height - comp.page_h_mm * s) / 2) };
  }

  /** @param {string} id */
  function imageFor(id) {
    let img = images.get(id);
    if (!img) {
      img = new Image();
      img.onload = draw;
      img.src = `/api/studio/assets/${id}/image`;
      images.set(id, img);
    }
    return img;
  }

  function draw() {
    const s = scale(), o = origin(), dpr = window.devicePixelRatio || 1;
    const cw = Math.max(canvasWrap.clientWidth, comp.page_w_mm * s + 48), ch = Math.max(canvasWrap.clientHeight, comp.page_h_mm * s + 48);
    canvas.width = Math.round(cw * dpr); canvas.height = Math.round(ch * dpr);
    canvas.style.width = `${cw}px`; canvas.style.height = `${ch}px`;
    const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext('2d'));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cw, ch);
    ctx.fillStyle = '#ffffff';
    ctx.shadowColor = 'rgba(29,28,25,0.18)'; ctx.shadowBlur = 8;
    ctx.fillRect(o.x, o.y, comp.page_w_mm * s, comp.page_h_mm * s);
    ctx.shadowBlur = 0;
    const [mt, mr, mb, ml] = comp.margins_mm;
    ctx.setLineDash([4, 4]); ctx.strokeStyle = '#cfc8ba'; ctx.lineWidth = 1;
    ctx.strokeRect(o.x + ml * s, o.y + mt * s, (comp.page_w_mm - ml - mr) * s, (comp.page_h_mm - mt - mb) * s);
    ctx.setLineDash([]);
    for (const el of comp.elements) {
      const img = imageFor(el.asset_id);
      const x = o.x + el.x_mm * s, y = o.y + el.y_mm * s, w = el.w_mm * s, hh = el.h_mm * s;
      ctx.globalCompositeOperation = el.blend === 'multiply' ? 'multiply' : 'source-over';
      if (img.complete && img.naturalWidth) ctx.drawImage(img, x, y, w, hh);
      else { ctx.fillStyle = '#ece8df'; ctx.fillRect(x, y, w, hh); }
      ctx.globalCompositeOperation = 'source-over';
      if (el.id === selected) {
        ctx.strokeStyle = '#2f5b3a'; ctx.lineWidth = 1.5; ctx.strokeRect(x - 1, y - 1, w + 2, hh + 2);
        ctx.fillStyle = '#ffffff'; ctx.fillRect(x + w - 5, y + hh - 5, 10, 10); ctx.strokeRect(x + w - 5, y + hh - 5, 10, 10);
      }
    }
  }

  /** @type {{mode: 'move'|'resize', id: string, start: {x:number,y:number}, orig: El} | null} */
  let drag = null;
  canvas.addEventListener('pointerdown', (e) => {
    const r = canvas.getBoundingClientRect(), s = scale(), o = origin();
    const mx = (e.clientX - r.left - o.x) / s, my = (e.clientY - r.top - o.y) / s;
    const hit = [...comp.elements].reverse().find((el) => mx >= el.x_mm && mx <= el.x_mm + el.w_mm + 6 / s && my >= el.y_mm && my <= el.y_mm + el.h_mm + 6 / s);
    canvas.focus();
    if (!hit) { selected = null; refresh(); return; }
    selected = hit.id;
    const onHandle = Math.abs(mx - (hit.x_mm + hit.w_mm)) * s < 9 && Math.abs(my - (hit.y_mm + hit.h_mm)) * s < 9;
    commit();
    drag = { mode: onHandle ? 'resize' : 'move', id: hit.id, start: { x: mx, y: my }, orig: { ...hit } };
    canvas.setPointerCapture(e.pointerId);
    refresh();
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const r = canvas.getBoundingClientRect(), s = scale(), o = origin();
    const mx = (e.clientX - r.left - o.x) / s, my = (e.clientY - r.top - o.y) / s;
    const el = comp.elements.find((x) => x.id === drag?.id);
    if (!el) return;
    const dx = mx - drag.start.x, dy = my - drag.start.y;
    if (drag.mode === 'move') { el.x_mm = round(drag.orig.x_mm + dx); el.y_mm = round(drag.orig.y_mm + dy); }
    else {
      el.w_mm = Math.max(1, round(drag.orig.w_mm + dx));
      el.h_mm = el.keep_aspect ? round(el.w_mm * (drag.orig.h_mm / drag.orig.w_mm)) : Math.max(1, round(drag.orig.h_mm + dy));
    }
    draw();
  });
  canvas.addEventListener('pointerup', () => { if (drag) { drag = null; refresh(); } });
  canvas.addEventListener('keydown', (e) => {
    const el = comp.elements.find((x) => x.id === selected);
    if (!el) return;
    const step = e.shiftKey ? 10 : 1;
    const d = /** @type {Record<string, [number, number]>} */ ({ ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] })[e.key];
    if (d) { e.preventDefault(); commit(); el.x_mm = round(el.x_mm + d[0]); el.y_mm = round(el.y_mm + d[1]); refresh(); }
    if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); removeSelected(); }
  });

  const round = (/** @type {number} */ v) => Math.round(v * 10) / 10;
  function removeSelected() { if (!selected) return; commit(); comp.elements = comp.elements.filter((x) => x.id !== selected); selected = null; refresh(); }

  /** Flow elements left-to-right within the margins at a common ink height, wrapping lines. @param {number} inkH @param {number} gap @param {number} lineGap */
  function flow(inkH, gap, lineGap) {
    commit();
    const [mt, mr, , ml] = comp.margins_mm;
    const maxX = comp.page_w_mm - mr;
    let x = ml, y = mt;
    for (const el of comp.elements) {
      const a = assets.get(el.asset_id);
      const ratio = a ? a.width / a.height : el.w_mm / el.h_mm;
      el.h_mm = inkH; el.w_mm = round(inkH * ratio); el.keep_aspect = true;
      if (x > ml && x + el.w_mm > maxX) { x = ml; y += inkH + lineGap; }
      el.x_mm = round(x); el.y_mm = round(y);
      x += el.w_mm + gap;
    }
    refresh();
  }

  // ---------- properties ----------
  function drawProps() {
    const el = comp.elements.find((x) => x.id === selected);
    const num = (/** @type {string} */ label, /** @type {number} */ value, /** @type {(v: number) => void} */ set, step = 0.5) => h('label', { class: 'field' }, h('span', { text: label }),
      h('input', { type: 'number', step: String(step), value: String(value), onChange: (/** @type {Event} */ e) => { commit(); set(Number(/** @type {HTMLInputElement} */ (e.target).value)); refresh(); } }));
    const preset = Object.entries(PRESETS).find(([, [w, hh]]) => w === comp.page_w_mm && hh === comp.page_h_mm)?.[0] || 'Custom';
    const inkH = /** @type {HTMLInputElement} */ (h('input', { type: 'number', value: '8', step: '0.5', min: '2' }));
    const gap = /** @type {HTMLInputElement} */ (h('input', { type: 'number', value: '3', step: '0.5', min: '0' }));
    const lineGap = /** @type {HTMLInputElement} */ (h('input', { type: 'number', value: '6', step: '0.5', min: '0' }));
    mount(props,
      h('section', { class: 'stack' }, h('h2', { text: 'Page' }),
        h('label', { class: 'field' }, h('span', { text: 'Name' }), h('input', { type: 'text', value: comp.name, maxlength: 120, onChange: (/** @type {Event} */ e) => { commit(); comp.name = /** @type {HTMLInputElement} */ (e.target).value || 'Untitled page'; } })),
        h('label', { class: 'field' }, h('span', { text: 'Size' }), h('select', { onChange: (/** @type {Event} */ e) => {
          const v = /** @type {HTMLSelectElement} */ (e.target).value; if (!PRESETS[v]) return; commit(); [comp.page_w_mm, comp.page_h_mm] = PRESETS[v]; zoom = 0; refresh(); } },
          [...Object.keys(PRESETS), 'Custom'].map((k) => h('option', { value: k, selected: k === preset ? true : undefined }, k)))),
        h('div', { class: 'st-grid2' }, num('Width mm', comp.page_w_mm, (v) => { comp.page_w_mm = Math.max(20, v); }, 1), num('Height mm', comp.page_h_mm, (v) => { comp.page_h_mm = Math.max(20, v); }, 1)),
        h('div', { class: 'st-grid4' }, ['Top', 'Right', 'Bottom', 'Left'].map((l, i) => num(l, comp.margins_mm[i], (v) => { comp.margins_mm[i] = Math.max(0, v); }, 1)))),
      h('hr', { class: 'divider' }),
      el ? h('section', { class: 'stack' }, h('h2', { text: 'Selected image' }),
        h('p', { class: 'xs muted', text: assets.get(el.asset_id)?.label || el.asset_id }),
        h('div', { class: 'st-grid2' }, num('X mm', el.x_mm, (v) => { el.x_mm = v; }), num('Y mm', el.y_mm, (v) => { el.y_mm = v; }),
          num('Width mm', el.w_mm, (v) => { const r = el.h_mm / el.w_mm; el.w_mm = Math.max(1, v); if (el.keep_aspect) el.h_mm = round(el.w_mm * r); }),
          num('Height mm', el.h_mm, (v) => { const r = el.w_mm / el.h_mm; el.h_mm = Math.max(1, v); if (el.keep_aspect) el.w_mm = round(el.h_mm * r); })),
        h('label', { class: 'check small' }, h('input', { type: 'checkbox', checked: el.keep_aspect ? true : undefined, onChange: (/** @type {Event} */ e) => { commit(); el.keep_aspect = /** @type {HTMLInputElement} */ (e.target).checked; refresh(); } }), 'Keep aspect ratio'),
        h('label', { class: 'field' }, h('span', { text: 'Blending' }), h('select', { onChange: (/** @type {Event} */ e) => { commit(); el.blend = /** @type {any} */ (/** @type {HTMLSelectElement} */ (e.target).value); refresh(); } },
          h('option', { value: 'multiply', selected: el.blend === 'multiply' ? true : undefined }, 'Multiply (darkens only)'), h('option', { value: 'normal', selected: el.blend === 'normal' ? true : undefined }, 'Normal (opaque)'))),
        h('p', { class: 'xs muted', text: 'Background is not removed: multiply only darkens, so a crop’s paper tone can still show. No transparency extraction is claimed.' }),
        h('button', { class: 'btn btn--sm btn--danger', type: 'button', onClick: removeSelected }, icon('trash'), 'Remove from page'))
        : h('p', { class: 'small muted', text: 'Select an image on the page or in the list below to edit it.' }),
      h('hr', { class: 'divider' }),
      h('section', { class: 'stack' }, h('h2', { text: 'Flow into lines' }),
        h('p', { class: 'xs muted', text: 'Places all images in list order within the margins, at one ink height, wrapping at the right margin. Aspect ratios are kept.' }),
        h('div', { class: 'st-grid3' }, h('label', { class: 'field' }, h('span', { text: 'Height mm' }), inkH), h('label', { class: 'field' }, h('span', { text: 'Gap mm' }), gap), h('label', { class: 'field' }, h('span', { text: 'Line gap mm' }), lineGap)),
        h('button', { class: 'btn btn--sm', type: 'button', disabled: !comp.elements.length, onClick: () => flow(Number(inkH.value) || 8, Number(gap.value) || 0, Number(lineGap.value) || 0) }, icon('layout'), 'Flow')),
      h('hr', { class: 'divider' }),
      h('section', { class: 'stack' }, h('h2', { text: `On this page (${comp.elements.length})` }),
        comp.elements.length ? h('ol', { class: 'st-ellist' }, comp.elements.map((x) => h('li', {},
          h('button', { class: `btn btn--sm btn--quiet${x.id === selected ? ' is-on' : ''}`, type: 'button', 'aria-pressed': String(x.id === selected), onClick: () => { selected = x.id; refresh(); } },
            assets.get(x.asset_id)?.label || x.asset_id)))) : h('p', { class: 'xs muted', text: 'Click an image in the library to add it.' })));
  }

  async function drawToolbar() {
    const list = await api('/api/studio/compositions').catch(() => []);
    const dpi = /** @type {HTMLSelectElement} */ (h('select', { 'aria-label': 'Export resolution' }, [150, 300, 600].map((d) => h('option', { value: String(d), selected: d === 300 ? true : undefined }, `${d} dpi`))));
    const pxInfo = h('span', { class: 'xs muted' });
    const updatePx = () => { const d = Number(dpi.value); pxInfo.textContent = `${Math.round(comp.page_w_mm / 25.4 * d)} × ${Math.round(comp.page_h_mm / 25.4 * d)} px`; };
    dpi.addEventListener('change', updatePx); updatePx();
    mount(toolbar,
      h('button', { class: 'btn btn--sm', type: 'button', title: 'Undo (Ctrl+Z)', 'aria-label': 'Undo', disabled: !undoStack.length, onClick: undo }, icon('undo')),
      h('button', { class: 'btn btn--sm', type: 'button', title: 'Redo (Ctrl+Shift+Z)', 'aria-label': 'Redo', disabled: !redoStack.length, onClick: redo }, icon('redo')),
      h('span', { class: 'ws__sep' }),
      h('button', { class: 'btn btn--sm', type: 'button', 'aria-label': 'Zoom out', onClick: () => { zoom = scale() / 1.25; draw(); } }, icon('zoomOut')),
      h('button', { class: 'btn btn--sm', type: 'button', 'aria-label': 'Zoom in', onClick: () => { zoom = scale() * 1.25; draw(); } }, icon('zoomIn')),
      h('button', { class: 'btn btn--sm', type: 'button', 'aria-label': 'Fit page', onClick: () => { zoom = 0; draw(); } }, icon('fit')),
      h('span', { class: 'ws__sep' }),
      h('select', { class: 'st-open', 'aria-label': 'Open a saved page', onChange: (/** @type {Event} */ e) => open(/** @type {HTMLSelectElement} */ (e.target).value) },
        h('option', { value: '' }, list.length ? 'Open saved page…' : 'No saved pages'), list.map((/** @type {any} */ c) => h('option', { value: c.id, selected: c.id === compId ? true : undefined }, c.name))),
      h('button', { class: 'btn btn--sm', type: 'button', onClick: save }, icon('save'), dirty ? 'Save*' : 'Save'),
      h('span', { class: 'spacer' }),
      pxInfo, dpi,
      h('button', { class: 'btn btn--sm btn--primary', type: 'button', disabled: !comp.elements.length, onClick: () => exportPng(Number(dpi.value)) }, icon('download'), 'Export PNG'));
  }

  async function save() {
    try {
      const res = compId
        ? await api(`/api/studio/compositions/${compId}`, { method: 'PUT', body: { composition: comp, expected_revision: revision } })
        : await api('/api/studio/compositions', { body: { composition: comp } });
      compId = res.id; revision = res.revision; dirty = false;
      toast('Page saved'); drawToolbar();
    } catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
  }

  /** @param {string} id */
  async function open(id) {
    if (!id) return;
    if (dirty && !confirm('Discard unsaved changes to this page?')) { drawToolbar(); return; }
    const c = await api(`/api/studio/compositions/${id}`);
    comp = c.data; compId = c.id; revision = c.revision; selected = null; dirty = false;
    undoStack.length = 0; redoStack.length = 0; zoom = 0;
    refresh();
  }

  /** @param {number} dpi */
  async function exportPng(dpi) {
    try {
      const res = await api('/api/studio/render', { body: { composition: comp, dpi }, raw: true });
      const blob = await res.blob();
      const a = /** @type {HTMLAnchorElement} */ (h('a', { href: URL.createObjectURL(blob), download: `${comp.name.replace(/[^\w.-]+/g, '-') || 'page'}-${dpi}dpi.png` }));
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    } catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
  }

  function refresh() { draw(); drawProps(); drawToolbar(); }

  /** @param {KeyboardEvent} e */
  function onKey(e) {
    if (isTyping(e.target) || document.querySelector('dialog[open]')) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') { e.preventDefault(); e.shiftKey ? redo() : undo(); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); }
  }
  /** @param {BeforeUnloadEvent} e */
  const beforeUnload = (e) => { if (dirty) e.preventDefault(); };
  document.addEventListener('keydown', onKey);
  window.addEventListener('beforeunload', beforeUnload);
  const ro = new ResizeObserver(() => draw());
  ro.observe(canvasWrap);
  loadLibrary();
  refresh();
  api('/api/studio/compositions').then((list) => { if (list.length && !compId && !comp.elements.length) open(list[0].id); }).catch(() => {});
  return () => { document.removeEventListener('keydown', onKey); window.removeEventListener('beforeunload', beforeUnload); ro.disconnect(); };
}
