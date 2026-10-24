// Isolated design review: no API requests, mutations, or product entry-point imports.
import { fixtureState, FIXED_NOW } from './fixtures.mjs';
import { renderDirectionA } from './direction-a.js';
import { renderDirectionB } from './direction-b.js';

const params = new URLSearchParams(location.search);
const direction = params.get('direction') === 'b' ? 'b' : 'a';
let stateName = ['running', 'waiting-answer', 'finished-score', 'finished-refusal'].includes(params.get('state')) ? params.get('state') : 'running';
const preview = document.querySelector('#preview');
const status = document.querySelector('#review-status');
let enabled = false;
let context;
const notified = new Set();

function sound() {
  if (!context || context.state !== 'running') return;
  const oscillator = context.createOscillator();
  const gain = context.createGain();
  oscillator.type = 'sine';
  oscillator.frequency.setValueAtTime(660, context.currentTime);
  gain.gain.setValueAtTime(0, context.currentTime);
  gain.gain.linearRampToValueAtTime(.04, context.currentTime + .04);
  gain.gain.exponentialRampToValueAtTime(.001, context.currentTime + .35);
  oscillator.connect(gain); gain.connect(context.destination);
  oscillator.start(); oscillator.stop(context.currentTime + .4);
}
function render() {
  const previousAnswer = preview.querySelector('#answer');
  const draft = previousAnswer?.value;
  const focus = document.activeElement;
  const focusId = focus?.id;
  const selection = focusId === 'answer' ? [focus.selectionStart, focus.selectionEnd] : null;
  const openDetails = [...preview.querySelectorAll('details')].map(detail => detail.open);
  const state = fixtureState(stateName);
  preview.innerHTML = (direction === 'a' ? renderDirectionA : renderDirectionB)(state, FIXED_NOW);
  const answer = preview.querySelector('#answer');
  if (answer && draft !== undefined) answer.value = draft;
  preview.querySelectorAll('details').forEach((detail, index) => { detail.open = !!openDetails[index]; });
  const question = state.run.question;
  document.title = question ? 'Answer needed · prs-agent' : state.run.status === 'completed' ? 'Your result · prs-agent' : 'Analysis in progress · prs-agent';
  document.querySelector('#announcement').textContent = question ? 'Your answer is needed. The analysis is waiting for you.' : '';
  const alerts = preview.querySelector('[data-action="alerts"]');
  if (alerts) alerts.setAttribute('aria-pressed', String(enabled));
  if (alerts && enabled) { alerts.textContent = 'Question alerts enabled'; alerts.setAttribute('aria-pressed', 'true'); }
  if (focusId) document.getElementById(focusId)?.focus({ preventScroll: true });
  else if (focus?.dataset.action === 'alerts') alerts?.focus({ preventScroll: true });
  if (answer && selection) answer.setSelectionRange(...selection);
  const key = question ? `${state.run.run_id}:${question.id}` : null;
  if (key && enabled && !notified.has(key)) {
    notified.add(key);
    sound();
    if (document.hidden && 'Notification' in window && Notification.permission === 'granted') {
      try {
        const notification = new Notification('Your answer is needed', { body: 'Open prs-agent to continue your analysis.', tag: 'prs-question' });
        notification.onclick = () => { window.focus(); notification.close(); };
      } catch { /* Some mobile browsers require service-worker notifications. The title still updates. */ }
    }
  }
  document.querySelector('#review-label').textContent = `Direction ${direction.toUpperCase()} · ${direction === 'a' ? 'Clear workbench' : 'Field journal'} · Synthetic test fixtures. No analysis performed.`;
  document.querySelector('#other-direction').href = `?direction=${direction === 'a' ? 'b' : 'a'}&state=${stateName}`;
  document.querySelectorAll('[data-state]').forEach(link => { link.href = `?direction=${direction}&state=${link.dataset.state}`; });
}
document.addEventListener('click', async event => {
  const fixtureLink = event.target.closest('[data-state]');
  if (fixtureLink) {
    event.preventDefault(); stateName = fixtureLink.dataset.state;
    history.replaceState({}, '', fixtureLink.href); render(); window.scrollTo(0, 0); return;
  }
  const button = event.target.closest('[data-action]');
  if (!button) return;
  if (button.dataset.action === 'alerts') {
    if (enabled) { enabled = false; render(); status.textContent = 'Question sounds and notifications are off. The tab title will still flag a question.'; return; }
    const Audio = window.AudioContext || window.webkitAudioContext;
    if (Audio) { context ||= new Audio(); await context.resume().catch(() => {}); }
    enabled = true;
    // Permission is requested only through this explicit button gesture.
    if ('Notification' in window && Notification.permission === 'default') {
      await Notification.requestPermission().catch(() => {});
    }
    render();
    status.textContent = 'Question sounds enabled. Notifications appear in another tab if browser permission allows them.';
  } else status.textContent = 'Design preview only. No session or run is changed.';
});
document.addEventListener('submit', event => {
  if (event.target.id !== 'answer-form') return;
  event.preventDefault();
  status.textContent = 'Design preview only. Your answer has not been sent or saved.';
});
window.addEventListener('pagehide', () => context?.close());
render();
