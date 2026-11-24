// Experiments: adapter status and capabilities, baseline runs, style profiles, blind A/B and generation.
import { api } from '../api.js';
import { fmt, h, mount } from '../dom.js';
import { icon } from '../components/icons.js';
import { callout, emptyState, errorState, loadingState, setupState } from '../components/states.js';
import { trackJob } from '../jobs.js';
import { toast } from '../app.js';

/** @param {HTMLElement} root */
export async function render(root) {
  const body = h('div', { class: 'page-body' });
  mount(root,
    h('header', { class: 'page-head' },
      h('div', {}, h('h1', { text: 'Experiments' }), h('p', { class: 'muted', text: 'DiffusionPen and Eruku, compared on the same prompts and the same snapshot; fine-tune DiffusionPen on your own words. Nothing large starts on its own.' }))),
    body);

  async function load() {
    mount(body, loadingState(3));
    try {
      const [adapters, runs, prompts, profiles, snaps, cmpWord, cmpLine, checkpoints, readiness] = await Promise.all([
        api('/api/experiments/adapters'), api('/api/experiments/runs'), api('/api/experiments/prompts'), api('/api/experiments/style-profiles'),
        api('/api/dataset/snapshots'), api('/api/experiments/comparison?level=word'), api('/api/experiments/comparison?level=line'),
        api('/api/experiments/checkpoints'), api('/api/experiments/readiness'),
      ]);
      const ready = adapters.filter((/** @type {any} */ a) => a.state === 'ready');
      mount(body,
        readinessPanel(readiness),
        h('div', { class: 'ex-adapters' }, adapters.map((/** @type {any} */ a) => adapterCard(a, load))),
        generatePanel(ready, profiles, prompts, checkpoints),
        trainPanel(ready.filter((/** @type {any} */ a) => a.capabilities.can_train_in_app), snaps, checkpoints, load, readiness.suggested_steps),
        h('div', { class: 'ex-grid' }, profilesPanel(profiles, snaps, load), promptsPanel(prompts)),
        comparisonPanel(cmpWord, cmpLine),
        runsPanel(runs));
    } catch (err) { mount(body, errorState(err, load)); }
  }
  load();
}

/**
 * Ready to train? Every step from approved words to a fine-tuned model, the next one highlighted.
 * @param {{steps: {key: string, title: string, done: boolean, detail: string, link: string | null, blocking: boolean}[], next: string | null, ready_to_train: boolean}} r
 */
function readinessPanel(r) {
  return h('section', { class: 'panel' },
    h('div', { class: 'panel__head' }, h('h2', { text: 'Ready to train?' }),
      r.ready_to_train ? h('span', { class: 'badge badge--ok' }, icon('check'), 'Ready') : h('span', { class: 'badge badge--review' }, 'Not yet')),
    h('ol', { class: 'panel__body ex-ready', role: 'list' }, r.steps.map((s) => h('li', { class: `ex-ready__step${s.key === r.next ? ' ex-ready__step--next' : ''}` },
      h('span', { class: s.done ? 'ex-ok' : 'ex-no', 'aria-hidden': 'true' }, icon(s.done ? 'check' : s.key === r.next ? 'right' : 'x')),
      h('div', { class: 'stack' },
        h('strong', {}, s.title, s.blocking ? '' : h('span', { class: 'xs muted', text: ' (recommended)' }), h('span', { class: 'sr-only', text: s.done ? ' done' : ' to do' })),
        h('span', { class: 'small', text: s.detail }),
        !s.done && s.link && !location.hash.startsWith(s.link) ? h('a', { class: 'xs', href: s.link }, 'Go there') : null)))),
    h('p', { class: 'panel__body xs muted' }, 'Step-by-step guide: docs/training.md in the Handprinter folder.'));
}

