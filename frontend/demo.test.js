import test from 'node:test';
import assert from 'node:assert/strict';
import { createDemoClient, renderDemo, reportView, requestError, retryText, xhrSend } from './demo.js';
import { exampleRefusal } from './examples.js';
import { encodeIntake } from './intake.js';

const SESSION = { authenticated: true, csrf: 'test-csrf-token', expires_at: 3600, limits: { upload_bytes: 1000, expanded_bytes: 2000 } };
const reply = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body, headers: new Headers() });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function harness(extraFetch, clock = { now: 1000 }) {
  const calls = [];
  const timers = new Map();
  let timerId = 0;
  let expirations = 0;
  const client = createDemoClient({
    now: () => clock.now,
    setTimer: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId; },
    clearTimer: id => timers.delete(id),
    onExpire: () => expirations++,
    fetchImpl: async (path, options) => {
      calls.push({ path, ...options });
      if (path === '/api/session') return reply(SESSION);
      return await extraFetch(path, options);
    },
  });
  return { client, calls, timers, expirations: () => expirations };
}

test('upload sends one raw file; every request avoids caches and every mutation carries CSRF', async () => {
  const { client, calls } = harness(async (path, options) => path === '/api/run' && options.method === 'GET' ? reply({ status: 'idle' }) : reply({ status: 'running', owned: true }));
  await client.start();
  const file = new Blob(['demo genome']);
  file.name = 'lab genome.vcf';
  assert.equal(await client.upload(file), true);
  const upload = calls.find(call => call.method === 'POST');
  assert.equal(upload.path, '/api/run');
  assert.equal(upload.body, file);
  assert.equal(upload.headers['Content-Type'], 'application/octet-stream');
  assert.equal(upload.headers['X-Filename'], 'lab%20genome.vcf');
  assert.equal(upload.headers['X-CSRF-Token'], SESSION.csrf);
  for (const call of calls) {
    assert.equal(call.cache, 'no-store');
    assert.equal(call.credentials, 'same-origin');
    assert.equal(call.mode, 'same-origin');
    assert.equal(call.redirect, 'error');
  }
  client.clear();
});

test('useSample starts a run on a named sample: JSON body, the answers in the header, no file', async () => {
  const { client, calls } = harness(async (path, options) => path === '/api/sample' && options.method === 'POST' ? reply({ status: 'running', owned: true }) : reply({ status: 'idle' }));
  await client.start();
  const intake = { condition: 'Condition X', consent: true };
  assert.equal(await client.useSample('sample-genome-1.txt', intake), true);
  const start = calls.find(call => call.method === 'POST');
  assert.equal(start.path, '/api/sample');
  assert.equal(JSON.parse(start.body).name, 'sample-genome-1.txt');
  assert.equal(start.headers['Content-Type'], 'application/json');
  assert.equal(start.headers['X-PRS-Intake'], encodeIntake(intake));
  assert.equal(start.headers['X-CSRF-Token'], SESSION.csrf);
  assert.equal(start.headers['X-Filename'], undefined);
  assert.equal(client.state.run.status, 'running');
  client.clear();
});

test('an upload shows its progress, then the reason a file was refused, and slow or full says so', async () => {
  const sent = [];
  let finish;
  const client = createDemoClient({
    now: () => 1000, setTimer: () => 0, clearTimer: () => {},
    fetchImpl: async path => path === '/api/session' ? reply(SESSION) : reply({ status: 'idle' }),
    sendImpl: (path, options) => { sent.push({ path, ...options }); return new Promise(done => { finish = done; }); },
  });
  await client.start();
  const file = new Blob(['demo genome']);
  file.name = 'genome.vcf';
  const answers = { condition: 'Condition X', ancestry: 'Mixed heritage', consent: true };
  const uploading = client.upload(file, answers);
  const [{ path, headers, body, onProgress }] = sent;
  assert.deepEqual([path, body, headers['X-CSRF-Token'], headers['Content-Type']], ['/api/run', file, SESSION.csrf, 'application/octet-stream']);
  // The answers to the routine questions go with the file, in one header, never in the URL.
  assert.equal(headers['X-PRS-Intake'], encodeIntake(answers));
  assert.match(renderDemo(client.state), /Uploading and starting…/);
  onProgress(250, 1000);
  assert.equal(client.state.uploaded, 25);
  assert.match(renderDemo(client.state), /Uploading… 25%.*<progress class="upload-progress" max="100" value="25"/s);
  onProgress(1000, 1000);
  assert.match(renderDemo(client.state), /Checking the file and starting…/);
  finish(reply({ error: 'The VCF contains an invalid record layout.' }, 400));
  assert.equal(await uploading, false);
  assert.equal(client.state.error, 'The VCF contains an invalid record layout.');
  assert.equal(client.state.uploaded, null);
  assert.doesNotMatch(renderDemo(client.state), /<progress/);
  assert.match(requestError(408), /The upload took too long/);
  assert.match(requestError(507), /enough space on the analysis server/);
  client.clear();
});

