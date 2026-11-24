// Dataset: real approved coverage, what blocks export, and immutable snapshots.
import { api } from '../api.js';
import { fmt, h, mount } from '../dom.js';
import { icon } from '../components/icons.js';
import { callout, emptyState, errorState, loadingState } from '../components/states.js';
import { trackJob } from '../jobs.js';
import { toast } from '../app.js';

const STATUS_ORDER = ['approved', 'needs_review', 'proposed', 'unprocessed', 'excluded'];
const STATUS_LABEL = /** @type {Record<string,string>} */ ({ approved: 'Approved', needs_review: 'Needs review', proposed: 'Proposed (unreviewed)', unprocessed: 'Unprocessed', excluded: 'Excluded' });

/** @param {HTMLElement} root */
export async function render(root) {
  const body = h('div', { class: 'page-body' });
  mount(root,
    h('header', { class: 'page-head' },
      h('div', {}, h('h1', { text: 'Dataset' }), h('p', { class: 'muted', text: 'Counts reflect human-approved, unflagged examples. They drive decisions; they are not a score.' }))),
    body);

  async function load() {
    mount(body, loadingState(3));
    try {
      const [cov, snaps] = await Promise.all([api('/api/dataset/coverage'), api('/api/dataset/snapshots')]);
      if (!cov.pages) {
        mount(body, emptyState({ title: 'No data yet', icon: 'dataset', body: h('p', {}, 'Import pages in ', h('a', { href: '#/sources', text: 'Sources' }), ' and approve examples in Review. Exports appear here.') }));
        return;
      }
      mount(body, h('div', { class: 'ds-grid' }, coverage(cov), snapshotForm(cov, load)), snapshots(snaps));
    } catch (err) { mount(body, errorState(err, load)); }
  }
  load();
}

/** @param {any} cov */
function coverage(cov) {
  const total = STATUS_ORDER.reduce((n, s) => n + (cov.status[s] || 0), 0);
  const words = cov.exportable.word || 0;
  const [lo, hi] = cov.pilot_target_words;
  const chars = Object.entries(cov.characters);
  return h('section', { class: 'stack stack--lg' },
    h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Exportable now' }), h('span', { class: 'xs muted', text: 'approved · unflagged · your handwriting · no marks' })),
      h('div', { class: 'panel__body stack' },
        h('div', { class: 'ds-kpis' },
          kpi('Words', fmt(words)), kpi('Lines', fmt(cov.exportable.line || 0)), kpi('Maths (LaTeX)', fmt(cov.exportable.maths || 0)), kpi('Distinct words', fmt(cov.word_types)), kpi('Pages', fmt(cov.pages))),
        h('div', { class: 'stack' },
          h('div', { class: 'row row--between small' }, h('span', { text: `Pilot target: ${fmt(lo)}–${fmt(hi)} labelled words` }), h('span', { class: 'muted', text: `${Math.min(100, Math.round((words / lo) * 100))}% of the lower bound` })),
          h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': 0, 'aria-valuemax': lo, 'aria-valuenow': words, 'aria-label': 'Words toward the pilot target' },
            h('span', { style: { width: `${Math.min(100, (words / lo) * 100)}%` } })),
          h('p', { class: 'xs muted', text: 'A planning target, not a quality threshold.' })))),
    h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Words and lines by status' }),
        h('a', { class: 'btn btn--sm', href: '#/review?status=needs_review' }, icon('review'), 'Open review queue')),
      h('table', { class: 'table' }, h('tbody', {}, STATUS_ORDER.map((s) => h('tr', {},
        h('td', {}, s === 'needs_review' ? h('span', { class: 'badge badge--review' }, icon('flag'), STATUS_LABEL[s]) : STATUS_LABEL[s]),
        h('td', { class: 'num', text: fmt(cov.status[s] || 0) }),
        h('td', { class: 'ds-bar' }, h('div', { class: `progress${s === 'needs_review' ? ' progress--review' : s === 'approved' ? '' : ' progress--neutral'}` }, h('span', { style: { width: `${total ? ((cov.status[s] || 0) / total) * 100 : 0}%` } })))))))),
    h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Why examples are not exportable' })),
      Object.keys(cov.excluded_reasons).length
        ? h('table', { class: 'table' }, h('tbody', {}, Object.entries(cov.excluded_reasons).map(([reason, n]) => h('tr', {}, h('td', { text: reason }), h('td', { class: 'num', text: fmt(Number(n)) })))))
        : h('p', { class: 'panel__body small muted', text: 'Nothing is held back.' }),
      cov.flagged_open ? h('div', { class: 'panel__body' }, callout(`${fmt(cov.flagged_open)} region(s) carry unresolved flags. Flagged examples are always excluded until a human resolves them.`, 'review')) : null),
    h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Character coverage (approved words)' }), h('span', { class: 'xs muted', text: `${chars.length} distinct characters` })),
      h('div', { class: 'panel__body' }, chars.length
        ? h('ul', { class: 'ds-chars', role: 'list' }, chars.map(([ch, n]) => h('li', { title: `${JSON.stringify(ch)}: ${n}` }, h('span', { class: 'ds-chars__ch mono', text: ch === ' ' ? '␠' : ch }), h('span', { class: 'xs muted', text: fmt(Number(n)) }))))
        : h('p', { class: 'small muted', text: 'No approved words yet.' }))),
    Object.keys(cov.marks).length ? h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Marks captured (not in the clean set)' })),
      h('table', { class: 'table' }, h('tbody', {}, Object.entries(cov.marks).map(([cls, n]) => h('tr', {}, h('td', { text: cls.replaceAll('_', ' ') }), h('td', { class: 'num', text: fmt(Number(n)) })))))) : null);
}