/** @param {any} a @param {() => void} reload */
function adapterCard(a, reload) {
  const caps = a.capabilities;
  const ready = a.state === 'ready';
  /** @param {string} action */
  const start = async (action) => {
    try {
      const { job_id } = await api(`/api/experiments/adapters/${a.name}/${action}`, { method: 'POST' });
      const job = await trackJob(job_id);
      toast(job.status === 'succeeded' ? `${a.title} ${action} finished` : `${a.title} ${action} failed: ${job.error}`, job.status === 'succeeded' ? 'info' : 'error');
      reload();
    } catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
  };
  const canDownload = a.checks.environment && a.checks.torch && (a.name === 'eruku' ? !a.checks.weights_cached : !a.checks.weights);
  return h('section', { class: 'panel ex-adapter' },
    h('div', { class: 'panel__head' },
      h('div', {}, h('h2', { text: a.title }), h('div', { class: 'row xs' }, Object.entries(a.upstream).map(([k, v]) => String(v).startsWith('http') ? h('a', { href: v, target: '_blank', rel: 'noopener noreferrer' }, k, icon('external')) : h('span', { class: 'muted', text: `${k}: ${v}` })))),
      ready ? h('span', { class: 'badge badge--ok' }, icon('check'), 'Ready') : h('span', { class: 'badge badge--review' }, icon('alert'), 'Blocked')),
    h('div', { class: 'panel__body stack' },
      h('ul', { class: 'ex-checks', role: 'list' }, Object.entries(a.checks).map(([k, ok]) => h('li', {}, h('span', { class: ok ? 'ex-ok' : 'ex-no', 'aria-hidden': 'true' }, icon(ok ? 'check' : 'x')), h('span', {}, k.replaceAll('_', ' ')), h('span', { class: 'sr-only', text: ok ? 'yes' : 'no' })))),
      a.next_step ? h('div', { class: 'callout callout--review' }, icon('right'), h('p', {}, h('strong', { text: 'Next step: ' }), a.next_step)) : null,
      a.blockers.length ? h('div', { class: 'stack' }, h('h4', { text: 'Blocked by' }),
        h('ul', { class: 'ex-blockers small' }, a.blockers.map((/** @type {string} */ b) => h('li', { text: b })))) : null,
      h('dl', { class: 'facts' },
        h('dt', { text: 'Levels' }), h('dd', { text: caps.levels.join(', ') }),
        h('dt', { text: 'Characters' }), h('dd', { text: caps.charset_note }),
        h('dt', { text: 'Style input' }), h('dd', { text: `${caps.needs_style_references} reference image(s) from a saved style profile` }),
        h('dt', { text: 'Training' }), h('dd', { text: caps.can_train_in_app ? 'available' : `not offered: ${caps.train_note}` })),
      h('div', { class: 'row' },
        canDownload ? h('button', { class: 'btn', type: 'button', onClick: () => { if (confirm(`Download ${a.title} weights (several GB) now?`)) start('download'); } }, icon('download'), 'Download weights…') : null,
        ready ? h('button', { class: 'btn', type: 'button', onClick: () => start('preflight') }, icon('play'), 'Run memory preflight') : null,
        !ready && !canDownload ? h('span', { class: 'xs muted', text: 'Actions appear once the blockers above are resolved. Run python -m handprinter doctor for details.' }) : null)));
}