test('the upload transport is XMLHttpRequest, answering as fetch does and never following a redirect', async () => {
  const made = [];
  class FakeXHR {
    constructor() { this.headers = {}; this.upload = {}; made.push(this); }
    open(method, url) { Object.assign(this, { method, url }); }
    setRequestHeader(name, value) { this.headers[name] = value; }
    getResponseHeader(name) { return name === 'Retry-After' ? '7' : null; }
    send(body) { this.body = body; }
    abort() { this.onabort(); }
  }
  globalThis.XMLHttpRequest = FakeXHR;
  globalThis.location = { href: 'https://demo.example.org/' };
  try {
    const progress = [];
    const controller = new AbortController();
    const answered = xhrSend('/api/run', { headers: { 'X-CSRF-Token': 't' }, body: 'genome', signal: controller.signal, onProgress: (...a) => progress.push(a) });
    const [xhr] = made;
    assert.deepEqual([xhr.method, xhr.url, xhr.headers, xhr.body], ['POST', '/api/run', { 'X-CSRF-Token': 't' }, 'genome']);
    xhr.upload.onprogress({ lengthComputable: true, loaded: 3, total: 6 });
    Object.assign(xhr, { status: 201, responseText: '{"status":"running"}', responseURL: 'https://demo.example.org/api/run' });
    xhr.onload();
    const response = await answered;
    assert.deepEqual([response.ok, response.status, await response.json(), response.headers.get('Retry-After')], [true, 201, { status: 'running' }, '7']);
    assert.deepEqual(progress, [[3, 6]]);
    const redirected = xhrSend('/api/run', { headers: {}, body: 'genome', signal: controller.signal, onProgress() {} });
    Object.assign(made[1], { status: 200, responseText: '{}', responseURL: 'https://demo.example.org/elsewhere' });
    made[1].onload();
    await assert.rejects(redirected, TypeError);
    const aborted = xhrSend('/api/run', { headers: {}, body: 'genome', signal: controller.signal, onProgress() {} });
    controller.abort();
    await assert.rejects(aborted, { name: 'AbortError' });
  } finally {
    delete globalThis.XMLHttpRequest;
    delete globalThis.location;
  }
});

test('a delayed status reply cannot resurrect an answered question', async () => {
  const pending = deferred();
  let reads = 0;
  const question = { id: 'question-1', text: 'What do you want to explore?' };
  const { client, calls } = harness(async (path, options) => {
    if (options.method === 'POST') return reply({ ok: true });
    if (++reads === 1) return reply({ status: 'waiting', owned: true, question });
    return pending.promise;
  });
  await client.start();
  const poll = client.poll();
  assert.equal(await client.answer(question.id, 'Skin health'), true);
  pending.resolve(reply({ status: 'waiting', owned: true, question }));
  await poll;
  assert.equal(client.state.run.question, null);
  assert.equal(client.state.run.status, 'running');
  assert.match(client.state.notice, /Waiting for the analysis to continue/);
  const answer = calls.find(call => call.path === '/api/answer');
  assert.deepEqual(JSON.parse(answer.body), { question_id: 'question-1', text: 'Skin health' });
  assert.equal(answer.headers['X-CSRF-Token'], SESSION.csrf);
  client.clear();
});

