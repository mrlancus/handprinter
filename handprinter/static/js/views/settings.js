// Settings: what this machine reports (doctor), configuration, and setup guidance.
import { api } from '../api.js';
import { h, mount } from '../dom.js';
import { icon } from '../components/icons.js';
import { errorState, loadingState } from '../components/states.js';

/** @param {HTMLElement} root */
export async function render(root) {
  const body = h('div', { class: 'page-body' });
  mount(root,
    h('header', { class: 'page-head' },
      h('div', {}, h('h1', { text: 'Settings' }), h('p', { class: 'muted', text: 'What this machine reports, and how each optional part is set up. Values are configured in .env.' })),
      h('button', { class: 'btn', type: 'button', onClick: () => load(true) }, icon('refresh'), 'Re-check machine')),
    body);

  /** @param {boolean} [refresh] */
  async function load(refresh = false) {
    mount(body, loadingState(4));
    try {
      const d = await api(`/api/doctor${refresh ? '?refresh=true' : ''}`);
      mount(body, h('div', { class: 'set-grid' },
        appearance(), machine(d), recognition(d), adapters(d), storage(d)));
    } catch (err) { mount(body, errorState(err, () => load(true))); }
  }
  load();
}

/** Light / dark / follow the device. Stored per device (this browser only). */
function appearance() {
  let current = 'auto';
  try { current = localStorage.getItem('hp-theme') || 'auto'; } catch { /* storage blocked */ }
  /** @param {string} v */
  const set = (v) => {
    try { v === 'auto' ? localStorage.removeItem('hp-theme') : localStorage.setItem('hp-theme', v); } catch { /* storage blocked */ }
    if (v === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', v);
  };
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Appearance' })),
    h('div', { class: 'panel__body stack' },
      h('div', { class: 'segmented', role: 'radiogroup', 'aria-label': 'Theme' },
        [['auto', 'Match device'], ['light', 'Light'], ['dark', 'Dark']].map(([v, label]) =>
          h('label', {}, h('input', { type: 'radio', name: 'theme', value: v, checked: v === current ? true : undefined, onChange: () => set(v) }), h('span', { text: label })))),
      h('p', { class: 'xs muted', text: 'Saved on this device. Page photos are never recoloured.' })));
}

/** @param {boolean} ok @param {string} label @param {string} [detail] */
function line(ok, label, detail) {
  return h('li', { class: 'set-line' }, h('span', { class: ok ? 'ex-ok' : 'ex-no', 'aria-hidden': 'true' }, icon(ok ? 'check' : 'x')),
    h('span', {}, h('span', { text: label }), h('span', { class: 'sr-only', text: ok ? ': yes' : ': no' }), detail ? h('span', { class: 'xs muted', text: ` — ${detail}` }) : null));
}

/** @param {any} d */
function machine(d) {
  const smi = d.gpu.nvidia_smi;
  const gpus = Array.isArray(smi) ? smi : [];
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'This machine' })),
    h('div', { class: 'panel__body stack' },
      h('dl', { class: 'facts' },
        h('dt', { text: 'OS' }), h('dd', { text: d.os }),
        h('dt', { text: 'Python' }), h('dd', { text: `${d.python}${d.python_ok ? '' : ' (needs 3.11+)'}` }),
        h('dt', { text: 'GPU' }), h('dd', { text: gpus.length ? gpus.map((g) => `${g.name}, ${g.vram_total_mb} MB VRAM (driver ${g.driver})`).join('; ') : `none reported (nvidia-smi ${smi === null ? 'not found' : smi})` }),
        h('dt', { text: 'torch.cuda' }), h('dd', { text: typeof d.gpu.torch_cuda === 'string' ? d.gpu.torch_cuda : `${d.gpu.torch_cuda.name}, ${d.gpu.torch_cuda.vram_total_mb} MB` })),
      h('ul', { class: 'plain-list' },
        line(d.decoders.heic, 'HEIC/HEIF decoder', d.decoders.heic ? 'installed' : 'optional: pip install -e ".[heic]"'),
        line(d.decoders.pdf, 'PDF page renderer', d.decoders.pdf ? 'installed' : 'optional: pip install -e ".[pdf]"')),
      h('p', { class: 'xs muted' }, 'Same report on the command line: ', h('code', { text: 'python -m handprinter doctor' }))));
}