/** @param {any[]} ready @param {any[]} profiles @param {any} prompts @param {any[]} checkpoints */
function generatePanel(ready, profiles, prompts, checkpoints) {
  if (!ready.length) {
    return h('section', { class: 'panel' }, setupState({
      title: 'Generation is not available yet',
      body: 'No model adapter is ready on this machine, so nothing can be generated or compared. Studio still works with imported images and approved crops.',
      steps: ['Export an approved snapshot in Dataset.', 'Set up an adapter environment (see the blockers above and docs/model-experiments.md).', 'Create a style profile from the snapshot.', 'Run the baseline on the fixed prompt set.'],
    }));
  }
  const adapter = /** @type {HTMLSelectElement} */ (h('select', {}, ready.map((a) => h('option', { value: a.name }, a.title))));
  const level = /** @type {HTMLSelectElement} */ (h('select', {}, h('option', { value: 'word' }, 'Words'), h('option', { value: 'line' }, 'Short lines')));
  const profile = /** @type {HTMLSelectElement} */ (h('select', {}, profiles.map((p) => h('option', { value: p.id }, p.name))));
  const text = /** @type {HTMLTextAreaElement} */ (h('textarea', { placeholder: 'One word or short line per line', rows: 3 }));
  const weights = /** @type {HTMLSelectElement} */ (h('select', {}));
  const fillWeights = () => mount(weights, h('option', { value: 'pretrained' }, 'Pretrained (not trained on you)'),
    checkpoints.filter((c) => c.adapter === adapter.value).map((c) => h('option', { value: c.run_id }, `${c.name} (${c.train_words} words)`)));
  fillWeights();
  adapter.addEventListener('change', fillWeights);
  const out = h('div', { class: 'stack' });
  async function run(/** @type {boolean} */ baseline) {
    const texts = baseline ? null : text.value.split('\n').map((s) => s.trim()).filter(Boolean);
    if (!baseline && !texts?.length) { mount(out, callout('Type some text first.', 'error')); return; }
    try {
      const { job_id } = await api('/api/experiments/runs', { body: { adapter: adapter.value, level: level.value, texts, style_profile_id: profile.value || null, kind: baseline ? 'baseline' : 'generation', checkpoint: weights.value || 'pretrained' } });
      mount(out, h('p', { class: 'small muted', text: 'Running in the worker (GPU lane, one job at a time)…' }));
      const job = await trackJob(job_id);
      if (job.status !== 'succeeded') { mount(out, callout(job.error || job.status, 'error')); return; }
      const skipped = Object.entries(job.result.skipped);
      mount(out, h('p', { text: `${job.result.generated} image(s) generated in run ${job.result.run_id}.` }),
        skipped.length ? callout(`Not generated: ${skipped.map(([t, r]) => `“${t}” (${/** @type {string[]} */ (r).join('; ')})`).join(', ')}`, 'review') : null,
        h('a', { class: 'btn btn--sm', href: '#/studio' }, 'Use in Studio'));
    } catch (err) { mount(out, callout(err instanceof Error ? err.message : String(err), 'error')); }
  }
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Generate' }), h('span', { class: 'xs muted', text: 'style comes from a saved profile (cached references, not learned weights)' })),
    h('div', { class: 'panel__body stack' },
      h('div', { class: 'row' }, h('label', { class: 'field' }, h('span', { text: 'Model' }), adapter), h('label', { class: 'field' }, h('span', { text: 'Level' }), level), h('label', { class: 'field' }, h('span', { text: 'Style profile' }), profile), h('label', { class: 'field' }, h('span', { text: 'Weights' }), weights)),
      h('label', { class: 'field' }, h('span', { text: 'Text' }), text),
      h('div', { class: 'row' },
        h('button', { class: 'btn btn--primary', type: 'button', onClick: () => run(false) }, icon('play'), 'Generate'),
        h('button', { class: 'btn', type: 'button', onClick: () => run(true) }, `Run baseline on the fixed prompt set (${prompts.prompts.word.length} words, ${prompts.prompts.line.length} lines × ${prompts.seeds.length} seeds)`)),
      out));
}

/** @param {any[]} trainable @param {any[]} snaps @param {any[]} checkpoints @param {() => void} reload @param {number} suggested */
function trainPanel(trainable, snaps, checkpoints, reload, suggested) {
  if (!trainable.length) return null;
  const adapter = /** @type {HTMLSelectElement} */ (h('select', {}, trainable.map((a) => h('option', { value: a.name }, a.title))));
  const snap = /** @type {HTMLSelectElement} */ (h('select', {}, snaps.map((s) => h('option', { value: s.id }, `${s.name} (${s.counts.words} words)`))));
  const name = /** @type {HTMLInputElement} */ (h('input', { type: 'text', value: 'my handwriting', maxlength: 80 }));
  const steps = /** @type {HTMLInputElement} */ (h('input', { type: 'number', value: String(suggested || 2000), min: '50', max: '50000', step: '50' }));
  const out = h('div', { class: 'stack' });
  async function start() {
    try {
      const { job_id, train_words } = await api(`/api/experiments/adapters/${adapter.value}/train`, { body: { snapshot_id: snap.value, name: name.value, steps: Number(steps.value) } });
      mount(out, h('p', { class: 'small muted', text: `Fine-tuning on ${train_words} training words in the worker. You can leave this page; progress shows in the job tray.` }));
      const job = await trackJob(job_id);
      if (job.status !== 'succeeded') { mount(out, callout(job.error || job.status, 'error')); return; }
      toast(`Fine-tune finished (final loss ${job.result.final_loss})`);
      reload();
    } catch (err) { mount(out, callout(err instanceof Error ? err.message : String(err), 'error')); }
  }
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Fine-tune on your handwriting' }),
    h('span', { class: 'xs muted', text: 'training-split words only; validation and test words stay unseen' })),
    h('div', { class: 'panel__body stack' },
      h('p', { class: 'xs muted', text: 'Teaches the model your letter shapes, pen weight and spacing. Needs at least 50 approved words; a few hundred is better. On an RTX 2060 expect roughly 10–30 minutes for 2000 steps (not yet measured on your PC).' }),
      checkpoints.length ? h('ul', { class: 'plain-list' }, checkpoints.map((c) => h('li', {}, h('strong', { text: c.name }), h('span', { class: 'xs muted', text: ` · ${c.adapter} · ${c.train_words} words · ${c.finished_at}` })))) : null,
      snaps.length ? h('div', { class: 'row' },
        h('label', { class: 'field' }, h('span', { text: 'Model' }), adapter), h('label', { class: 'field' }, h('span', { text: 'Snapshot' }), snap),
        h('label', { class: 'field' }, h('span', { text: 'Name' }), name), h('label', { class: 'field' }, h('span', { text: 'Steps' }), steps),
        h('button', { class: 'btn btn--primary', type: 'button', onClick: () => { if (confirm('Start fine-tuning now? It uses the GPU until it finishes.')) start(); } }, icon('play'), 'Fine-tune'))
        : h('p', { class: 'small muted' }, 'Export a snapshot in ', h('a', { href: '#/dataset', text: 'Dataset' }), ' first.'),
      out));
}