test('a delayed pre-answer poll cannot restore waiting after an immediate terminal acknowledgement', async () => {
  const pending = deferred();
  let reads = 0;
  const waiting = { status: 'waiting', owned: true,
    question: { id: 'example-question', text: 'Continue?' } };
  const { client } = harness(async (_path, options) => {
    if (options.method === 'POST') return reply({ status: 'completed', owned: true });
    return ++reads === 1 ? reply(waiting) : pending.promise;
  });
  await client.start();
  const poll = client.poll();
  assert.equal(await client.answer(waiting.question.id, 'Yes'), true);
  assert.equal(client.state.notice, '');
  pending.resolve(reply(waiting));
  await poll;
  assert.equal(client.state.run.status, 'completed');
  assert.equal(client.state.notice, '');
  client.clear();
});

test('session expiration erases the report and discards an in-flight response', async () => {
  const pending = deferred();
  let reads = 0;
  const clock = { now: 1000 };
  const { client, timers, expirations } = harness(async () => ++reads === 1 ? reply({ status: 'completed', report: { secret: 'private genome result' } }) : pending.promise, clock);
  await client.start();
  const poll = client.poll();
  const expiry = [...timers.values()].find(timer => timer.delay === 3599000);
  clock.now = 3600 * 1000;
  expiry.fn();
  assert.equal(expirations(), 1);
  assert.equal(client.state.session, null);
  assert.equal(client.state.run, null);
  pending.resolve(reply({ status: 'completed', report: { secret: 'private genome result' } }));
  await poll;
  assert.equal(client.state.run, null);
  assert.doesNotMatch(renderDemo(client.state), /private genome result/);
});

test('past the daily password change the page stays while its demo is going or its result is there, and leaves once it is not', async () => {
  let clock = 1000;
  let expirations = 0;
  let run = { status: 'running', owned: true, progress: [] };
  const timers = new Map();
  let id = 0;
  const client = createDemoClient({
    now: () => clock, onExpire: () => expirations++,
    setTimer: (fn, delay) => { timers.set(++id, { fn, delay }); return id; }, clearTimer: tid => timers.delete(tid),
    fetchImpl: async path => path === '/api/session' ? reply(SESSION) : run ? reply(run) : reply({ error: 'Please log in again.' }, 401),
  });
  await client.start();
  clock = SESSION.expires_at * 1000 + 1;  // midnight in New York: the password changed
  [...timers.values()].find(timer => timer.delay === SESSION.expires_at * 1000 - 1000).fn();
  await client.poll();
  assert.equal(expirations, 0);
  assert.match(renderDemo(client.state, clock), /Your analysis is underway/);
  assert.match(renderDemo(client.state, clock), /signed in for this analysis/);
  run = { status: 'completed', owned: true, progress: [], report: {} };
  await client.poll();
  assert.equal(expirations, 0);
  assert.match(renderDemo(client.state, clock), /Your analysis is complete/);
  // Its result purged (or the demo ended): the server signs the session out, and the page leaves.
  run = null;
  await client.poll();
  assert.equal(expirations, 1);
  // Reopened after midnight, a session the server keeps for its demo opens the demo.
  run = { status: 'running', owned: true, progress: [] };
  const reopened = createDemoClient({
    now: () => clock, onExpire: () => expirations++, setTimer: () => 0, clearTimer: () => {},
    fetchImpl: async path => path === '/api/session' ? reply({ ...SESSION, kept_for_run: true }) : reply(run),
  });
  await reopened.start();
  assert.equal(expirations, 1);
  assert.equal(reopened.state.run.status, 'running');
  reopened.clear();
});

test('401 clears the session, and upload limits and throttling produce actionable errors', async () => {
  const unauthorized = harness(async () => reply({}, 401));
  await unauthorized.client.start();
  assert.equal(unauthorized.expirations(), 1);
  assert.equal(unauthorized.client.state.session, null);
  for (const status of [413, 429]) {
    const { client } = harness(async (_path, options) => options.method === 'POST' ? reply({ retry_after: 12 }, status) : reply({ status: 'idle' }));
    await client.start();
    assert.equal(await client.upload({ name: 'genome.txt' }), false);
    assert.match(client.state.error, status === 413 ? /is too large to upload or unpack/ : /wait 12 seconds/);
    assert.equal(client.state.run.status, 'idle');
    client.clear();
  }
});