/** @param {any} d */
function recognition(d) {
  const r = d.recognition, cli = d.claude_cli;
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Recognition (Claude Code)' })),
    h('div', { class: 'panel__body stack' },
      h('ul', { class: 'plain-list' },
        line(cli.installed, 'claude CLI installed', cli.installed ? cli.path : 'install Claude Code to run /recognise'),
        line(cli.logged_in, 'claude CLI logged in', cli.logged_in ? 'uses your Claude plan’s usage limits' : 'run: claude auth login'),
        line(r.consistent, `Model pinned to ${r.configured_model}`, r.consistent ? 'matches the subagent file' : `subagent file says ${r.subagent_model}; align them`),
        line(r.headless_enabled, 'Headless automation (claude -p)', r.headless_enabled ? 'enabled' : 'off by default; HANDPRINTER_HEADLESS_RECOGNITION=1')),
      h('ol', { class: 'steps small' },
        h('li', {}, 'In Sources, select pages and choose “Prepare recognition…”. You will see exactly which images will be sent.'),
        h('li', {}, 'Open Claude Code in this repository and run ', h('code', { text: '/recognise' }), '.'),
        h('li', {}, 'Import the results under Sources → Recognition packages. Results are proposals; Review decides.')),
      h('p', { class: 'xs muted', text: 'No API key or SDK is used. Recognition never starts by itself.' })));
}

/** @param {any} d */
function adapters(d) {
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Model adapters' }), h('a', { class: 'btn btn--sm', href: '#/experiments' }, 'Experiments')),
    h('div', { class: 'panel__body stack' },
      Object.values(d.adapters).map((/** @type {any} */ a) => h('div', { class: 'stack set-adapter' },
        h('div', { class: 'row' }, h('strong', { text: a.title }), a.state === 'ready' ? h('span', { class: 'badge badge--ok', text: 'ready' }) : h('span', { class: 'badge badge--review', text: 'blocked' })),
        a.blockers.length ? h('p', { class: 'small muted', text: `${a.blockers.length} blocker${a.blockers.length > 1 ? 's' : ''}; details and next step in Experiments.` }) : null)),
      h('p', { class: 'xs muted' }, 'Each model runs in its own Python environment (PyTorch is never installed into the app). See ', h('code', { text: 'docs/model-experiments.md' }), '.')));
}

/** @param {any} d */
function storage(d) {
  return h('section', { class: 'panel' }, h('div', { class: 'panel__head' }, h('h2', { text: 'Data and access' })),
    h('div', { class: 'panel__body stack' },
      h('dl', { class: 'facts' },
        h('dt', { text: 'Data directory' }), h('dd', { class: 'mono xs', text: d.data_dir }),
        h('dt', { text: 'Free space' }), h('dd', { text: `${d.disk_free_gb} GB` }),
        h('dt', { text: 'Network' }), h('dd', { text: d.network.online ? 'Online through a Cloudflare Tunnel (password required)' : d.network.lan_mode ? `LAN mode on ${d.network.host} (token required)` : 'This computer only (127.0.0.1)' }),
        d.network.online ? h('dt', { text: 'Web address' }) : null,
        d.network.online ? h('dd', {}, d.network.public_url ? h('a', { href: d.network.public_url, class: 'mono xs', text: d.network.public_url }) : h('span', { class: 'muted', text: 'starting…' })) : null),
      d.network.online || d.network.lan_mode ? h('button', { class: 'btn btn--sm', type: 'button', onClick: async () => { await api('/api/logout', { body: {} }); location.href = '/login'; } }, 'Sign out on this device') : null,
      d.network.online ? null : h('p', { class: 'xs muted' }, 'To use it from an iPad or phone anywhere, start it with ', h('code', { text: 'scripts\\online.bat' }), '. See ', h('code', { text: 'docs/online.md' }), '.'),
      h('h4', { text: 'Backup' }),
      h('p', { class: 'small' }, 'Stop the app, then run ', h('code', { text: 'python -m handprinter backup D:\\handprinter-backup' }), '. It copies the SQLite database safely plus all images, crops, packages and snapshots. To restore, point ', h('code', { text: 'HANDPRINTER_DATA_DIR' }), ' at the backup folder (or copy it back).'),
      h('p', { class: 'xs muted', text: 'Nothing in the data directory is ever committed to Git.' })));
}
