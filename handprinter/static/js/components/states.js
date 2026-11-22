// Shared empty / loading / error / setup-required states with equal polish.
import { h } from '../dom.js';
import { icon } from './icons.js';

/** @param {{title: string, body?: any, actions?: any[], icon?: string, center?: boolean}} o */
export function emptyState(o) {
  return h('section', { class: `state${o.center ? ' state--center' : ''}`, role: 'status' },
    icon(o.icon || 'info', { class: 'state__icon' }), h('h2', { text: o.title }),
    o.body ? h('div', { class: 'muted' }, o.body) : null, o.actions ? h('div', { class: 'row' }, o.actions) : null);
}

/** @param {{title: string, steps?: string[], body?: any, actions?: any[]}} o */
export function setupState(o) {
  return h('section', { class: 'state state--setup', role: 'status' },
    icon('alert', { class: 'state__icon' }), h('h2', { text: o.title }),
    o.body ? h('div', { class: 'muted' }, o.body) : null,
    o.steps ? h('ol', {}, o.steps.map((s) => h('li', {}, s))) : null,
    o.actions ? h('div', { class: 'row' }, o.actions) : null);
}

/** @param {unknown} err @param {() => void} [retry] */
export function errorState(err, retry) {
  const message = err instanceof Error ? err.message : String(err);
  return h('section', { class: 'state state--error', role: 'alert' },
    icon('alert', { class: 'state__icon' }), h('h2', { text: 'Something went wrong' }),
    h('p', { class: 'muted', text: message }),
    retry ? h('button', { class: 'btn', type: 'button', onClick: retry }, icon('refresh'), 'Try again') : null);
}

/** @param {number} [rows] */
export function loadingState(rows = 3) {
  return h('div', { class: 'stack', 'aria-busy': 'true', 'aria-label': 'Loading' },
    Array.from({ length: rows }, (_, i) => h('div', { class: 'skeleton', style: { height: `${i === 0 ? 28 : 64}px`, width: i === 0 ? '40%' : '100%' } })));
}

/** @param {string} message @param {'review'|'error'|'info'} [tone] */
export function callout(message, tone = 'info') {
  return h('div', { class: `callout${tone === 'info' ? '' : ` callout--${tone}`}`, role: tone === 'error' ? 'alert' : 'note' },
    icon(tone === 'info' ? 'info' : 'alert'), h('div', {}, message));
}

const STATUS_LABEL = /** @type {Record<string, string>} */ ({
  unprocessed: 'Unprocessed', proposed: 'Proposed', needs_review: 'Needs review', approved: 'Approved', excluded: 'Excluded',
});

/** Status badge: amber for review, never colour alone. @param {string} status */
export function statusBadge(status) {
  const tone = status === 'needs_review' ? ' badge--review' : status === 'approved' ? ' badge--ok' : '';
  return h('span', { class: `badge${tone}` }, status === 'needs_review' ? icon('flag') : status === 'approved' ? icon('check') : null, STATUS_LABEL[status] || status);
}

/** @param {string} flag */
export function flagBadge(flag) {
  return h('span', { class: 'badge badge--review' }, flag.replaceAll('_', ' '));
}