test('only one mutation can be in flight and End demo drops all run data', async () => {
  const pending = deferred();
  const { client, calls } = harness(async (_path, options) => options.method === 'POST' ? pending.promise : reply({ status: 'running', owned: true, report: { private: 'data' } }));
  await client.start();
  const ending = client.end();
  assert.equal(await client.answer('id', 'answer'), false);
  assert.equal(calls.filter(call => call.method === 'POST').length, 1);
  pending.resolve(reply({ ok: true }));
  assert.equal(await ending, true);
  assert.deepEqual(client.state.run, { status: 'idle', owned: false });
  client.clear();
});

test('untrusted questions, progress and report prose are escaped; busy users cannot upload or end', () => {
  const unsafe = '<img src=x onerror=alert(1)>';
  const state = { ready: true, session: SESSION, run: { status: 'waiting', owned: true, question: { id: 'q', text: unsafe }, progress: [{ kind: 'stage_progress', stage: 'ancestry', text: unsafe }] } };
  const html = renderDemo(state);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.match(html, /answer-form/);
  const busy = renderDemo({ ...state, run: { status: 'busy', busy: true, owned: false } });
  assert.match(busy, /One analysis at a time/);
  assert.doesNotMatch(busy, /upload-form|answer-form|data-action="end"/);
  // The report stage's decision is its internal note: not shown at all, escaped or not.
  const report = reportView({ bundle: { report: { decision: unsafe } } });
  assert.doesNotMatch(report, /img src=x/);
});

