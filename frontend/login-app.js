import { fontsReady } from './fonts.js';
export { fontsReady };

function mountLogin() {
const form = document.querySelector('#login-form');
const password = document.querySelector('#password');
const submit = document.querySelector('#login-submit');
const message = document.querySelector('#login-message');
const retry = document.querySelector('#login-retry');
let csrf = '';
let cooldown = null;
let controller = null;

async function connect() {
  retry.hidden = true;
  message.textContent = 'Connecting…';
  submit.disabled = true;
  password.disabled = true;
  controller?.abort();
  controller = new AbortController();
  try {
    const response = await fetch('/api/session', { cache: 'no-store', credentials: 'same-origin', mode: 'same-origin', redirect: 'error', signal: controller.signal });
    const session = await response.json();
    if (!response.ok) throw new Error('connection');
    if (session.authenticated) { location.replace('/'); return; }
    if (typeof session.csrf !== 'string' || !session.csrf) throw new Error('connection');
    csrf = session.csrf;
    password.disabled = false;
    submit.disabled = false;
    message.textContent = 'Your sign-in lasts 24 hours.';
    password.focus();
  } catch (error) {
    if (error.name === 'AbortError') return;
    message.textContent = 'Could not connect. Please try again.';
    retry.hidden = false;
  }
}

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (submit.disabled || !csrf) return;
  const value = password.value;
  password.value = '';
  submit.disabled = true;
  message.textContent = 'Signing in…';
  controller = new AbortController();
  try {
    const response = await fetch('/api/login', {
      method: 'POST', cache: 'no-store', credentials: 'same-origin', mode: 'same-origin', redirect: 'error', signal: controller.signal,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify({ password: value }),
    });
    const body = await response.json().catch(() => ({}));
    if (response.ok) { location.replace('/'); return; }
    const seconds = Math.max(1, Math.min(3600, Math.ceil(Number(body.retry_after ?? response.headers.get('Retry-After')) || 1)));
    // A wrong password (401) says so plainly, in the server's words; being throttled (429) says to
    // wait. The server authors both messages, so the page shows exactly one of them.
    const wrong = response.status === 401;
    message.textContent = typeof body.error === 'string' && body.error
      ? body.error
      : `Could not sign in. Please wait ${seconds} ${seconds === 1 ? 'second' : 'seconds'} and try again.`;
    // A wrong password: let them retype at once. Throttled: hold the field until the wait is up.
    if (wrong) { submit.disabled = false; password.focus(); }
    else cooldown = setTimeout(() => { submit.disabled = false; message.textContent = 'Please try signing in again.'; }, seconds * 1000);
    if (response.status === 403) { retry.hidden = false; retry.textContent = 'Try signing in again'; }
  } catch (error) {
    if (error.name === 'AbortError') return;
    message.textContent = 'Could not connect. Please try again.';
    submit.disabled = false;
  }
});
retry.addEventListener('click', () => { clearTimeout(cooldown); connect(); });
window.addEventListener('pagehide', () => { controller?.abort(); clearTimeout(cooldown); password.value = ''; csrf = ''; });
window.addEventListener('pageshow', event => { if (event.persisted) connect(); });
connect();

}
if (typeof document !== 'undefined' && document.querySelector('#login-form')) fontsReady.then(mountLogin);
