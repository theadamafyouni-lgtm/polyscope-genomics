import test from 'node:test';
import assert from 'node:assert/strict';
import { fixtures, FIXED_NOW } from './fixtures.mjs';
import { timingInfo, stageInfo, timelineView, reportContent, questionContent } from './presentation.mjs';

test('time estimates use only a retained run start and never invent progress', () => {
  assert.equal(timingInfo(fixtures.running, FIXED_NOW).primary, 'About 32–62 min left');
  assert.equal(timingInfo(fixtures.running, FIXED_NOW).elapsed, '38 min elapsed');
  const noStart = { ...fixtures.running, progress: fixtures.running.progress.slice(1) };
  assert.match(timingInfo(noStart, FIXED_NOW).caption, /Remaining time is not available/);
  assert.equal(timingInfo(fixtures['waiting-answer'], FIXED_NOW).primary, 'Waiting for you');
  assert.equal(timingInfo({ ...fixtures['waiting-answer'], progress: [] }, FIXED_NOW).primary, 'Waiting for you');
  assert.equal(timingInfo(fixtures.running, FIXED_NOW + 100 * 60000).primary, 'Taking longer than usual');
  assert.equal(stageInfo(fixtures.running).current.id, 'score');
  assert.equal(stageInfo(fixtures['waiting-answer']).current.id, 'select');
  assert.equal(stageInfo(fixtures['waiting-answer']).current.status, 'ask');
});

test('stage completion depends on recorded outcomes, including refusal and unknown', () => {
  const run = { status: 'running', progress: [{ kind: 'stage_started', stage: 'report' }] };
  assert.equal(stageInfo(run).stages[0].status, 'pending');
  const refusal = stageInfo(fixtures['finished-refusal']);
  assert.equal(refusal.stages.at(-1).status, 'refuse');
  assert.equal(refusal.current, null);
  assert.match(timelineView(fixtures['finished-refusal']), /No score/);
});

test('report presentation preserves exact values, delivered prose and all warnings', () => {
  const report = structuredClone(fixtures['finished-score'].report);
  // Retain every recorded digit when the interpretation delivers the full artifact range.
  const recordedRange = report.bundle.percentile_range.percentile_range;
  report.bundle.report.delivered.placement.percentile_range = [...recordedRange];
  report.bundle.interpret.percentile_range = [...recordedRange];
  const html = reportContent(report);
  assert.match(html, /20\.7655–93\.8492<span class="result-unit">percentile range<\/span>/);
  assert.doesNotMatch(html, /undefined/);
  assert.match(html, /0\.97/);
  assert.match(html, /0\.25/);
  for (const value of ['Example personalized limit.', 'Example disclaimer: not a diagnosis.', 'Example adapter warning.', 'Example interpretation caveat.']) assert.ok(html.includes(value));
  assert.doesNotMatch(html, /Example test judgment|Example test reason/);
  assert.ok(html.indexOf('Your result') < html.indexOf('What this answer is based on'));
  assert.ok(html.indexOf('What this answer is based on') < html.indexOf('What this can tell you'));
});

test('inconsistent reports cannot display a percentile range and stale refusals cannot replace a score', () => {
  const report = structuredClone(fixtures['finished-score'].report);
  report.refusal = { headline: 'Stale refusal', detail: 'Must not be displayed.' };
  assert.doesNotMatch(reportContent(report), /Stale refusal/);
  report.bundle.report.delivered.placement.percentile_range = [99.123, 100];
  const html = reportContent(report);
  assert.match(html, /complete score report is not available/);
  assert.doesNotMatch(html, /99\.123|Stale refusal/);
});

test('refusal envelope is unchanged and hostile run text is escaped', () => {
  const report = fixtures['finished-refusal'].report;
  const html = reportContent(report);
  for (const value of Object.values(report.bundle.report.refusal_envelope).filter(value => typeof value === 'string')) assert.ok(html.includes(value));
  assert.match(questionContent({ text: '<img src=x onerror=alert(1)>' }), /&lt;img/);
  assert.doesNotMatch(questionContent({ text: '<img src=x onerror=alert(1)>' }), /<img/);
});