test('a full demo says how many slots there are and when to come back, and keeps checking', async () => {
  const now = Date.UTC(2026, 9, 14, 18, 0);
  const full = { status: 'busy', busy: true, owned: false, full: true, slots: 2, retry_at: now / 1000 + 42 * 60 };
  const html = renderDemo({ ready: true, session: SESSION, run: full }, now);
  const at = new Date(full.retry_at * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  assert.match(html, /All 2 places are taken/);
  assert.ok(html.includes(`Please try again around ${at} (in about 42 minutes).`));
  assert.match(html, /shows the upload form when there’s room for another analysis/);
  assert.doesNotMatch(html, /upload-form|answer-form|data-action="end"/);
  assert.match(renderDemo({ ready: true, session: SESSION, run: { ...full, slots: 1 } }, now), /One analysis at a time/);
  assert.equal(retryText(now / 1000 + 125 * 60, now).endsWith('(in about 2 h 5 min).'), true);
  assert.equal(retryText(null, now), 'Please try again later.');
  assert.match(requestError(409, { full: true, retry_at: null }), /^There’s no room for another analysis right now\. Please try again later\.$/);
  // An upload refused as full shows that, then the next poll shows the card.
  const { client, timers } = harness(async (_path, options) => options.method === 'POST' ? reply({ error: 'All demo slots are in use.', full: true, retry_at: null }, 409) : reply(full));
  await client.start();
  assert.equal(await client.upload({ name: 'genome.txt' }), false);
  assert.match(client.state.error, /There’s no room for another analysis right now/);
  assert.equal([...timers.values()].at(-1).delay, 2000);
  client.clear();
});

test('the upload card says where the file goes, which is not the computer serving the page', () => {
  const html = renderDemo({ ready: true, session: SESSION, run: { status: 'idle', owned: false } });
  assert.match(html, /It goes to the lab’s analysis server\./);
  // Whole genomes too, and what they cost; not exomes, which normalize.py cannot read yet (it
  // needs the capture regions), so the card does not invite a file that can only be refused.
  assert.match(html, /a VCF from a whole genome, or a Complete Genomics var file; plain, or in ZIP, gzip or bzip2/);
  assert.match(html, /Exomes can’t be analysed yet\./);
  assert.doesNotMatch(html, /this VM/i);
  // Whole-genome limits read in GiB.
  const limits = { upload_bytes: 1024 ** 3, expanded_bytes: 8 * 1024 ** 3 };
  assert.match(renderDemo({ ready: true, session: { ...SESSION, limits }, run: { status: 'idle', owned: false } }), /Upload limit: 1 GiB\. Size limit after unpacking: 8 GiB\./);
});

test('signed-in people can run each sample here or download it, escaped and only plain names', () => {
  const samples = [{ name: 'sample-genome-1.txt', bytes: 15703366 }, { name: 'sample-genome-10.zip', bytes: 8247233 },
    { name: '../escape.txt', bytes: 3 }, { name: '<b>x</b>', bytes: 3 }, { name: 'none.txt', bytes: 0 }];
  const html = renderDemo({ ready: true, session: { ...SESSION, samples }, run: { status: 'idle', owned: false } });
  // Each good sample: a "Use this sample" button that starts a run on it, plus a download link.
  assert.match(html, /data-action="use-sample" data-sample="sample-genome-1\.txt"[^>]*>Use this sample/);
  assert.match(html, /<a class="sample-download" href="\/samples\/sample-genome-1\.txt" download>/);
  assert.match(html, /data-action="use-sample" data-sample="sample-genome-10\.zip"/);
  assert.equal((html.match(/data-action="use-sample"/g) || []).length, 2);
  assert.equal((html.match(/class="sample-download"/g) || []).length, 2);
  assert.doesNotMatch(html, /escape|<b>|none\.txt/);
  assert.ok(html.indexOf('Show sample files') < html.indexOf('id="upload-form"'));
  // While an analysis is pending or new runs are paused, the buttons are disabled.
  assert.match(renderDemo({ ready: true, session: { ...SESSION, samples }, run: { status: 'idle', owned: false, paused: 'x' } }),
    /data-action="use-sample"[^>]*disabled/);
  assert.doesNotMatch(renderDemo({ ready: true, session: SESSION, run: { status: 'idle', owned: false } }), /Show sample files/);
});

test('while no analysis can start, the upload form says so before a file is chosen and cannot be sent', () => {
  const said = 'The demo can’t start a new analysis right now because of a problem on its side. Please try again later.';
  const html = renderDemo({ ready: true, session: SESSION, run: { status: 'idle', owned: false, paused: 'server text' } });
  assert.ok(html.includes(`<div class="paused-notice" role="status"><strong>New analyses are paused</strong><p>${said} This page checks again by itself.</p></div>`));
  assert.ok(html.indexOf('paused-notice') < html.indexOf('id="upload-form"'));
  assert.match(html, /<button class="button wide" id="upload-submit" type="submit" disabled>/);
  assert.doesNotMatch(html, /server text/);
  const open = renderDemo({ ready: true, session: SESSION, run: { status: 'idle', owned: false } });
  assert.doesNotMatch(open, /paused-notice|id="upload-submit" type="submit" disabled/);
  assert.equal(requestError(503, { paused: true, error: 'server text' }), said);
});

test('a run whose report scored never ends on a refusal, even with a stale refusal event', () => {
  const refusal = { kind: 'refusal', stage: 'score', headline: 'This run ended in a refusal.', detail: 'The run refused at the score stage and did not reach report.' };
  const html = reportView({ bundle: { report: { outcome: 'score', decision: 'Delivered.' } }, refusal });
  assert.doesNotMatch(html, /ended in a refusal|did not reach report/);
  assert.match(html, /A complete score report is not available/);
  // A run that did refuse upstream, without a report, shows that refusal (this used to throw).
  assert.match(reportView({ bundle: { score: { outcome: 'refuse' } }, refusal }), /This run ended in a refusal\./);
  // Without the event's words it says so, and never shows the judgment's notes as if to the person.
  const bare = reportView({ bundle: { score: { outcome: 'refuse', decision: 'Told them coverage was too low.' } } });
  assert.match(bare, /A complete score report is not available/);
  assert.doesNotMatch(bare, /Told them/);
});

test('a visitor sees the genetic score heading, never the report stage\'s note or reasons, anywhere on the page', () => {
  // Synthetic, as contracts.test.js's example bundle: not data, never shown to anyone.
  const note = 'Delivered the scored result with care; the person confirmed understanding.';
  const judgment = outcome => ({ outcome, decision: note, reasons: ['An internal reason.'], evidence: [{ source: 'example/test-artifact.json', field: 'example' }], requirement_ids: ['EXAMPLE-1'] });
  const bundle = {
    score: { ...judgment('score'), raw_score: 0.25, model: 'PGS000000' },
    coverage: { tool: 'weight_coverage.py measure', resolved_weight_fraction: 0.97 },
    interpret: { ...judgment('score'), percentile_range: [20.77, 93.85], z_score: 0.25, caveats_for_report: ['INT-4: Example internal caveat.'] },
    percentile_range: { tool: 'rank_range.py', n: 3, n_below: 2, n_equal: 0, n_above: 1, ties: 'half', level: 0.95, percentile_range: [20.77, 93.85], z_score: { value: 0.25, patient_raw_score: 0.25, mean: 0, sd: 1, sd_ddof: 1, n: 3 } },
    reference_response: { status: 'reference_valid', interpret_can_proceed: true, warnings_for_report: [], reference: {
      usable: true, source: 'Example reference', version_string: 'example-version', ancestry_grouping_definition: 'Example grouping',
      cohort_construction_rule: 'Example rule', n_participants: 3, too_few_threshold_used: 2, assay_layer: 'Example assay',
      scoring_parity: 'same-resolved-set', distribution: { raw_scores: [-1, 0, 1], mean: 0, sd: 1, n: 3 } } },
    report: { ...judgment('score'), delivered: { summary: 'Example short summary from the report.', limits: ['Example plain limit.'], score: 0.25, placement: { percentile_range: [21, 94], z_score: 0.25 }, resolved_weight_fraction: 0.97, personalized_limit: 'Example limit.', disclaimer: 'Example disclaimer.' } },
  };
  const scored = reportView({ bundle });
  assert.match(scored, /<h1 class="result-title">Your genetic score<\/h1>/);
  // A range, never one number.
  assert.match(scored, /<p class="result-sentence">You scored higher than about <span class="result-range">21 to 94<\/span> out of every 100 people in your comparison group\.<\/p>/);
  const incomplete = reportView({ bundle: { report: judgment('score') } });
  assert.match(incomplete, /A complete score report is not available/);
  const page = renderDemo({ ready: true, session: SESSION, run: { status: 'completed', owned: true, report: { bundle },
    progress: [{ kind: 'stage_completed', stage: 'report', outcome: 'score', text: note }] } });
  assert.match(page, /Your report is ready\./);
  for (const html of [scored, incomplete, page]) assert.doesNotMatch(html, /confirmed understanding|An internal reason|INT-4/);
});

test('report rendering preserves a real refusal and does not manufacture a percentile from an incomplete score', () => {
  const refusal = reportView({ bundle: { report: exampleRefusal } });
  assert.match(refusal, /What was tried/);
  assert.match(refusal, /What would change it/);
  const incomplete = reportView({ bundle: { report: { outcome: 'score', delivered: { placement: 99.123 } } } });
  assert.match(incomplete, /complete score report is not available/);
  assert.doesNotMatch(incomplete, /99\.123/);
});

test('terminal progress renders final judgments after bookkeeping and a truncated event feed', () => {
  const judgments = {
    classification: { outcome: 'proceed' }, ancestry: { outcome: 'proceed' },
    model_choice: { outcome: 'proceed' }, score: { outcome: 'score' },
    interpret: { outcome: 'score' }, report: { outcome: 'score' },
  };
  const progress = [
    { kind: 'stage_started', stage: 'door' },
    { kind: 'stage_progress', stage: 'door', text: 'synthetic provenance recorded' },
    { kind: 'run_completed', outcome: 'score' },
  ];
  const html = renderDemo({ ready: true, session: SESSION,
    run: { status: 'completed', owned: true, progress, report: { bundle: judgments } } });
  const timeline = html.match(/<ol class="timeline">(.*?)<\/ol>/s)[1];
  assert.equal((timeline.match(/<p>Complete<\/p>/g) || []).length, 6);
  assert.doesNotMatch(timeline, /In progress|Not started/);
  assert.match(html, /synthetic provenance recorded/);
});

test('End demo respects a newly busy slot, and terminal refusal events retain their delivered explanation', async () => {
  const busy = { status: 'busy', busy: true, owned: false };
  const { client } = harness(async (_path, options) => options.method === 'POST' ? reply(busy) : reply({ status: 'completed', owned: true }));
  await client.start();
  assert.equal(await client.end(), true);
  assert.deepEqual(client.state.run, busy);
  assert.doesNotMatch(renderDemo(client.state), /upload-form/);
  const html = reportView({ refusal: { kind: 'refusal', headline: 'No useful comparison is available.', detail: 'Your file was read, but the comparison data does not support a score.', is_about_the_person: false } });
  assert.match(html, /Your file was read/);
  assert.match(html, /not a finding about your body/);
  client.clear();
});
