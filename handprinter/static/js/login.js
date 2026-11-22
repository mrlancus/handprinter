// Sign-in for online (Cloudflare Tunnel) and LAN mode.
const form = /** @type {HTMLFormElement} */ (document.getElementById('login'));
const input = /** @type {HTMLInputElement} */ (document.getElementById('token'));
const button = /** @type {HTMLButtonElement} */ (document.getElementById('submit'));
const error = /** @type {HTMLElement} */ (document.getElementById('error'));
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  button.disabled = true;
  error.textContent = '';
  try {
    const res = await fetch('/api/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ token: input.value }), credentials: 'same-origin' });
    if (res.ok) { location.replace('/'); return; }
    let detail = '';
    try { detail = (await res.json()).detail; } catch { /* not JSON */ }
    error.textContent = res.status === 429 ? detail : 'That password is not correct.';
    input.select();
  } catch {
    error.textContent = 'The server could not be reached. Is the PC on and Handprinter running?';
  } finally {
    button.disabled = false;
  }
});