/** @param {string} label @param {string} value */
function kpi(label, value) {
  return h('div', { class: 'ds-kpi' }, h('span', { class: 'ds-kpi__value', text: value }), h('span', { class: 'xs muted', text: label }));
}

/** @param {any} cov @param {() => void} onDone */
function snapshotForm(cov, onDone) {
  const name = /** @type {HTMLInputElement} */ (h('input', { type: 'text', value: `snapshot ${new Date().toISOString().slice(0, 10)}`, maxlength: 80, required: true }));
  const seed = /** @type {HTMLInputElement} */ (h('input', { type: 'number', value: '1234', class: 'input-narrow' }));
  const unseen = /** @type {HTMLInputElement} */ (h('input', { type: 'number', value: '10', min: '0', max: '50', class: 'input-narrow' }));
  const holdout = /** @type {HTMLInputElement} */ (h('input', { type: 'text', placeholder: 'session names, comma separated' }));
  // Start with the kinds of page you actually have (iPad-only collections would otherwise export nothing).
  const byType = cov.pages_by_type || {};
  const paper = /** @type {HTMLInputElement} */ (h('input', { type: 'checkbox', checked: !!byType.paper || !byType.ipad }));
  const ipad = /** @type {HTMLInputElement} */ (h('input', { type: 'checkbox', checked: !!byType.ipad }));
  const current = /** @type {HTMLInputElement} */ (h('input', { type: 'checkbox', checked: true }));
  const older = /** @type {HTMLInputElement} */ (h('input', { type: 'checkbox' }));
  const advanced = /** @type {HTMLInputElement} */ (h('input', { type: 'checkbox' }));
  const warn = callout('Includes unflagged machine proposals nobody has reviewed. Labelled “unreviewed_machine_proposal” in the export; never “verified”.', 'review');
  warn.hidden = true;
  advanced.addEventListener('change', () => { warn.hidden = !advanced.checked; });
  const status = h('div', { role: 'status' });
  const maths = /** @type {HTMLInputElement} */ (h('input', { type: 'checkbox', checked: true }));

  async function submit(/** @type {Event} */ e) {
    e.preventDefault();
    const source_types = [paper.checked && 'paper', ipad.checked && 'ipad'].filter(Boolean);
    const style_eras = [current.checked && 'current', older.checked && 'older'].filter(Boolean);
    if (!source_types.length || !style_eras.length) { mount(status, callout('Pick at least one source type and one style era.', 'error')); return; }
    mount(status, h('p', { class: 'small muted', text: 'Exporting in the worker…' }));
    try {
      const { job_id } = await api('/api/dataset/snapshots', { body: {
        name: name.value, policy: advanced.checked ? 'include_unreviewed' : 'approved_only', source_types, style_eras, seed: Number(seed.value),
        unseen_word_fraction: Math.max(0, Math.min(50, Number(unseen.value))) / 100,
        holdout_sessions: holdout.value.split(',').map((s) => s.trim()).filter(Boolean), include_maths: maths.checked,
      } });
      const job = await trackJob(job_id);
      if (job.status !== 'succeeded') { mount(status, callout(job.error || job.status, 'error')); return; }
      toast(`Snapshot exported: ${job.result.counts.words} words, ${job.result.counts.lines} lines, ${job.result.counts.maths || 0} maths`);
      onDone();
    } catch (err) { mount(status, callout(err instanceof Error ? err.message : String(err), 'error')); }
  }

  return h('form', { class: 'panel ds-form', onSubmit: submit },
    h('div', { class: 'panel__head' }, h('h2', { text: 'New snapshot' })),
    h('div', { class: 'panel__body stack' },
      h('label', { class: 'field' }, h('span', { text: 'Name' }), name),
      h('fieldset', { class: 'ds-fieldset' }, h('legend', { class: 'label', text: 'Include' }),
        h('div', { class: 'row' }, h('label', { class: 'check' }, paper, 'Paper'), h('label', { class: 'check' }, ipad, 'iPad')),
        h('div', { class: 'row' }, h('label', { class: 'check' }, current, 'Current style'), h('label', { class: 'check' }, older, 'Older style')),
        h('label', { class: 'check' }, maths, 'Maths as LaTeX (separate maths.jsonl; never mixed into words)')),
      h('div', { class: 'row' }, h('label', { class: 'field' }, h('span', { text: 'Split seed' }), seed), h('label', { class: 'field' }, h('span', { text: 'Unseen words %' }), unseen)),
      h('label', { class: 'field' }, h('span', { text: 'Hold out sessions (test only)' }), holdout),
      h('p', { class: 'xs muted', text: '≈80/10/10 train/validation/test, grouped by page, source group and near-duplicates. Words and lines of the same page never cross a split.' }),
      h('details', {}, h('summary', { class: 'small', text: 'Advanced export policy' }),
        h('div', { class: 'stack ds-adv' }, h('label', { class: 'check small' }, advanced, 'Also include unreviewed, unflagged machine proposals'), warn)),
      h('button', { class: 'btn btn--primary', type: 'submit', disabled: !(cov.exportable.word || cov.exportable.line || cov.exportable.maths) && !cov.status.proposed ? true : undefined }, icon('download'), 'Export snapshot'),
      !(cov.exportable.word || cov.exportable.line || cov.exportable.maths) ? h('p', { class: 'xs muted', text: 'Approve examples in Review before exporting.' }) : null,
      status));
}

