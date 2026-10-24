import test from 'node:test';
import assert from 'node:assert/strict';
import { buildReportTopics } from './exploration.js';
import { exampleRefusal } from './examples.js';

// Example prose fixtures only. No genetic measurements are created here.
const scoreReport = () => ({ outcome: 'score', delivered: { personalized_limit: 'Example recorded limit for this result.' } });
const selection = () => ({
  chosen_model: 'PGS000000',
  candidates_considered: [
    { pgs_id: 'PGS000001', why_ranked_here: 'Example unused reasoning.', cost_of_choosing_it: 'Example unused cost.' },
    { pgs_id: 'PGS000000', why_ranked_here: 'Example chosen reasoning.\nKept as recorded.', cost_of_choosing_it: 'Example chosen cost.' },
  ],
});
const find = (topics, id) => topics.find(value => value.id === id);

test('missing, malformed and non-final reports have no exploration topics', () => {
  for (const input of [undefined, null, [], 'score', {}, { kind: 'score' }, { kind: 'score', report: null },
    { kind: 'score', report: {} }, { kind: 'score', report: { outcome: 'score', delivered: [] } },
    { kind: 'score', report: { outcome: 'score', delivered: { personalized_limit: { toString: null } } } },
    { kind: 'refuse', report: { outcome: 'refuse' } }, { kind: 'ask', report: { outcome: 'ask' } },
    { kind: 'score', report: exampleRefusal }, { kind: 'refuse', report: scoreReport() }]) {
    assert.deepEqual(buildReportTopics(input), []);
  }
});

test('percentile explanation keeps probability and estimate limits together without inventing numbers', () => {
  const topics = buildReportTopics({ kind: 'score', report: scoreReport() });
  const percentile = find(topics, 'percentile');
  assert.equal(percentile.sourceLabel, 'General explanation');
  assert.match(percentile.paragraphs.join(' '), /not the probability/);
  assert.match(percentile.paragraphs.join(' '), /estimate.*can move/);
  assert.doesNotMatch(percentile.paragraphs.join(' '), /\d/);
  assert.deepEqual(topics.map(value => value.id), ['percentile', 'model', 'limits', 'support']);
});

test('model topic preserves chosen reasoning and cost, never borrowing a different candidate', () => {
  const modelChoice = selection();
  const topic = find(buildReportTopics({ kind: 'score', report: scoreReport(), modelChoice }), 'model');
  assert.deepEqual(topic.paragraphs, [modelChoice.candidates_considered[1].why_ranked_here, modelChoice.candidates_considered[1].cost_of_choosing_it]);
  assert.equal(topic.sourceLabel, 'From this report');
  assert.doesNotMatch(JSON.stringify(topic), /unused/);
});

test('missing or malformed model choices disclose unavailable reasoning', () => {
  for (const modelChoice of [null, {}, [], { chosen_model: { toString: null }, candidates_considered: [] },
    { chosen_model: 'PGS000000', candidates_considered: {} },
    { chosen_model: 'PGS000002', candidates_considered: selection().candidates_considered },
    { chosen_model: 'PGS000000', candidates_considered: [null, [], { pgs_id: 'PGS000000', why_ranked_here: 'Incomplete example reasoning.' }] }]) {
    const topic = find(buildReportTopics({ kind: 'score', report: scoreReport(), modelChoice }), 'model');
    assert.equal(topic.sourceLabel, 'General explanation');
    assert.match(topic.title, /unavailable/);
    assert.match(topic.paragraphs[0], /not included/);
    assert.doesNotMatch(JSON.stringify(topic), /unused|Incomplete example reasoning/);
  }
});

