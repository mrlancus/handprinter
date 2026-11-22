// LAN sign-in (only used when HANDPRINTER_HOST is non-loopback).
const form = /** @type {HTMLFormElement} */ (document.getElementById('login'));
const input = /** @type {HTMLInputElement} */ (document.getElementById('token'));
const error = /** @type {HTMLElement} */ (document.getElementById('error'));
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const res = await fetch('/api/login', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ token: input.value }) });
  if (res.ok) location.href = '/';
  else error.textContent = 'That token is not correct.';
});
