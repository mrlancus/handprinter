// Job tray: polls active jobs (1 s while any run, 5 s otherwise), shows progress and cancel.
import { api } from './api.js';
import { h, mount } from './dom.js';
import { icon } from './components/icons.js';

const KIND_LABEL = /** @type {Record<string, string>} */ ({
  import_upload: 'Import', detect: 'Detect regions', process: 'Apply processing', build_recognition: 'Build recognition package',
  import_recognition: 'Import recognition results', headless_recognition: 'Run recogniser (headless)', export_snapshot: 'Export dataset', recut_line: 'Re-cut word boxes', apply_sheet: 'Label writing sheet', refill_sheets: 'Re-cut all sheet pages', haiku_cuts: 'Haiku places word boxes',
  adapter_preflight: 'Model preflight', adapter_download: 'Model download', adapter_infer: 'Generate', adapter_train: 'Train',
});
export const jobLabel = (/** @type {string} */ kind) => KIND_LABEL[kind] || kind;

/** @type {Map<string, (job: any) => void>} */
const waiters = new Map();

/** Resolve when a job reaches a terminal state. @param {string} jobId @returns {Promise<any>} */
export function waitForJob(jobId) {
  return new Promise((resolve) => waiters.set(jobId, resolve));
}

/** @param {() => void} onFinished */
export function startJobTray(onFinished) {
  const list = h('div', { class: 'jobtray__list' });
  const panel = h('section', { class: 'panel', 'aria-label': 'Background jobs' },
    h('div', { class: 'panel__head' }, h('h3', { text: 'Background jobs' }), h('span', { class: 'xs muted', text: 'Runs in the worker; safe to leave this page' })), list);
  const tray = h('div', { class: 'jobtray', hidden: true }, panel);
  document.body.appendChild(tray);
  /** @type {Set<string>} */
  let active = new Set();

  async function poll() {
    let delay = 5000;
    try {
      const jobs = await api('/api/jobs?active=true');
      const now = new Set(jobs.map((/** @type {any} */ j) => j.id));
      const finished = [...active].filter((id) => !now.has(id));
      for (const id of [...finished, ...[...waiters.keys()].filter((id) => !now.has(id))]) {
        const waiter = waiters.get(id);
        if (waiter) { waiters.delete(id); waiter(await api(`/api/jobs/${id}`)); }
      }
      if (finished.length) onFinished();
      active = now;
      tray.hidden = jobs.length === 0;
      mount(list, jobs.slice(0, 8).map((/** @type {any} */ j) => h('div', { class: 'job' },
        h('div', { class: 'row row--between' }, h('strong', { text: jobLabel(j.kind) }),
          h('button', { class: 'btn btn--quiet btn--sm', type: 'button', 'aria-label': `Cancel ${jobLabel(j.kind)}`, onClick: () => api(`/api/jobs/${j.id}/cancel`, { method: 'POST' }).then(poll) }, icon('x'), 'Cancel')),
        h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': Math.round(j.progress * 100) },
          h('span', { style: { width: `${Math.round(j.progress * 100)}%` } })),
        h('span', { class: 'xs muted', text: j.status === 'queued' ? 'Queued' : (j.message || 'Running') }))));
      if (jobs.length || waiters.size) delay = 1000;
    } catch { delay = 5000; }
    setTimeout(poll, delay);
  }
  poll();
  window.addEventListener('hp:job-started', () => { setTimeout(poll, 300); });
}

/** Announce a newly enqueued job so the tray polls promptly. @param {string} jobId */
export function trackJob(jobId) {
  window.dispatchEvent(new CustomEvent('hp:job-started', { detail: jobId }));
  return waitForJob(jobId);
}