test('limits carry exact personalized wording and every distinct textual warning without mutation', () => {
  const report = scoreReport();
  const warnings = ['Example assay warning.', 'Example portability warning.\nSecond line.', '', null, { toString: null }, 'Example assay warning.'];
  const before = JSON.stringify({ report, warnings });
  const topic = find(buildReportTopics({ kind: 'score', report, warnings }), 'limits');
  assert.deepEqual(topic.paragraphs, [report.delivered.personalized_limit, warnings[0], warnings[1]]);
  assert.equal(topic.sourceLabel, 'From this report');
  assert.equal(JSON.stringify({ report, warnings }), before);
  for (const value of [null, {}, 'Example warning']) {
    assert.deepEqual(find(buildReportTopics({ kind: 'score', report, warnings: value }), 'limits').paragraphs, [report.delivered.personalized_limit]);
  }
});

test('refusal exploration preserves direct envelope delivery and never surfaces score context', () => {
  const report = structuredClone(exampleRefusal);
  report.delivered.personalized_limit = 'Example stale score limit must stay hidden.';
  const topics = buildReportTopics({ kind: 'refuse', report, modelChoice: selection(), warnings: ['Example stale score warning.'] });
  assert.deepEqual(find(topics, 'why').paragraphs, [report.refusal_envelope.what_was_tried, report.refusal_envelope.why_this_is_honest]);
  assert.deepEqual(find(topics, 'change').paragraphs, [report.refusal_envelope.what_would_change_it]);
  assert.equal(find(topics, 'why').sourceLabel, 'From this report');
  assert.equal(find(topics, 'change').sourceLabel, 'From this report');
  assert.match(find(topics, 'about').paragraphs.join(' '), /not a finding about your body/);
  assert.deepEqual(topics.map(value => value.id), ['why', 'change', 'about', 'support']);
  assert.doesNotMatch(JSON.stringify(topics), /stale score|chosen reasoning|position.*probability/i);
});

test('a refusal scope statement requires the actual false flag', () => {
  const report = structuredClone(exampleRefusal);
  report.refusal_envelope.is_about_the_person = true;
  assert.equal(find(buildReportTopics({ kind: 'refuse', report }), 'about'), undefined);
  for (const value of [undefined, 'false', null, 0]) {
    report.refusal_envelope.is_about_the_person = value;
    assert.deepEqual(buildReportTopics({ kind: 'refuse', report }), []);
  }
});

test('partial refusal envelopes cannot become a complete explanation', () => {
  for (const field of ['headline', 'detail', 'what_was_tried', 'why_this_is_honest', 'what_would_change_it']) {
    const report = structuredClone(exampleRefusal);
    report.refusal_envelope[field] = [];
    assert.deepEqual(buildReportTopics({ kind: 'refuse', report }), [], field);
  }
});

test('every example topic keeps the example label for direct and general content', () => {
  for (const input of [
    { kind: 'score', report: scoreReport() },
    { kind: 'score', report: scoreReport(), modelChoice: selection() },
    { kind: 'refuse', report: exampleRefusal },
  ]) {
    const topics = buildReportTopics({ ...input, example: true });
    assert.ok(topics.length);
    for (const topic of topics) {
      assert.match(topic.sourceLabel, /^Example · (From this report|General explanation)$/);
      assert.ok(topic.label && topic.title && topic.suggestedQuestion);
      assert.ok(topic.paragraphs.length && topic.paragraphs.every(paragraph => typeof paragraph === 'string'));
    }
  }
});

test('support provides a contact step and leaves personal health decisions to a clinician', () => {
  for (const input of [{ kind: 'score', report: scoreReport() }, { kind: 'refuse', report: exampleRefusal }]) {
    const topic = find(buildReportTopics(input), 'support');
    const content = topic.paragraphs.join(' ');
    assert.equal(topic.sourceLabel, 'General explanation');
    assert.match(content, /clinician or genetic counselor/);
    assert.match(content, /appointment request/);
    assert.match(content, /chance of developing a condition, medication, or screening belong in that conversation/);
    assert.match(content, /cannot answer them for you/);
    assert.doesNotMatch(content, /you should (?:start|stop|take|get screened)|recommend (?:medication|screening)/i);
  }
});
