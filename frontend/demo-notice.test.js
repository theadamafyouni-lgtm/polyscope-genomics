import test from 'node:test';
import assert from 'node:assert/strict';
import { createDemoClient, renderDemo } from './demo.js';

// Invented API replies only; no retained run or person data.
const session = { authenticated: true, csrf: 'synthetic-csrf', expires_at: 3600 };
const question = { id: 'synthetic-question', text: 'Continue with this example?' };
const initial = {
  status: 'waiting', owned: true, run_id: 'synthetic-run', question,
  progress: [{ kind: 'stage_completed', stage: 'select', outcome: 'proceed', text: 'Example model selected.' }],
};
const acknowledged = { ...initial, status: 'running', question: null };
const advanced = { ...acknowledged,
  progress: [{ kind: 'stage_progress', stage: 'score', text: 'Example scoring update.' }] };
const reply = body => ({ ok: true, status: 200, json: async () => body });

async function setup(t, answerReply = acknowledged, firstRun = initial) {
  let next = firstRun;
  const client = createDemoClient({
    now: () => 1000, setTimer: () => 1, clearTimer: () => {},
    fetchImpl: async (path, options) => reply(path === '/api/session' ? session
      : path === '/api/answer' ? answerReply
        : options.method === 'POST' ? { status: 'idle', owned: false } : next),
  });
  t.after(() => client.clear());
  await client.start();
  assert.equal(await client.answer(question.id, 'Continue'), true);
  return { client, poll: async run => { next = run; await client.poll(); } };
}

test('answer notice remains while the agent has produced no new update', async t => {
  const { client, poll } = await setup(t);
  assert.match(client.state.notice, /Waiting for the analysis to continue/);
  await poll(structuredClone(acknowledged));
  assert.match(client.state.notice, /Waiting for the analysis to continue/);
});

test('new progress clears the answer notice even when the progress window has the same length', async t => {
  const { client, poll } = await setup(t);
  assert.equal(advanced.progress.length, initial.progress.length);
  await poll(advanced);
  assert.equal(client.state.notice, '');
  assert.doesNotMatch(renderDemo(client.state), /Waiting for the analysis to continue/);
});

test('raw answer and question history alone is not new agent progress', async t => {
  const firstRun = { ...initial, progress: undefined, events: initial.progress };
  const answerReply = { ...firstRun, status: 'running', question: null };
  const { client, poll } = await setup(t, answerReply, firstRun);
  const answered = { ...answerReply, events: [...firstRun.events,
    { kind: 'question', question_id: question.id, stage: 'select', question: question.text },
    { kind: 'answer', question_id: question.id, text: 'Continue' },
  ] };
  await poll(answered);
  assert.match(client.state.notice, /Waiting for the analysis to continue/);
  await poll({ ...answered, events: [...answered.events, ...advanced.progress] });
  assert.equal(client.state.notice, '');
});

test('a new question, terminal status, or replacement run clears the answer notice', async t => {
  for (const run of [
    { ...initial, question: { id: 'next-question', text: 'Another example question?' } },
    { ...acknowledged, status: 'completed' },
    { ...acknowledged, status: 'failed' },
    { status: 'idle', owned: false },
    { status: 'busy', owned: false },
    { ...acknowledged, run_id: 'replacement-run' },
  ]) {
    await t.test(run.question?.id || run.run_id + ':' + run.status, async t => {
      const { client, poll } = await setup(t);
      await poll(run);
      assert.equal(client.state.notice, '');
    });
  }
});

test('an answer acknowledgement that already contains new progress or an ending needs no waiting notice', async t => {
  for (const run of [advanced, { ...acknowledged, status: 'completed' },
    { ...acknowledged, status: 'failed' },
    { ...initial, question: { id: 'next-question', text: 'Next example?' } }]) {
    await t.test(run.status + ':' + (run.question?.id || 'no-question'), async t => {
      const { client } = await setup(t, run);
      assert.equal(client.state.notice, '');
    });
  }
});

test('an acknowledgement without run state keeps waiting until the next agent update', async t => {
  const { client, poll } = await setup(t, { ok: true });
  assert.match(client.state.notice, /Waiting for the analysis to continue/);
  await poll(advanced);
  assert.equal(client.state.notice, '');
});

test('polls preserve the separate End demo confirmation', async t => {
  const { client, poll } = await setup(t);
  assert.equal(await client.end(), true);
  const notice = client.state.notice;
  assert.match(notice, /Analysis ended/);
  await poll({ status: 'idle', owned: false });
  assert.equal(client.state.notice, notice);
});

test('the sign-in page asks for the shared password and says a sign-in lasts 24 hours', async () => {
  const { readFile } = await import('node:fs/promises');
  const page = await readFile(new URL('./login.html', import.meta.url), 'utf8');
  const script = await readFile(new URL('./login-app.js', import.meta.url), 'utf8');
  assert.match(page, /Sign in with the shared password from Adam\./);
  assert.doesNotMatch(page + script, /today’s shared password|midnight|eight hours/);
  assert.match(script, /Your sign-in lasts 24 hours\./);
});
