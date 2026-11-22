// App shell: hash router, navigation rail, job tray, toasts and offline banner.
import { api, onConnection } from './api.js';
import { h, mount } from './dom.js';
import { icon } from './components/icons.js';
import { errorState } from './components/states.js';
import { startJobTray } from './jobs.js';

/** @typedef {{render: (root: HTMLElement, params: URLSearchParams, path: string[]) => (void | (() => void) | Promise<void | (() => void)>)}} View */

const NAV = [
  { id: 'sources', label: 'Sources', icon: 'sources' },
  { id: 'review', label: 'Review', icon: 'review' },
  { id: 'dataset', label: 'Dataset', icon: 'dataset' },
  { id: 'experiments', label: 'Experiments', icon: 'experiments' },
  { id: 'studio', label: 'Studio', icon: 'studio' },
  { id: 'settings', label: 'Settings', icon: 'settings' },
];

/** @type {Record<string, () => Promise<View>>} */
const VIEWS = {
  sources: () => import('./views/sources.js'),
  review: () => import('./views/review.js'),
  dataset: () => import('./views/dataset.js'),
  experiments: () => import('./views/experiments.js'),
  studio: () => import('./views/studio.js'),
  settings: () => import('./views/settings.js'),
};

const root = /** @type {HTMLElement} */ (document.getElementById('app'));
const main = h('main', { class: 'main', id: 'main', tabindex: '-1' });
const offline = h('div', { class: 'offline', role: 'alert', hidden: true }, 'Server not reachable. Changes are not being saved; reconnecting…');
const toasts = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' });
const reviewCount = h('span', { class: 'rail__count', hidden: true });
const links = NAV.map((n) => h('a', { href: `#/${n.id}`, dataset: { nav: n.id } }, icon(n.icon), n.label, n.id === 'review' ? reviewCount : null));
const rail = h('nav', { class: 'rail', 'aria-label': 'Main' },
  h('div', { class: 'rail__brand', title: 'Handprinter' }, icon('brand', { label: 'Handprinter' })), links);
mount(root, h('div', { class: 'app' }, rail, h('div', { class: 'main-wrap' }, offline, main)), toasts);

onConnection((ok) => { offline.hidden = ok; });

/** @param {string} message @param {'info'|'error'} [tone] */
export function toast(message, tone = 'info') {
  const el = h('div', { class: `toast${tone === 'error' ? ' toast--error' : ''}` }, message);
  toasts.appendChild(el);
  setTimeout(() => el.remove(), tone === 'error' ? 7000 : 3500);
}

/** @type {(() => void) | void} */
let cleanup;
let renderSeq = 0;

async function route() {
  const hash = location.hash.replace(/^#\/?/, '') || 'sources';
  const [pathPart, query = ''] = hash.split('?');
  const path = pathPart.split('/').filter(Boolean);
  const viewId = VIEWS[path[0]] ? path[0] : 'sources';
  for (const a of links) {
    if (a.dataset.nav === viewId) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  if (typeof cleanup === 'function') cleanup();
  cleanup = undefined;
  const seq = ++renderSeq;
  const container = h('div', { class: 'view' });
  mount(main, container);
  try {
    const view = await VIEWS[viewId]();
    if (seq !== renderSeq) return;
    cleanup = await view.render(container, new URLSearchParams(query), path.slice(1));
  } catch (err) {
    if (seq === renderSeq) mount(container, errorState(err, route));
  }
  document.title = `${NAV.find((n) => n.id === viewId)?.label || ''} · Handprinter`;
}

async function refreshCounts() {
  try {
    const status = await api('/api/status');
    const n = status.counts.needs_review;
    reviewCount.hidden = !n;
    reviewCount.textContent = n > 999 ? '999+' : String(n);
    reviewCount.setAttribute('aria-label', `${n} need review`);
  } catch { /* offline banner covers this */ }
}

window.addEventListener('hashchange', route);
window.addEventListener('hp:changed', refreshCounts);
startJobTray(() => { refreshCounts(); window.dispatchEvent(new CustomEvent('hp:jobs-finished')); });
route();
refreshCounts();
