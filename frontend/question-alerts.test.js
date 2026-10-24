import test from 'node:test';
import assert from 'node:assert/strict';
import { createQuestionAlerts } from './question-alerts.js';

const questionRun = (id = 'synthetic-question', run_id = 'synthetic-run') => ({
  status: 'waiting', run_id, question: { id, text: 'Private synthetic question about a condition', why_it_matters: 'Private synthetic context' },
});
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function harness({ permission = 'default', answer = 'granted', hidden = true, resume } = {}) {
  const calls = { permissions: 0, notifications: [], contexts: [], sounds: 0, focused: 0, changes: [] };
  const document = { title: 'Initial title', hidden };
  class Notification {
    static permission = permission;
    static async requestPermission() { calls.permissions += 1; this.permission = answer; return answer; }
    constructor(title, options) { this.title = title; this.options = options; this.closed = 0; calls.notifications.push(this); }
    close() { this.closed += 1; }
  }
  class AudioContext {
    constructor() { this.state = 'suspended'; this.currentTime = 10; this.destination = {}; this.closed = 0; calls.contexts.push(this); }
    async resume() { if (resume) await resume(); this.state = 'running'; }
    async close() { this.closed += 1; this.state = 'closed'; }
    createOscillator() { return { frequency: { setValueAtTime() {} }, connect() {}, start() { calls.sounds += 1; }, stop() {} }; }
    createGain() { return { gain: { setValueAtTime() {}, linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {} }; }
  }
  const alerts = createQuestionAlerts({ documentRef: document, NotificationImpl: Notification, AudioContextImpl: AudioContext,
    onChange: state => calls.changes.push(state), focusWindow: () => calls.focused += 1 });
  return { alerts, calls, document, Notification };
}

test('loading and observing questions never requests permission or makes sound before opt-in', () => {
  const { alerts, calls, document } = harness();
  assert.deepEqual(alerts.state(), { enabled: false, message: '' });
  alerts.observe(questionRun());
  assert.equal(calls.permissions, 0);
  assert.equal(calls.contexts.length, 0);
  assert.equal(calls.notifications.length, 0);
  assert.equal(calls.sounds, 0);
  assert.equal(document.title, 'Answer needed · prs-agent');
  alerts.dispose();
});

test('explicit opt-in permits one gentle sound and generic background notification for the pending question', async () => {
  const { alerts, calls } = harness();
  alerts.observe(questionRun('sensitive-question-id', 'sensitive-run-id'));
  const state = await alerts.toggle();
  assert.equal(calls.permissions, 1);
  assert.equal(state.enabled, true);
  assert.match(state.message, /Sound on.*Notifications allowed/);
  assert.equal(calls.sounds, 1);
  assert.equal(calls.notifications.length, 1);
  const notification = calls.notifications[0];
  assert.equal(notification.title, 'Your answer is needed');
  assert.equal(notification.options.body, 'Open prs-agent to continue your analysis.');
  assert.doesNotMatch(JSON.stringify({ title: notification.title, ...notification.options }), /condition|Private|sensitive/);
  notification.onclick();
  assert.equal(calls.focused, 1);
  assert.equal(notification.closed, 1);
  alerts.dispose();
  assert.equal(notification.closed, 1);
});

test('repeated polls deduplicate alerts; a new question or new run can alert again', async () => {
  const { alerts, calls, document } = harness({ permission: 'granted' });
  await alerts.toggle();
  alerts.observe(questionRun());
  for (let i = 0; i < 5; i++) alerts.observe(structuredClone(questionRun()));
  assert.equal(calls.sounds, 1);
  assert.equal(calls.notifications.length, 1);
  alerts.observe(questionRun('next-question'));
  assert.equal(calls.notifications.length, 2);
  alerts.observe(questionRun('next-question', 'next-run'));
  assert.equal(calls.notifications.length, 3);
  assert.equal(calls.permissions, 0);
  alerts.observe({ ...questionRun(), status: 'completed' });
  assert.equal(calls.notifications.length, 3);
  assert.equal(document.title, 'Your result · prs-agent');
  alerts.dispose();
});

test('foreground questions sound after opt-in without issuing desktop notifications', async () => {
  const { alerts, calls, document } = harness({ permission: 'granted', hidden: false });
  await alerts.toggle();
  alerts.observe(questionRun());
  assert.equal(calls.sounds, 1);
  assert.equal(calls.notifications.length, 0);
  assert.equal(document.title, 'Answer needed · prs-agent');
  alerts.dispose();
});

test('denied notification permission is not requested again and sound remains opt-in', async () => {
  for (const options of [{ permission: 'denied' }, { permission: 'default', answer: 'denied' }]) {
    const { alerts, calls } = harness(options);
    await alerts.toggle();
    assert.equal(calls.permissions, options.permission === 'default' ? 1 : 0);
    assert.match(alerts.state().message, /Notifications unavailable or not allowed/);
    alerts.observe(questionRun());
    assert.equal(calls.notifications.length, 0);
    assert.equal(calls.sounds, 1);
    await alerts.toggle();
    await alerts.toggle();
    assert.equal(calls.permissions, options.permission === 'default' ? 1 : 0);
    alerts.dispose();
  }
});

test('unsupported sound and notifications retain the question title with an honest availability message', async () => {
  const document = { title: '', hidden: true };
  const alerts = createQuestionAlerts({ documentRef: document, NotificationImpl: null, AudioContextImpl: null });
  await alerts.toggle();
  assert.match(alerts.state().message, /Notifications unavailable or not allowed/);
  assert.doesNotMatch(alerts.state().message, /Sound on|Notifications allowed/);
  assert.doesNotThrow(() => alerts.observe(questionRun()));
  assert.equal(document.title, 'Answer needed · prs-agent');
  alerts.dispose();
});

test('browser audio refusal and notification constructor failures leave a usable title alert', async () => {
  const { alerts, document, calls, Notification } = harness({ resume: async () => { throw new Error('Audio unavailable'); } });
  await alerts.toggle();
  alerts.observe(questionRun());
  assert.equal(calls.sounds, 0);
  assert.doesNotMatch(alerts.state().message, /Sound on/);
  assert.equal(document.title, 'Answer needed · prs-agent');
  alerts.dispose();
  class UnsupportedNotification extends Notification { constructor() { throw new Error('Use service-worker notifications'); } }
  const unsupported = createQuestionAlerts({ documentRef: document, NotificationImpl: UnsupportedNotification, AudioContextImpl: null });
  await unsupported.toggle();
  assert.doesNotThrow(() => unsupported.observe(questionRun()));
  assert.equal(document.title, 'Answer needed · prs-agent');
  unsupported.dispose();
});

test('disabling alerts stops new sounds and notifications while keeping titles informative', async () => {
  const { alerts, calls, document } = harness({ permission: 'granted' });
  await alerts.toggle();
  alerts.observe(questionRun());
  await alerts.toggle();
  assert.equal(alerts.state().enabled, false);
  assert.match(alerts.state().message, /off.*tab title/);
  alerts.observe(questionRun('second-question'));
  assert.equal(calls.notifications.length, 1);
  assert.equal(calls.sounds, 1);
  assert.equal(document.title, 'Answer needed · prs-agent');
  alerts.dispose();
});

test('answered questions and replacement runs close stale desktop notifications', async () => {
  const { alerts, calls } = harness({ permission: 'granted' });
  await alerts.toggle();
  alerts.observe(questionRun());
  alerts.observe({ status: 'running', run_id: 'synthetic-run', question: null });
  assert.equal(calls.notifications[0].closed, 1);
  alerts.observe(questionRun('next-question'));
  alerts.observe(questionRun('next-question', 'next-run'));
  assert.equal(calls.notifications[1].closed, 1);
  alerts.dispose();
});

test('dispose closes notifications and audio and leaves the controller inert', async () => {
  const { alerts, calls, document } = harness({ permission: 'granted' });
  await alerts.toggle();
  alerts.observe(questionRun());
  alerts.dispose();
  assert.equal(calls.contexts[0].closed, 1);
  assert.equal(calls.notifications[0].closed, 1);
  document.title = 'Signed out';
  alerts.observe(questionRun('after-dispose'));
  await alerts.toggle();
  assert.equal(document.title, 'Signed out');
  assert.equal(alerts.state().enabled, false);
  assert.equal(calls.sounds, 1);
  assert.equal(calls.notifications.length, 1);
  assert.equal(calls.changes.length, 1);
});

test('disposing during audio activation cannot trigger a delayed permission prompt', async () => {
  const pending = deferred();
  const { alerts, calls } = harness({ resume: () => pending.promise });
  const enabling = alerts.toggle();
  alerts.dispose();
  pending.resolve();
  await enabling;
  assert.equal(calls.permissions, 0);
  assert.equal(alerts.state().enabled, false);
  assert.equal(calls.changes.length, 0);
  assert.equal(calls.notifications.length, 0);
});