/** @param {any[]} snaps */
function snapshots(snaps) {
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Snapshots' }), h('span', { class: 'xs muted', text: 'immutable; experiments reference them by ID' })),
    snaps.length ? h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ['Snapshot', 'Policy', 'Words', 'Lines', 'Maths', 'Train / val / test', 'Unseen', 'Warnings', 'Folder'].map((t) => h('th', { text: t })))),
      h('tbody', {}, snaps.map((s) => h('tr', {},
        h('td', {}, h('strong', { text: s.name }), h('div', { class: 'xs muted mono', text: s.id }), h('div', { class: 'xs muted', text: new Date(s.created_at).toLocaleString() })),
        h('td', {}, s.policy === 'approved_only' ? h('span', { class: 'badge badge--ok', text: 'approved only' }) : h('span', { class: 'badge badge--review', text: 'includes unreviewed' })),
        h('td', { class: 'num', text: fmt(s.counts.words) }), h('td', { class: 'num', text: fmt(s.counts.lines) }), h('td', { class: 'num', text: fmt(s.counts.maths || 0) }),
        h('td', { class: 'mono xs', text: ['train', 'val', 'test'].map((k) => s.counts.splits[k] || 0).join(' / ') }),
        h('td', { class: 'num', text: `${fmt(s.counts.unseen_words)} (${s.counts.unseen_vocabulary} types)` }),
        h('td', {}, s.warnings.length ? s.warnings.map((/** @type {string} */ w) => h('div', { class: 'badge badge--review', text: w })) : h('span', { class: 'faint', text: 'none' })),
        h('td', { class: 'mono xs ds-path', text: s.path })))))
    : h('p', { class: 'panel__body small muted', text: 'No snapshots yet.' }));
}
