import { createHelixRenderer } from './helix.js';
import { fontsReady } from './login.js';
import { PAUSED_TEXT, renderPage, reportView, stageInfo } from './workspace.js';
import { createQuestionAlerts } from './question-alerts.js';
import { createQuestionAnswer } from './question.js';
import { createIntakeDraft, encodeIntake } from './intake.js';
export { reportView };
export const renderDemo = renderPage;

const list = value => Array.isArray(value) ? value : [];
const formatBytes = bytes => `${(bytes / (1024 * 1024)).toLocaleString(undefined, { maximumFractionDigits: 1 })} MiB`;

/** When a full demo should have a free slot, from the worker's retry_at (Unix seconds). */
export function retryText(retryAt, now = Date.now()) {
  const at = Number(retryAt) * 1000;
  if (!Number.isFinite(at) || at <= 0) return 'Please try again later.';
  const minutes = Math.max(1, Math.round((at - now) / 60000));
  const time = new Date(at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  const wait = minutes < 60 ? `${minutes} minute${minutes === 1 ? '' : 's'}` : `${Math.floor(minutes / 60)} h${minutes % 60 ? ` ${minutes % 60} min` : ''}`;
  return `Please try again around ${time} (in about ${wait}).`;
}

export function requestError(status, body = {}) {
  if (status === 413) return 'This file is too large to upload or unpack. Choose a smaller DNA file.';
  if (status === 408) return 'The upload took too long. Check your connection and try again.';
  if (status === 507) return 'There isn’t enough space on the analysis server right now. Please try again later.';
  if (status === 409 && body.full) return `There’s no room for another analysis right now. ${retryText(body.retry_at)}`;
  if (status === 429) {
    const seconds = Math.max(1, Math.min(3600, Math.ceil(Number(body.retry_after) || 5)));
    return `Too many requests. Please wait ${seconds} seconds and try again.`;
  }
  if (status === 503 && body.paused) return PAUSED_TEXT;
  if (status === 409) return 'The analysis changed while this request was being sent. Check the current status and try again.';
  if (status === 403) return 'This request could not be accepted. Reload the page and try again.';
  if (status === 400 || status === 422) return 'This request could not be accepted. Check the file or answer and try again.';
  return 'Connection lost. Your last confirmed status is shown; reconnecting…';
}

/** A file upload that reports its progress, which fetch cannot: XMLHttpRequest, answering as fetch
 * does. A redirect is an error, as it is for fetch here. */
export function xhrSend(path, { headers, body, signal, onProgress }) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = event => { if (event.lengthComputable) onProgress(event.loaded, event.total); };
    xhr.onload = () => {
      if (new URL(xhr.responseURL, location.href).pathname !== path) { reject(new TypeError('Redirected')); return; }
      resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, json: async () => JSON.parse(xhr.responseText),
        headers: { get: name => xhr.getResponseHeader(name) } });
    };
    xhr.onerror = xhr.ontimeout = () => reject(new TypeError('Network error'));
    xhr.onabort = () => reject(new DOMException('Aborted', 'AbortError'));
    signal.addEventListener('abort', () => xhr.abort(), { once: true });
    xhr.send(body);
  });
}