/** @param {any[]} profiles @param {any[]} snaps @param {() => void} reload */
function profilesPanel(profiles, snaps, reload) {
  const snap = /** @type {HTMLSelectElement} */ (h('select', {}, snaps.map((s) => h('option', { value: s.id }, `${s.name} (${s.counts.words} words)`))));
  const name = /** @type {HTMLInputElement} */ (h('input', { type: 'text', value: 'current paper style', maxlength: 80 }));
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Style profiles' })),
    h('div', { class: 'panel__body stack' },
      h('p', { class: 'xs muted', text: 'A profile stores which approved training crops are used as style references, so you never re-upload samples. It is not a trained model.' }),
      profiles.length ? h('ul', { class: 'plain-list' }, profiles.map((p) => h('li', {}, h('strong', { text: p.name }), h('span', { class: 'xs muted', text: ` · ${p.region_ids.length} references from ${p.snapshot_id}` })))) : null,
      snaps.length ? h('div', { class: 'row' }, h('label', { class: 'field' }, h('span', { text: 'From snapshot' }), snap), h('label', { class: 'field' }, h('span', { text: 'Name' }), name),
        h('button', { class: 'btn', type: 'button', onClick: async () => {
          try { await api('/api/experiments/style-profiles', { body: { snapshot_id: snap.value, name: name.value } }); toast('Style profile saved'); reload(); }
          catch (err) { toast(err instanceof Error ? err.message : String(err), 'error'); }
        } }, icon('plus'), 'Create'))
        : h('p', { class: 'small muted' }, 'Export a snapshot in ', h('a', { href: '#/dataset', text: 'Dataset' }), ' first.')));
}

/** @param {any} prompts */
function promptsPanel(prompts) {
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Fixed prompt set' }), h('span', { class: 'xs muted', text: `seeds ${prompts.seeds.join(', ')}` })),
    h('div', { class: 'panel__body stack' },
      h('h4', { text: 'Words' }), h('p', { class: 'mono small', text: prompts.prompts.word.join(' · ') }),
      h('h4', { text: 'Short lines' }), h('ul', { class: 'plain-list mono small' }, prompts.prompts.line.map((/** @type {string} */ l) => h('li', { text: l }))),
      h('p', { class: 'xs muted', text: 'Identical for both models. The same seed does not mean equivalent randomness across architectures.' })));
}

