// Fetch wrapper: adds the per-start request token, parses JSON, raises readable errors.

const tokenMeta = /** @type {HTMLMetaElement | null} */ (document.querySelector('meta[name="hp-token"]'));
export const TOKEN = tokenMeta ? tokenMeta.content : '';

export class ApiError extends Error {
  /** @param {string} message @param {number} status */
  constructor(message, status) { super(message); this.status = status; }
}

/** @type {Set<(online: boolean) => void>} */
const connectionListeners = new Set();
let online = true;
/** @param {(online: boolean) => void} fn */
export function onConnection(fn) { connectionListeners.add(fn); }
/** @param {boolean} value */
function setOnline(value) {
  if (value === online) return;
  online = value;
  connectionListeners.forEach((fn) => fn(value));
}

/**
 * @param {string} path
 * @param {{method?: string, body?: any, raw?: boolean, signal?: AbortSignal}} [opts]
 * @returns {Promise<any>}
 */
export async function api(path, opts = {}) {
  const method = opts.method || (opts.body !== undefined ? 'POST' : 'GET');
  /** @type {Record<string, string>} */
  const headers = { 'x-handprinter-token': TOKEN };
  let body;
  if (opts.body instanceof FormData) body = opts.body;
  else if (opts.body !== undefined) { headers['content-type'] = 'application/json'; body = JSON.stringify(opts.body); }
  let res;
  try {
    res = await fetch(path, { method, headers, body, signal: opts.signal, credentials: 'same-origin' });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    setOnline(false);
    throw new ApiError('The Handprinter server is not reachable. Is it still running?', 0);
  }
  setOnline(true);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === 'string' ? data.detail : (data.detail || []).map((/** @type {any} */ d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
    } catch { /* not JSON */ }
    throw new ApiError(detail || `Request failed (${res.status})`, res.status);
  }
  if (opts.raw) return res;
  const type = res.headers.get('content-type') || '';
  return type.includes('application/json') ? res.json() : res.text();
}

/**
 * Upload one file with progress (fetch has no upload progress).
 * @param {string} path @param {File} file @param {(fraction: number) => void} onProgress
 * @returns {Promise<any>}
 */
export function upload(path, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.setRequestHeader('x-handprinter-token', TOKEN);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new ApiError((data && data.detail) || `Upload failed (${xhr.status})`, xhr.status));
    };
    xhr.onerror = () => { setOnline(false); reject(new ApiError('Upload failed: server not reachable', 0)); };
    const form = new FormData();
    form.append('file', file, file.name);
    xhr.send(form);
  });
}
