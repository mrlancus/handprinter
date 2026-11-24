// Manual hand-off: each numbered batch keeps the same images across downloads and imports.
import { api } from '../api.js';
import { h, mount } from '../dom.js';
import { trackJob } from '../jobs.js';
import { callout, loadingState } from '../components/states.js';

/** @param {string} id @param {() => void} [onClose] */
export function openChatPack(id, onClose) {
  const body = h('div', { class: 'stack' });
  const dialog = h('dialog', { class: 'dialog dialog--wide', 'aria-label': 'Chat pack' },
    h('div', { class: 'dialog__head' }, h('h2', { text: id.startsWith('ccut_') ? 'Word boxes · chat pack' : 'Recognition · chat pack' }),
      h('button', { class: 'btn', onClick: () => dialog.close() }, 'Close')), body);
  dialog.addEventListener('close', () => { dialog.remove(); onClose?.(); });
  document.body.appendChild(dialog);
  dialog.showModal();
  const base = '/api/chat-packs/' + encodeURIComponent(id);

  /** @param {number} [batch] @param {string} [notice] @param {string} [draft] */
  async function load(batch, notice = '', draft = '') {
    mount(body, loadingState(1));
    try {
      const p = await api(base + (batch ? '?batch=' + batch : ''));
      if (!p.total) { mount(body, callout('There are no images in this pack.')); return; }
      const url = base + '/batches/' + p.number;
      const status = h('p', { role: 'status', class: 'small', text: notice });
      const prompt = h('textarea', { readonly: true, rows: 4, 'aria-label': 'Chat prompt' });
      prompt.value = p.prompt;
      const reply = h('textarea', { rows: 5, maxlength: 192000, 'aria-label': 'JSON reply', placeholder: 'Paste the JSON reply here' });
      reply.value = draft;
      const chooseFile = h('input', { type: 'file', accept: '.json,.txt,application/json,text/plain' });
      chooseFile.onchange = async () => {
        const file = chooseFile.files?.[0];
        if (!file) return;
        if (file.size > 768000) { status.textContent = 'Reply file is too large for this batch.'; return; }
        try { reply.value = await file.text(); } catch { status.textContent = 'Could not read the reply file.'; }
      };
      const submit = h('button', { class: 'btn btn--primary' }, 'Import reply');
      submit.onclick = async () => {
        submit.disabled = true;
        status.textContent = 'Checking reply…';
        try {
          const { job_id } = await api(url + '/reply', { body: { reply: reply.value } });
          const job = await trackJob(job_id);
          if (job.status !== 'succeeded') throw new Error(job.error || job.status);
          const r = job.result;
          const errors = Object.entries(r.errors).map(([key, reasons]) => key + ': ' + /** @type {string[]} */ (reasons).join(', '));
          const msg = [(r.batches && r.batches.length > 1 ? `Batches ${r.batches.join(', ')}: ` : '') + r.applied + ' imported for review.', r.previous ? r.previous + ' already processed.' : '',
            r.skipped.length ? r.skipped.length + ' skipped because the page or boxes changed.' : '', ...errors].filter(Boolean).join(' ');
          await load(errors.length ? p.number : undefined, msg, errors.length ? reply.value : '');
        } catch (err) { status.textContent = err instanceof Error ? err.message : String(err); }
        finally { submit.disabled = false; }
      };
      mount(body, h('div', { class: 'row' },
        h('button', { class: 'btn btn--sm', disabled: p.number <= 1, onClick: () => load(p.number - 1) }, 'Previous'),
        h('strong', { text: `Batch ${p.number} of ${p.batches}` }),
        h('button', { class: 'btn btn--sm', disabled: p.number >= p.batches, onClick: () => load(p.number + 1) }, 'Next'),
        h('span', { class: 'small muted', text: `${p.done} of ${p.total} images processed` })),
        status,
        !p.images.length ? callout(p.done === p.total ? 'Pack complete. Open Check or Review to inspect the proposals.' : 'This batch has been processed.') :
          h('div', { class: 'stack' },
            h('p', {}, 'Download and unzip this batch. Attach its PNGs in the listed order to ChatGPT, Gemini, Claude or another image-capable chat, then paste the prompt.'),
            h('div', { class: 'row' }, h('a', { class: 'btn', href: url + '/download', download: true }, 'Download batch ZIP'),
              h('button', { class: 'btn', onClick: async () => {
                try { await navigator.clipboard.writeText(prompt.value); status.textContent = 'Prompt copied.'; }
                catch { const details = prompt.closest('details'); if (details) details.open = true; prompt.focus(); prompt.select(); status.textContent = 'Prompt selected. Use Copy on your device.'; }
              } }, 'Copy prompt')),
            h('details', {}, h('summary', {}, 'Preview or save individual images'),
              h('div', { class: 'stack' }, p.images.map((/** @type {string} */ item) => {
                const image = url + '/images/' + item + '.png';
                return h('figure', {}, h('img', { class: 'chat-crop', loading: 'lazy', src: image, alt: 'Crop ' + item }),
                  h('figcaption', {}, h('a', { href: image, download: item + '.png' }, 'Save ' + item + '.png')));
              }))),
            h('details', { class: 'chat-prompt' }, h('summary', {}, 'Read prompt'), prompt),
            h('label', { class: 'field' }, 'Chat reply (one batch, or several batches pasted together)', reply),
            h('label', { class: 'field' }, 'Or open a JSON reply file', chooseFile), submit));
    } catch (err) {
      mount(body, callout(err instanceof Error ? err.message : String(err), 'error'),
        h('button', { class: 'btn', onClick: () => load(batch, notice, draft) }, 'Try again'));
    }
  }
  load();
}