/** @param {any} word @param {any} line */
function comparisonPanel(word, line) {
  const any = word.comparable.length || line.comparable.length;
  const area = h('div', { class: 'stack' });
  /** @param {string} level */
  async function trial(level) {
    try {
      const t = await api('/api/experiments/trials', { body: { level } });
      /** @param {string} choice */
      const rate = async (choice) => {
        const r = await api(`/api/experiments/trials/${t.id}/rate`, { body: { choice } });
        mount(area, h('p', {}, `You chose ${choice}. Left was `, h('strong', { text: r.left.adapter }), ', right was ', h('strong', { text: r.right.adapter }), '.'),
          h('button', { class: 'btn', type: 'button', onClick: () => trial(level) }, 'Next pair'));
      };
      mount(area, h('p', {}, 'Prompt: ', h('strong', { class: 'mono', text: t.prompt_text })),
        h('div', { class: 'ex-ab' },
          h('figure', {}, h('img', { src: `/api/generations/${t.left}/image`, alt: 'Option A' }), h('figcaption', { text: 'A' })),
          h('figure', {}, h('img', { src: `/api/generations/${t.right}/image`, alt: 'Option B' }), h('figcaption', { text: 'B' }))),
        h('div', { class: 'row' }, h('button', { class: 'btn', type: 'button', onClick: () => rate('left') }, 'A looks more like me'),
          h('button', { class: 'btn', type: 'button', onClick: () => rate('right') }, 'B looks more like me'),
          h('button', { class: 'btn btn--quiet', type: 'button', onClick: () => rate('tie') }, 'Same'), h('button', { class: 'btn btn--quiet', type: 'button', onClick: () => rate('neither') }, 'Neither')));
    } catch (err) { mount(area, callout(err instanceof Error ? err.message : String(err), 'error')); }
  }
  const summary = word.summary.ratings;
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Blind comparison' }), h('span', { class: 'xs muted', text: 'model names are revealed after you choose' })),
    h('div', { class: 'panel__body stack' },
      any ? h('div', { class: 'row' }, word.comparable.length ? h('button', { class: 'btn btn--primary', type: 'button', onClick: () => trial('word') }, 'Compare words') : null,
        line.comparable.length ? h('button', { class: 'btn btn--primary', type: 'button', onClick: () => trial('line') }, 'Compare lines') : null)
        : emptyState({ title: 'Nothing to compare yet', icon: 'eye', body: 'A blind comparison needs outputs from both models for the same prompts. Run the baseline on each adapter first.' }),
      area,
      Object.keys(summary).length ? h('div', { class: 'stack' }, h('h4', { text: 'Preference counts' }),
        h('table', { class: 'table' }, h('tbody', {}, Object.entries(summary).flatMap(([level, counts]) => Object.entries(/** @type {Record<string, number>} */ (counts)).map(([k, n]) => h('tr', {}, h('td', { text: level }), h('td', { text: k }), h('td', { class: 'num', text: fmt(n) })))))),
        h('p', { class: 'xs muted', text: word.summary.note })) : null,
      Object.keys(word.excluded).length ? h('p', { class: 'xs muted', text: `Excluded from comparison (only one model produced them): ${Object.keys(word.excluded).join(', ')}` }) : null));
}

/** @param {any[]} runs */
function runsPanel(runs) {
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Run log' }), h('span', { class: 'xs muted', text: 'every run records snapshot, seed, checkpoint, code version, configuration, environment and duration' })),
    runs.length ? h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ['Run', 'Model', 'Kind', 'Status', 'Snapshot', 'Seeds', 'Duration', 'Peak memory', 'Code'].map((t) => h('th', { text: t })))),
      h('tbody', {}, runs.map((r) => h('tr', {},
        h('td', { class: 'mono xs', text: r.id }), h('td', { text: r.adapter }), h('td', { text: r.kind }),
        h('td', {}, h('span', { class: `badge${r.status === 'failed' ? ' badge--error' : r.status === 'succeeded' ? ' badge--ok' : ''}`, text: r.status }), r.error ? h('div', { class: 'xs', text: r.error }) : null),
        h('td', { class: 'mono xs', text: r.snapshot_id || '—' }), h('td', { class: 'xs', text: (r.config.seeds || []).join(', ') }),
        h('td', { class: 'num', text: r.duration_s ? `${r.duration_s.toFixed(1)} s` : '—' }),
        h('td', { class: 'num', text: r.peak_memory_mb ? `${fmt(r.peak_memory_mb)} MB` : 'not measured' }),
        h('td', { class: 'mono xs', text: r.code_version }))))) : h('p', { class: 'panel__body small muted', text: 'No runs yet. Runs appear here with their full configuration once an adapter is ready.' }));
}