/** In-memory transport. Every reply belongs to one session generation. */
export function createDemoClient({ fetchImpl = globalThis.fetch, sendImpl = globalThis.XMLHttpRequest ? xhrSend : null, onChange = () => {}, onExpire = () => {}, now = Date.now, setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  const state = { session: null, run: null, error: '', notice: '', pending: '', ready: false, uploaded: null };
  let generation = 0;
  let active = true;
  let pollTimer = null;
  let expiryTimer = null;
  let fetchingRun = false;
  let runRevision = 0;
  let pollError = '';
  let answerWait = null;
  const controllers = new Set();
  const emit = () => onChange({ ...state });
  const expiry = () => Number(state.session?.expires_at) * 1000;
  // Past expires_at -- the daily password changed -- the server keeps a session for its run
  // while it is going or its result is still there to read, and answers 401 once it does not:
  // the page stays until then, and leaves on that 401.
  const keptForDemo = () => !!state.session?.kept_for_run
    || !!state.run?.owned && ['running', 'waiting', 'completed', 'failed'].includes(state.run.status);
  const expired = () => !!state.session && now() >= expiry() && !keptForDemo();
  // Compare content because the worker's rolling progress window can stay the
  // same length when the agent advances. A person's answer is not agent progress.
  const progressSnapshot = run => JSON.stringify(list(run?.events ?? run?.progress)
    .filter(event => event?.kind !== 'answer' && event?.kind !== 'question'));
  function acceptRun(run) {
    state.run = run;
    if (answerWait && (
      !['running', 'waiting'].includes(run.status)
      || (run.run_id && answerWait.runId && run.run_id !== answerWait.runId)
      || (run.question?.id && run.question.id !== answerWait.questionId)
      || progressSnapshot(run) !== answerWait.progress
    )) {
      answerWait = null;
      state.notice = '';
    }
  }
  function clear() {
    active = false;
    generation++;
    clearTimer(pollTimer);
    clearTimer(expiryTimer);
    for (const controller of controllers) controller.abort();
    controllers.clear();
    answerWait = null;
    Object.assign(state, { session: null, run: null, error: '', notice: '', pending: '', ready: false, uploaded: null });
    emit();
  }
  function expire() { if (!active) return; clear(); onExpire(); }
  function expireUnlessKept() {
    if (!active) return;
    if (!expired()) { expiryTimer = setTimer(expireUnlessKept, 60000); return; }
    expire();
  }
  async function request(path, { method = 'GET', json, file, intake } = {}) {
    if (!active) return null;
    if (expired()) { expire(); return null; }
    const version = generation;
    const controller = new AbortController();
    controllers.add(controller);
    const headers = { Accept: 'application/json' };
    let body;
    if (method !== 'GET') {
      headers['X-CSRF-Token'] = state.session?.csrf || '';
      // The answers to the routine questions go in one header, never in the URL — with an
      // uploaded file, or with the name of a supplied sample run without one.
      if (intake) headers['X-PRS-Intake'] = encodeIntake(intake);
      if (file) {
        headers['Content-Type'] = 'application/octet-stream';
        headers['X-Filename'] = encodeURIComponent(file.name);
        body = file;
      } else {
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify(json ?? {});
      }
    }
    // Whole percents of an upload sent, for its progress bar.
    const onProgress = (loaded, total) => {
      const percent = Math.floor(100 * loaded / total);
      if (active && version === generation && percent !== state.uploaded) { state.uploaded = percent; emit(); }
    };
    try {
      const response = file && sendImpl ? await sendImpl(path, { headers, body, signal: controller.signal, onProgress })
        : await fetchImpl(path, { method, headers, body, signal: controller.signal, cache: 'no-store', credentials: 'same-origin', mode: 'same-origin', redirect: 'error' });
      if (!active || version !== generation) return null;
      if (response.status === 401 || expired()) { expire(); return null; }
      const data = await response.json().catch(() => ({}));
      if (!active || version !== generation) return null;
      if (!response.ok) {
        const error = new Error(requestError(response.status, data));
        error.status = response.status;
        // A file the analysis can't read: the server's reason, written for the person.
        if (file && response.status === 400 && typeof data.error === 'string' && data.error) error.message = data.error;
        error.retryAfter = Math.max(5, Math.min(3600, Number(data.retry_after ?? response.headers.get('Retry-After')) || 5));
        throw error;
      }
      return data;
    } finally { controllers.delete(controller); }
  }
  function schedulePoll(delay = 2000) {
    clearTimer(pollTimer);
    if (active && state.session) pollTimer = setTimer(poll, delay);
  }
  async function poll() {
    if (!active || !state.session || fetchingRun || state.pending) return;
    fetchingRun = true;
    const revision = runRevision;
    let delay = 2000;
    try {
      const run = await request('/api/run');
      if (run && revision === runRevision) {
        acceptRun(run);
        if (state.error === pollError) state.error = '';
        pollError = '';
        emit();
      }
    } catch (error) {
      if (active && revision === runRevision && error.name !== 'AbortError') { pollError = error.status ? error.message : requestError(500); state.error = pollError; emit(); }
      delay = error.status === 429 ? error.retryAfter * 1000 : 5000;
    } finally { fetchingRun = false; schedulePoll(delay); }
  }
  async function start() {
    if (!active) return;
    try {
      const session = await request('/api/session');
      if (!session) return;
      if (!session.authenticated || typeof session.csrf !== 'string' || !Number.isFinite(Number(session.expires_at))) { expire(); return; }
      state.session = session;
      if (expired()) { expire(); return; }
      expiryTimer = setTimer(expireUnlessKept, Math.max(0, expiry() - now()));
      state.ready = true;
      emit();
      await poll();
    } catch (error) {
      if (active && error.name !== 'AbortError') { state.error = 'Could not connect. Reload the page to try again.'; emit(); }
    }
  }
  async function mutate(kind, path, payload = {}) {
    if (!active || !state.session || state.pending) return false;
    const answerBaseline = kind === 'answer' ? {
      questionId: payload.json.question_id, runId: state.run?.run_id,
      progress: progressSnapshot(state.run),
    } : null;
    runRevision++;
    state.pending = kind;
    state.error = '';
    pollError = '';
    state.notice = '';
    answerWait = null;
    clearTimer(pollTimer);
    emit();
    let delay = 2000;
    try {
      const reply = await request(path, { method: 'POST', ...payload });
      if (!reply) return false;
      if (kind === 'logout') { expire(); return true; }
      if (kind === 'end') { state.run = reply.status ? reply : { status: 'idle', owned: false }; state.notice = 'Analysis ended. Its files have been deleted.'; }
      else if (kind === 'answer') {
        answerWait = answerBaseline;
        state.notice = 'Your answer was sent. Waiting for the analysis to continue.';
        acceptRun(reply.status ? reply : { ...state.run, question: null, status: 'running' });
      }
      else { state.run = reply.status ? reply : { status: 'running', owned: true, progress: [] }; }
      return true;
    } catch (error) {
      if (active && error.name !== 'AbortError') { state.error = error.status ? error.message : requestError(500); }
      if (error.status === 429) delay = error.retryAfter * 1000;
      return false;
    } finally {
      if (active) { state.pending = ''; state.uploaded = null; emit(); schedulePoll(delay); }
    }
  }
  return {
    state, start, poll, clear,
    upload: (file, intake) => mutate('upload', '/api/run', { file, intake }),
    useSample: (name, intake) => mutate('upload', '/api/sample', { json: { name }, intake }),
    answer: (id, text) => mutate('answer', '/api/answer', { json: { question_id: id, text } }),
    end: () => mutate('end', '/api/end'),
    logout: () => mutate('logout', '/api/logout'),
  };
}

function mount() {
  const app = document.querySelector('#app');
  const announcement = document.querySelector('#announcement');
  const urgent = document.querySelector('#urgent-announcement');
  let helixRenderer = null;
  const answerDraft = createQuestionAnswer();
  const intakeDraft = createIntakeDraft();
  let file = null, previous = '', lastAnnouncement = '', lastQuestion = null, lastScene = '';
  const alerts = createQuestionAlerts({ onChange: () => renderState(client.state) });
  function renderState(state) {
    if (!state.session) { file = null; answerDraft.reset(); intakeDraft.reset(); }
    const questionId = state.run?.question?.id ?? null;
    answerDraft.sync(state.run?.question);
    const alertState = alerts.state();
    const html = renderDemo({ ...state, origin: location.origin, alertsEnabled: alertState.enabled, alertMessage: alertState.message });
    alerts.observe(state.run);
    if (html === previous) { helixRenderer?.sync(); return; }
    const focus = document.activeElement;
    const selection = ['answer', 'intake-condition', 'intake-reason', 'intake-ancestry'].includes(focus?.id) ? [focus.selectionStart, focus.selectionEnd] : null;
    const focusId = focus?.id, focusAction = focus?.closest('[data-action]')?.dataset.action;
    const openDetails = new Map([...app.querySelectorAll('details[id]')].map(detail => [detail.id, detail.open]));
    const helix = app.querySelector('.dna-helix');
    const current = state.run ? stageInfo(state.run).current : null;
    const scene = `${state.run?.run_id}:${state.run?.status}:${questionId}:${current?.id}:${current?.status}`;
    previous = html;
    app.innerHTML = html;
    // Where the estimate's low end falls on the time bar, its window and its mark: set here, as
    // the page's CSP allows no inline style.
    app.querySelectorAll('.elapsed-track [data-at]').forEach(mark => { mark.style.left = `${mark.dataset.at}%`; });
    const replacement = app.querySelector('.dna-helix');
    if (helix && replacement) { helix.className = replacement.className; replacement.replaceWith(helix); helixRenderer?.sync(); }
    else { helixRenderer?.dispose(); helixRenderer = replacement ? createHelixRenderer(replacement) : null; }
    if (lastScene && scene !== lastScene) app.querySelector('.analysis-hero, .question-panel, .report')?.classList.add('scene-arrival');
    lastScene = scene;
    answerDraft.restore(app, !!state.pending);
    intakeDraft.restore(app);
    const fileLabel = app.querySelector('#selected-file');
    if (fileLabel && file) fileLabel.textContent = `${file.name} · ${formatBytes(file.size)}`;
    app.querySelectorAll('details[id]').forEach(detail => { if (openDetails.has(detail.id)) detail.open = openDetails.get(detail.id); });
    const newFocus = focusId ? document.getElementById(focusId) : focusAction ? app.querySelector(`[data-action="${focusAction}"]`) : null;
    if (newFocus && !newFocus.disabled) { newFocus.focus({ preventScroll: true }); if (selection) newFocus.setSelectionRange(...selection); }
    const notice = state.error || state.notice || (state.run?.status === 'completed' ? 'Your analysis is complete.' : '');
    if (notice !== lastAnnouncement) { announcement.textContent = notice; lastAnnouncement = notice; }
    if (questionId !== lastQuestion) { urgent.textContent = questionId ? `Your answer is needed. ${state.run.question.text}` : ''; lastQuestion = questionId; }
  }
  const client = createDemoClient({ onExpire: () => { alerts.dispose(); location.replace('/'); }, onChange: renderState });
  app.addEventListener('input', event => {
    if (intakeDraft.update(event.target)) { app.querySelector('#intake-problem')?.replaceChildren(); return; }
    if (event.target.id !== 'answer') return;
    answerDraft.type(event.target.value);
    answerDraft.restore(app, !!client.state.pending);
  });
  app.addEventListener('change', event => {
    if (intakeDraft.update(event.target)) { app.querySelector('#intake-problem')?.replaceChildren(); return; }
    if (event.target.name === 'answer-choice') {
      answerDraft.choose(event.target.value);
      answerDraft.restore(app, !!client.state.pending);
      return;
    }
    if (event.target.id !== 'genome-file') return;
    file = event.target.files?.[0] || null;
    const label = app.querySelector('#selected-file');
    label.textContent = file ? `${file.name} · ${formatBytes(file.size)}` : '';
    const tooLarge = file && file.size > Number(client.state.session?.limits?.upload_bytes);
    if (tooLarge) label.textContent = requestError(413);
    app.querySelector('#upload-submit').disabled = !!tooLarge;
  });
  app.addEventListener('submit', async event => {
    event.preventDefault();
    if (event.target.id === 'upload-form') {
      // In the order the form asks: the condition, the file, the agreement.
      const missing = intakeDraft.problem();
      const say = problem => { app.querySelector('#intake-problem').textContent = problem.message; app.querySelector(`#${problem.field}`).focus(); };
      if (missing?.field === 'intake-condition') { say(missing); return; }
      if (!file) { app.querySelector('#selected-file').textContent = 'Choose one genome file first.'; app.querySelector('#genome-file').focus(); return; }
      if (missing) { say(missing); return; }
      if (file.size > Number(client.state.session?.limits?.upload_bytes) || client.state.run?.paused) return;
      if (await client.upload(file, intakeDraft.payload())) { file = null; intakeDraft.reset(); }
    } else if (event.target.id === 'answer-form') {
      answerDraft.sync(client.state.run?.question);
      const answer = answerDraft.payload();
      if (!answer || client.state.pending) return;
      await client.answer(answer.question_id, answer.text);
    }
  });
  app.addEventListener('click', async event => {
    const action = event.target.closest('[data-action]')?.dataset.action;
    if (action === 'logout') await client.logout();
    if (action === 'end' && await client.end()) { file = null; answerDraft.reset(); }
    if (action === 'alerts') await alerts.toggle();
    if (action === 'copy-link') {
      const field = app.querySelector('#private-link');
      field?.select();
      try { await navigator.clipboard.writeText(field.value); event.target.closest('[data-action]').textContent = 'Copied'; } catch { /* Selected: it can be copied by hand. */ }
    }
    if (action === 'use-sample') {
      const name = event.target.closest('[data-action]')?.dataset.sample;
      if (!name || client.state.pending || client.state.run?.paused) return;
      // The routine answers are required for a sample run too, in the order the form asks.
      const missing = intakeDraft.problem();
      if (missing) { app.querySelector('#intake-problem').textContent = missing.message; app.querySelector(`#${missing.field}`)?.focus(); return; }
      if (await client.useSample(name, intakeDraft.payload())) { file = null; intakeDraft.reset(); }
    }
    if (action === 'retry') { if (client.state.ready) await client.poll(); else location.reload(); }
  });
  const visibility = () => document.documentElement.classList.toggle('is-page-hidden', document.hidden);
  document.addEventListener('visibilitychange', visibility); visibility();
  window.addEventListener('pagehide', () => { helixRenderer?.dispose(); alerts.dispose(); client.clear(); app.replaceChildren(); announcement.textContent = ''; urgent.textContent = ''; });
  window.addEventListener('pageshow', event => { if (event.persisted) location.replace('/'); });
  client.start();
}
if (typeof document !== 'undefined') fontsReady.then(mount);
