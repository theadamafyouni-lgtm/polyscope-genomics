import test from 'node:test';
import assert from 'node:assert/strict';
import { recordedTraitName, renderPage, reportView, stageInfo, timingInfo } from './workspace.js';
import { getDisplayResult } from './contracts.js';
import { fixtureState, FIXTURE_NOW } from './visual/fixtures.mjs';

// These are synthetic interface fixtures, never a retained person's result.
const ESTIMATE = { low_minutes: 70, median_minutes: 85, high_minutes: 100, runs: 10 };
const startedRun = (elapsed, extra = {}) => ({ status: 'running', owned: true, kind: 'chip', estimate: ESTIMATE,
  progress: [{ kind: 'run_started', ts: new Date(FIXTURE_NOW - elapsed * 60_000).toISOString() },
    { kind: 'stage_started', stage: 'score' }], ...extra });
const renderRun = run => renderPage({ ...fixtureState('running'), run }, FIXTURE_NOW);
const scoreReport = () => fixtureState('finished-score').run.report;
const fullReport = html => {
  const match = html.match(/<details\b[^>]*\bid="full-report-details"[^>]*>([\s\S]*?)<\/details>/);
  assert.ok(match, 'The full report is a native disclosure.');
  assert.doesNotMatch(match[0].slice(0, match[0].indexOf('>')), /\bopen(?:\s|=|>)/);
  return { content: match[1], outside: html.replace(match[0], ''), index: match.index };
};

test('trait headings use the exact explicit request label when recorded identities agree', () => {
  for (const choice of ['EXAMPLE_1000', [{ id: 'EXAMPLE_1000', label: 'Example choice wording' }]]) {
    const bundle = {
      model_choice: { trait_id: choice },
      reference_response: {
        request_echo: { trait: { id: [{ id: 'EXAMPLE_1000', label: 'Example catalog wording' }], label: 'Example recorded trait' } },
        reference: { trait: { id: 'EXAMPLE_1000', label: 'Example reference wording' } },
      },
    };
    const before = structuredClone(bundle);
    assert.equal(recordedTraitName(bundle), 'Example recorded trait');
    assert.deepEqual(bundle, before);
  }
  assert.equal(recordedTraitName({ reference_response: { request_echo: { trait: { label: '  Example exact wording  ' } } } }), '  Example exact wording  ');
});

test('trait headings can use one catalog label or the reference label without inferring a name', () => {
  const bundle = {
    stages: { select: { trait_id: 'EXAMPLE_1000' } },
    reference_response: {
      request_echo: { trait: { id: [{ id: 'EXAMPLE_1000', label: 'Example catalog trait' }], label: '  ' } },
      reference: { trait: { id: 'EXAMPLE_1000', label: 'Example reference trait' } },
    },
  };
  assert.equal(recordedTraitName(bundle), 'Example catalog trait');
  delete bundle.reference_response.request_echo.trait.id[0].label;
  assert.equal(recordedTraitName(bundle), 'Example reference trait');
  bundle.reference_response.reference.trait = { id: [{ id: 'EXAMPLE_1000', label: 'Example reference catalog trait' }] };
  assert.equal(recordedTraitName(bundle), 'Example reference catalog trait');
  bundle.stages.select.trait_id = 'EXAMPLE_2000';
  assert.equal(recordedTraitName(bundle), null);
});

test('conflicting, ambiguous and malformed trait identities suppress a supplied name', () => {
  const variants = [
    { model_choice: { trait_id: 'EXAMPLE_2000' } },
    { model_choice: { trait_id: [{ id: 'EXAMPLE_1000' }, { id: 'EXAMPLE_1000' }] } },
    { model_choice: { trait_id: 1000 } },
    { reference: { trait: { id: 'EXAMPLE_2000', label: 'Example other trait' } } },
  ];
  for (const { model_choice, reference } of variants) {
    assert.equal(recordedTraitName({ model_choice, reference_response: {
      request_echo: { trait: { id: 'EXAMPLE_1000', label: 'Example supplied trait' } }, reference,
    } }), null);
  }
  for (const id of [[], [{ id: 'EXAMPLE_1000' }, { id: 'EXAMPLE_2000' }], [{}], [{ id: 1000 }], '', ' ', 1000, { id: 'EXAMPLE_1000' }]) {
    assert.equal(recordedTraitName({ reference_response: { request_echo: { trait: { id, label: 'Example supplied trait' } } } }), null);
  }
});

test('missing trait labels are not fabricated from the model, free text or unrelated metadata', () => {
  const bundle = scoreReport().bundle;
  assert.equal(recordedTraitName(bundle), null);
  bundle.model_choice = { chosen_model: 'PGS000000', trait_id: 'EXAMPLE_1000', decision: 'Example prose names an unrelated trait.', trait_resolution: 'Example inferred wording.' };
  bundle.reference_response.request_echo = {
    trait: { id: [{ id: 'EXAMPLE_1000', description: 'Example description that is not a label.' }] },
    model: { pgs_id: 'PGS000000', header_verbatim: ['#trait_reported=Example header prose'] },
  };
  bundle.report.delivered.summary = 'Example summary mentions a trait.';
  assert.equal(recordedTraitName(bundle), null);
  for (const label of [null, false, 123, {}, [], '', '\t\n']) {
    bundle.reference_response.request_echo.trait.label = label;
    assert.equal(recordedTraitName(bundle), null);
  }
});

test('the accepted result has a trait heading with escaped recorded text or an honest missing-name notice', () => {
  const report = scoreReport();
  assert.equal(getDisplayResult(report.bundle).kind, 'score');
  const unnamed = reportView(report);
  assert.match(unnamed, /<h1 class="result-title">Your genetic score<\/h1>/);
  assert.match(unnamed, /class="trait-unavailable"/);
  const unsafe = 'example <trait> & "quoted name"';
  report.bundle.reference_response.request_echo = { trait: { id: 'EXAMPLE_1000', label: unsafe } };
  assert.equal(getDisplayResult(report.bundle).kind, 'score');
  const html = reportView(report);
  assert.match(html, /<h1 class="result-title">Example &lt;trait&gt; &amp; &quot;quoted name&quot;: your genetic score<\/h1>/);
  assert.doesNotMatch(html, /<trait>|class="trait-unavailable"/);
  assert.ok(html.indexOf('result-title') < html.indexOf('result-sentence'));
  assert.equal(recordedTraitName(report.bundle), unsafe);
});

test('a trait name cannot turn an incomplete or refused report into a score result', () => {
  const report = scoreReport();
  report.bundle.reference_response.request_echo = { trait: { id: 'EXAMPLE_1000', label: 'Example gated trait' } };
  report.bundle.report.delivered.score = 987.654321;
  assert.equal(getDisplayResult(report.bundle).kind, 'incomplete');
  assert.doesNotMatch(reportView(report), /Example gated trait|result-title|result-sentence/);
  const refusal = fixtureState('finished-refusal').run.report;
  refusal.bundle.reference_response = { request_echo: { trait: { label: 'Example gated trait' } } };
  assert.doesNotMatch(reportView(refusal), /Example gated trait|result-title|result-sentence/);
});

test('elapsed progress expresses elapsed time against the estimate, never measured work', () => {
  const run = startedRun(38);
  assert.deepEqual(timingInfo(run, FIXTURE_NOW), {
    elapsed: 38, worked: 38, range: { low: 70, high: 100 }, primary: 'About 32–62 min left',
    caption: 'An estimate from recent genotype-file runs, which took 70–100 minutes. Questions can add time.',
  });
  const html = renderRun(run);
  assert.match(html, /<progress max="100" value="38"/);
  assert.match(html, /aria-valuetext="38 minutes elapsed\. Time passed, not work completed\."/);
  assert.match(html, /Time passed, not work completed/);
  assert.doesNotMatch(html, /38%|38 percent/);
});

test('missing, invalid and future start times never manufacture remaining time or a progress amount', () => {
  for (const progress of [[], [{ kind: 'stage_started', stage: 'score' }],
    [{ kind: 'run_started' }], [{ kind: 'run_started', ts: 'not-a-date' }],
    [{ kind: 'run_started', ts: Infinity }],
    [{ kind: 'run_started', ts: new Date(FIXTURE_NOW + 60_000).toISOString() }]]) {
    const run = { status: 'running', owned: true, progress, estimate: ESTIMATE };
    assert.equal(timingInfo(run, FIXTURE_NOW).elapsed, null);
    const html = renderRun(run);
    assert.match(html, /Start time is unavailable/);
    assert.match(html, /Usually 70–100 minutes/);
    assert.doesNotMatch(html, /<progress|min left|min elapsed/);
  }
});

test('numeric seconds and zero starts remain valid; overlong runs never imply completion', () => {
  const epochRun = { status: 'running', events: [{ kind: 'run_started', ts: 0 }] };
  assert.equal(timingInfo(epochRun, 38 * 60_000).elapsed, 38);
  const run = { ...startedRun(0), progress: [{ kind: 'run_started', ts: FIXTURE_NOW / 1000 }] };
  assert.equal(timingInfo(run, FIXTURE_NOW).elapsed, 0);
  assert.equal(timingInfo(startedRun(70), FIXTURE_NOW).primary, 'About up to 30 min left');
  for (const elapsed of [100, 127, 240]) {
    const html = renderRun(startedRun(elapsed));
    assert.match(html, /Taking longer than usual/);
    assert.match(html, /<progress max="100" value="100"/);
    assert.ok(html.includes(`aria-valuetext="${elapsed} minutes elapsed.`));
    assert.doesNotMatch(html, /Your analysis is complete|0 min left|100%/);
    assert.equal(stageInfo(startedRun(elapsed)).current.status, 'running');
  }
});

test('a real question takes priority over the time estimate and marks its reported step', () => {
  const question = { id: 'synthetic-question', stage: 'select', text: 'Example question?', why_it_matters: 'Example explanation.' };
  const run = startedRun(38, { status: 'waiting', question });
  assert.equal(timingInfo(run, FIXTURE_NOW).primary, 'Waiting for your answer');
  const html = renderRun(run);
  assert.match(html, /id="live-question">Example question\?/);
  assert.match(html, /<form id="answer-form"[^>]*aria-labelledby="live-question"/);
  assert.match(html, /<textarea[^>]*aria-describedby="answer-help"/);
  assert.match(html, /Waiting for your answer/);
  assert.doesNotMatch(html, /32–62 min left/);
  assert.ok(html.indexOf('id="live-question"') < html.indexOf('id="analysis-step"'));
  assert.equal(stageInfo(run).current.id, 'select');
  assert.equal(stageInfo(run).current.status, 'ask');
  assert.match(html, /dna-helix-waiting/);
});

test('unknown and unreported steps remain unknown instead of becoming successful', () => {
  const empty = { status: 'running', owned: true, progress: [] };
  assert.equal(stageInfo(empty).current, null);
  assert.ok(stageInfo(empty).stages.every(stage => stage.status === 'pending'));
  assert.equal((renderRun(empty).match(/<p>Not yet reported<\/p>/g) || []).length, 6);
  const unknown = { ...empty, progress: [{ kind: 'stage_completed', stage: 'score', outcome: 'unrecognized' }] };
  assert.equal(stageInfo(unknown).current.status, 'unknown');
  assert.match(renderRun(unknown), /Waiting for the next step/);
  assert.match(renderRun(unknown), /Status not available/);
  const terminal = { ...empty, status: 'completed', progress: [{ kind: 'stage_started', stage: 'score' }] };
  assert.equal(stageInfo(terminal).current, null);
  assert.equal(stageInfo(terminal).stages.find(stage => stage.id === 'score').status, 'unknown');
});

test('technical notes stay in a native disclosure and do not replace the plain step description', () => {
  const run = startedRun(38);
  run.progress.push({ kind: 'stage_progress', stage: 'score', text: 'synthetic-tool: --technical-argument' });
  const html = renderRun(run);
  assert.match(html, /Calculating the score using the chosen method\./);
  const notes = html.match(/<details id="technical-notes"[^>]*>(.*?)<\/details>/s)?.[1];
  assert.ok(notes);
  assert.match(notes, /<summary>Show step details<\/summary>/);
  assert.match(notes, /synthetic-tool: --technical-argument/);
  assert.doesNotMatch(html.replace(notes, ''), /synthetic-tool: --technical-argument/);
});

test('offline rendering retains the last confirmed stage and question without inventing new work', () => {
  const state = fixtureState('waiting-answer');
  state.error = 'Example disconnected transport.';
  const before = structuredClone(state);
  const html = renderPage(state, FIXTURE_NOW);
  assert.match(html, /role="alert"/);
  assert.match(html, /Last confirmed update/);
  assert.match(html, /data-action="retry"/);
  assert.match(html, /id="answer-form"/);
  assert.match(html, /dna-helix-waiting/);
  assert.deepEqual(state, before);
});

test('scored reports retain every recorded numeric value, and the person reads the report\'s own plain words', () => {
  const report = scoreReport();
  const bundle = report.bundle;
  const score = 0.25000000000000006, percentile = 60.1234567890123, coverage = 0.9700000000000001;
  bundle.score.raw_score = bundle.report.delivered.score = score;
  bundle.interpret.percentile_range = bundle.percentile_range.percentile_range = [percentile, 93.85];
  bundle.report.delivered.placement.percentile_range = [60, 94];
  bundle.coverage.resolved_weight_fraction = bundle.report.delivered.resolved_weight_fraction = coverage;
  assert.equal(getDisplayResult(bundle).kind, 'score');
  const before = structuredClone(report);
  const html = reportView(report);
  for (const value of [score, percentile, coverage]) assert.ok(html.includes(String(value)), String(value));
  // The z-score as the report delivered it, in the same group as the range.
  assert.match(html, /<dt>z-score in this group<\/dt><dd>0\.25<\/dd>/);
  // The person reads the report's own plain words: its summary first, part by part, then its
  // limits. The tools' warnings and interpret's caveats are the record's, written with internal codes.
  const summary = ['trait', 'where', 'comparison', 'not_a_chance'].map(key => bundle.report.delivered.summary_parts[key]);
  for (const value of [...summary, bundle.report.delivered.personalized_limit, bundle.report.delivered.disclaimer,
    ...bundle.report.delivered.limits]) assert.ok(html.includes(value), value);
  for (const value of [...bundle.interpret.caveats_for_report, ...bundle.reference_response.warnings_for_report]) assert.ok(!html.includes(value), value);
  assert.ok(html.indexOf(summary[0]) < html.indexOf('reference-chart'));
  assert.match(html, /data-chart-kind="people"/);
  assert.match(html, /Out of 3 people in your comparison group/);
  assert.ok(html.indexOf('result-sentence') < html.indexOf('reference-chart'));
  assert.ok(html.indexOf('reference-chart') < html.indexOf('report-limits'));
  assert.deepEqual(report, before);
});

test('invalid or inconsistent score bundles suppress the number and comparison chart', () => {
  for (const mutate of [
    bundle => { bundle.report.delivered.score = 99.987654321; },
    bundle => { bundle.interpret.percentile_range = [99.987654321, 99.99]; },
    bundle => { bundle.reference_response.reference.distribution = {}; },
    bundle => { bundle.coverage.is_an_estimate = true; },
    bundle => { bundle.events = [{ kind: 'question', stage: 'select', question_id: 'stale', question: 'Example unanswered question?' }]; },
  ]) {
    const report = scoreReport(); mutate(report.bundle);
    assert.equal(getDisplayResult(report.bundle).kind, 'incomplete');
    const html = reportView(report);
    assert.match(html, /A complete score report is not available/);
    assert.doesNotMatch(html, /result-sentence|reference-chart|99\.987654321/);
  }
});

test('the headline preserves the report’s recorded range without browser rounding', () => {
  const report = scoreReport();
  delete report.bundle.report.delivered.summary_parts;
  report.bundle.report.delivered.placement.percentile_range = [...report.bundle.interpret.percentile_range];
  const html = reportView(report);
  assert.match(html, /result-sentence">You scored higher than about <span class="result-range">20\.77 to 93\.85<\/span>/);
});

test('the recorded summary appears before the unchanged grid and closed full report, with all parts in order', () => {
  const report = scoreReport();
  const before = structuredClone(report);
  const parts = report.bundle.report.delivered.summary_parts;
  const html = reportView(report);
  const details = fullReport(html);
  const chartAt = html.indexOf('reference-chart');
  let previousAt = html.indexOf('result-title');
  for (const key of ['trait', 'where', 'comparison', 'not_a_chance']) {
    const at = html.indexOf(parts[key]);
    assert.ok(at > previousAt && at < chartAt, `${key} stays in its recorded order before the grid`);
    assert.equal(html.split(parts[key]).length - 1, 1, `${key} is not repeated`);
    previousAt = at;
  }
  assert.match(html, /class="[^"]*\bresult-sentence\b[^"]*"[^>]*>Example place in the group, with its range\./);
  assert.doesNotMatch(html, /You scored higher than about/);
  assert.ok(chartAt < details.index);
  assert.match(details.content, /id="full-report-toggle"/);
  assert.match(details.content, /Show full details/);
  assert.match(details.content, /Hide full details/);
  for (const value of [report.bundle.report.delivered.personalized_limit, report.bundle.report.delivered.disclaimer,
    ...report.bundle.report.delivered.limits]) {
    assert.ok(details.content.includes(value));
    assert.ok(!details.outside.includes(value));
  }
  assert.deepEqual(report, before);
});

test('absent, incomplete or conflicting summary parts preserve the exact complete summary and delivered range', () => {
  for (const parts of [undefined, { trait: 'Incomplete example part.' },
    { trait: 'Conflicting example trait.', where: 'Conflicting example placement.', comparison: 'Conflicting example comparison.', not_a_chance: 'Conflicting example limit.' }]) {
    const report = scoreReport();
    const delivered = report.bundle.report.delivered;
    delivered.summary_parts = parts;
    delivered.summary = 'The exact recorded summary, including its own phrasing. No rewriting.';
    const before = structuredClone(report);
    const html = reportView(report);
    assert.equal(html.split(delivered.summary).length - 1, 1);
    assert.ok(html.indexOf(delivered.summary) < html.indexOf('reference-chart'));
    assert.match(html, /You scored higher than about <span class="result-range">21 to 94<\/span>/);
    assert.doesNotMatch(html, /Incomplete example part|Conflicting example/);
    assert.deepEqual(report, before);
  }
});

test('the full report shows the delivered z-score and its meaning, without leaking internal warning codes', () => {
  const report = scoreReport();
  report.bundle.report.delivered.placement.z_score = 0.3;
  report.bundle.reference_response.warnings_for_report = ['R1: Example internal adapter warning.'];
  report.bundle.interpret.caveats_for_report = ['INT-4: Example internal interpretation note.', 'SEL-12: Example internal selection note.'];
  assert.equal(getDisplayResult(report.bundle).zScore, 0.25);
  const before = structuredClone(report);
  const html = reportView(report);
  const { content, outside } = fullReport(html);
  assert.match(content, /z-score in this group<\/dt>\s*<dd>0\.3<\/dd>/i);
  assert.match(content, /above or below the group(?:’|&#39;|')s average/);
  assert.match(content, /usual spread/);
  assert.match(content, /It is a position, not a risk\./);
  assert.doesNotMatch(outside, /z-score|above or below the group|usual spread/);
  assert.doesNotMatch(html, /INT-4|SEL-12|R1:|Example internal/);
  assert.deepEqual(report, before);
});

test('missing or inconsistent z-scores and missing summary or plain limits cannot reach the score presentation', () => {
  for (const mutate of [
    bundle => { delete bundle.percentile_range.z_score; },
    bundle => { bundle.percentile_range.z_score.n = 4; },
    bundle => { delete bundle.interpret.z_score; },
    bundle => { bundle.interpret.z_score = 987.654321; },
    bundle => { delete bundle.report.delivered.placement.z_score; },
    bundle => { bundle.report.delivered.placement.z_score = 987.654321; },
    bundle => { delete bundle.report.delivered.summary; },
    bundle => { delete bundle.report.delivered.limits; },
  ]) {
    const report = scoreReport();
    mutate(report.bundle);
    assert.equal(getDisplayResult(report.bundle).kind, 'incomplete');
    const html = reportView(report);
    assert.match(html, /A complete score report is not available/);
    assert.doesNotMatch(html, /result-title|reference-chart|full-report-details|987\.654321/);
  }
});

test('refusal headline and explanation stay visible while its full delivered reasoning is disclosed', () => {
  const report = fixtureState('finished-refusal').run.report;
  const before = structuredClone(report);
  const envelope = report.bundle.report.refusal_envelope;
  const html = reportView(report);
  const { content, outside } = fullReport(html);
  for (const key of ['headline', 'detail']) {
    assert.ok(outside.includes(envelope[key]), key);
    assert.ok(!content.includes(envelope[key]), key);
  }
  for (const key of ['what_was_tried', 'why_this_is_honest', 'what_would_change_it']) {
    assert.ok(content.includes(envelope[key]), key);
    assert.ok(!outside.includes(envelope[key]), key);
  }
  assert.doesNotMatch(html, /reference-chart|z-score/);
  assert.deepEqual(report, before);
});

test('untrusted summary parts and delivered limits are escaped without changing the report', () => {
  const report = scoreReport();
  const delivered = report.bundle.report.delivered;
  const unsafe = '<img src=x onerror="alert(1)">&';
  delivered.summary_parts = Object.fromEntries(['trait', 'where', 'comparison', 'not_a_chance'].map(key => [key, `${key}: ${unsafe}`]));
  delivered.summary = Object.values(delivered.summary_parts).join(' ');
  delivered.limits = [unsafe];
  const before = structuredClone(report);
  const html = reportView(report);
  assert.doesNotMatch(html, /<img src=x|<script/);
  assert.equal(html.split('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;').length - 1, 5);
  assert.deepEqual(report, before);
});

test('comparison people use the adapter’s exact score when report precision differs', () => {
  const report = scoreReport(), bundle = report.bundle;
  bundle.reference_response.reference.distribution.raw_scores = [-1, 0.25000000000000006, 1];
  bundle.reference_response.placement = { n: 3, n_below: 1, n_equal: 1, n_above: 1, patient_raw_score: 0.25000000000000006 };
  Object.assign(bundle.percentile_range, { n_below: 1, n_equal: 1 });
  const html = reportView(report);
  assert.equal(getDisplayResult(bundle).kind, 'score');
  assert.match(html, /data-chart-kind="people"/);
  assert.match(html, /1 scored the same\. 1 scored higher/);
  assert.match(html, /Your recorded score is 0\.25/);
});

test('refusal reports use the delivered envelope and stale refusal events cannot replace a score', () => {
  const refusal = fixtureState('finished-refusal').run.report;
  const html = reportView(refusal);
  for (const [key, value] of Object.entries(refusal.bundle.report.refusal_envelope)) {
    if (typeof value === 'string') assert.ok(html.includes(value), key);
  }
  assert.doesNotMatch(html, /result-sentence|reference-chart|Example of a reference-related refusal/);
  const scored = scoreReport();
  scored.refusal = { headline: 'Synthetic stale refusal', detail: 'Synthetic stale explanation' };
  const scoreHTML = reportView(scored);
  assert.match(scoreHTML, /result-sentence/);
  assert.doesNotMatch(scoreHTML, /Synthetic stale/);
});

test('where the comparison group comes from is the report\'s plain sentence, never the reference\'s own record', () => {
  const report = scoreReport();
  const reference = report.bundle.reference_response.reference;
  // The provider's record of it, as the page showed it on 10-15: files, paths, a header, a tool.
  reference.source = 'reference genotypes: ref_1kg_PGS005218.vcf.gz (2504 samples); cohort selected by ancestry.py build-ref from refdata/ancestry-ref';
  reference.version_string = '##source=1000GenomesPhase3Pipeline; ##reference=ftp://ftp.1000genomes.ebi.ac.uk/vol1';
  const html = reportView(report);
  assert.match(html, /<dt>Where the group comes from<\/dt><dd>Example: who you are compared with, from which study, chosen how\.<\/dd>/);
  for (const raw of ['ref_1kg', 'ancestry.py', 'refdata', '##source', 'ftp://', 'Comparison data version', 'Comparison source', 'Reference version']) {
    assert.ok(!html.includes(raw), raw);
  }
  // A report from before the sentence: the row is left out, not filled with the record.
  delete report.bundle.report.delivered.comparison_group;
  const older = reportView(report);
  assert.doesNotMatch(older, /Where the group comes from|ref_1kg|##source/);
  assert.match(older, /<dt>People in the group<\/dt>/);
});

test('the upload form asks the routine questions before the file, and the agreement before the button', () => {
  const html = renderPage(fixtureState('upload'), FIXTURE_NOW);
  const form = html.slice(html.indexOf('<form id="upload-form"'), html.indexOf('</form>'));
  const at = id => form.indexOf(`id="${id}"`);
  for (const id of ['intake-condition', 'intake-reason', 'intake-ancestry', 'intake-doctor-note', 'intake-consent']) assert.ok(at(id) > 0, id);
  assert.ok(at('intake-condition') < at('genome-file') && at('genome-file') < at('intake-consent') && at('intake-consent') < at('upload-submit'));
  // Nobody waits for a first question; and what is kept, and for how long, as it is now.
  assert.doesNotMatch(html, /We’ll ask what we need to know|We’ll let you know when we need you/);
  assert.match(html, /We’ll only ask you something if your answers and your file can’t settle it\./);
  assert.match(html, /Your file is deleted when the analysis ends\. Your result is kept for 24 hours, then deleted\./);
  assert.doesNotMatch(html, /an hour after the analysis/);
  // Disabled together while the file goes up.
  const sending = renderPage({ ...fixtureState('upload'), pending: 'upload' }, FIXTURE_NOW);
  assert.match(sending, /<fieldset class="intake-questions" disabled>/);
  assert.match(sending, /id="intake-consent"[^>]* disabled/);
});

test('a report for a person who answered up front shows why the method was picked, their ancestry note and the note for their doctor', () => {
  const report = scoreReport();
  const delivered = report.bundle.report.delivered;
  Object.assign(delivered, {
    model_choice: 'Example: which scoring method was picked, why, and what it costs you.',
    ancestry_note: 'Example: what your DNA shows beside what you told us.',
    doctor_note: 'Example: a short note for your doctor.',
  });
  const html = reportView(report);
  const details = html.indexOf('id="full-report-details"');
  // The note they asked for is not hidden behind the details.
  assert.match(html, /<section class="doctor-note" aria-labelledby="doctor-note-title"><h2 id="doctor-note-title">A note for your doctor<\/h2><p class="report-prose">Example: a short note for your doctor\.<\/p><\/section>/);
  assert.ok(html.indexOf('doctor-note') < details);
  assert.match(html, /<h3 class="report-subhead">Why this scoring method<\/h3><p class="report-prose">Example: which scoring method was picked, why, and what it costs you\.<\/p>/);
  assert.match(html, /<h3 class="report-subhead">Your ancestry and your DNA<\/h3><p class="report-prose">Example: what your DNA shows beside what you told us\.<\/p>/);
  assert.ok(html.indexOf('Why this scoring method') > details && html.indexOf('Your ancestry and your DNA') > details);
  // A report without them, as every benchmark run's: none of it.
  assert.doesNotMatch(reportView(scoreReport()), /doctor-note|Why this scoring method|Your ancestry and your DNA/);
});

test('a sign-in lasts 24 hours: the footer names the day it ends, and nothing says today\'s password', () => {
  const state = fixtureState('upload');
  state.session = { ...state.session, expires_at: FIXTURE_NOW / 1000 + 24 * 3600 };
  const day = new Date(FIXTURE_NOW + 24 * 3600 * 1000).toLocaleDateString([], { weekday: 'long' });
  assert.match(renderPage(state, FIXTURE_NOW), new RegExp(`Signed in until ${day}`));
  // Past its 24 hours, kept for its run.
  const kept = { ...fixtureState('running'), session: { ...state.session, expires_at: FIXTURE_NOW / 1000 - 1, kept_for_run: true } };
  const html = renderPage(kept, FIXTURE_NOW);
  assert.match(html, /Your sign-in has ended: you stay signed in for this analysis until it ends and its result is gone\./);
  const gone = renderPage({ ...fixtureState('upload'), session: { ...state.session, via_link: true }, run: { status: 'idle', owned: false } }, FIXTURE_NOW);
  assert.match(gone, /sign in with the password\./);
  for (const page of [html, gone]) assert.doesNotMatch(page, /[Tt]oday’s password|midnight/);
});

test('untrusted page and delivered report content is escaped without changing recorded text', () => {
  const unsafe = '<img src=x onerror="alert(1)">&';
  const run = startedRun(38, { status: 'waiting', question: {
    id: 'synthetic-xss', stage: 'score', text: unsafe, why_it_matters: unsafe, options: [unsafe, { label: unsafe }],
  } });
  run.progress.push({ kind: 'stage_progress', stage: 'score', text: unsafe });
  const state = { ...fixtureState('running'), run, error: unsafe, notice: unsafe };
  const report = scoreReport();
  report.bundle.report.delivered.personalized_limit = unsafe;
  report.bundle.report.delivered.disclaimer = unsafe;
  report.bundle.reference_response.reference.source = unsafe;
  report.bundle.reference_response.warnings_for_report = [unsafe];
  for (const html of [renderPage(state, FIXTURE_NOW), reportView(report)]) {
    assert.doesNotMatch(html, /<img src=x|<script/);
    assert.match(html, /&lt;img src=x onerror=&quot;alert\(1\)&quot;&gt;&amp;/);
  }
});

test('every rendered state keeps the research disclaimer without a demo footer', () => {
  for (const name of ['upload', 'busy', 'running', 'waiting-answer', 'offline', 'finished-score', 'finished-refusal', 'error']) {
    const html = renderPage(fixtureState(name), FIXTURE_NOW);
    assert.match(html, /Research tool\. Not medical advice\./, name);
    // Retained event notes may themselves say "demo"; the interface does not.
    const visible = html.replace(/<details\b[^>]*>[\s\S]*?<\/details>/g, '');
    assert.equal((visible.match(/\bdemo\b/gi) || []).length, 0, name);
  }
});

test('the estimate is the worker\'s, from measured runs; without one the page names no numbers', () => {
  const measured = { low_minutes: 52, median_minutes: 58, high_minutes: 66, runs: 7 };
  const run = startedRun(38, { estimate: measured });
  assert.equal(timingInfo(run, FIXTURE_NOW).primary, 'About 14–28 min left');
  assert.equal(timingInfo(run, FIXTURE_NOW).caption, 'An estimate from recent genotype-file runs, which took 52–66 minutes. Questions can add time.');
  assert.equal(timingInfo({ ...run, kind: 'whole_genome' }, FIXTURE_NOW).caption, 'An estimate from recent whole-genome runs, which took 52–66 minutes. Questions can add time.');
  const html = renderRun(run);
  assert.match(html, /<progress max="66" value="38"/);
  assert.match(html, /class="estimate-threshold" data-at="79"/);
  // The window the estimate spans starts there too, not at the 70% the page once fixed.
  assert.match(html, /class="estimate-range" data-at="79"/);
  assert.match(html, /52–66 min estimate/);
  // The time it waited for its person's answers is not counted against the estimate.
  assert.equal(timingInfo({ ...run, waited_seconds: 330 }, FIXTURE_NOW).primary, 'About 19–33 min left');
  assert.match(renderRun({ ...run, status: 'waiting', question: { id: 'q', text: 'Example?' } }), /An estimate from recent genotype-file runs, which took 52–66 minutes\. Questions can add time\./);
  // The upload card: each kind of file its own estimate, the whole genome's once one is measured.
  const idle = renderPage({ ...fixtureState('upload'), run: { status: 'idle', owned: false, estimates: { chip: measured } } }, FIXTURE_NOW);
  assert.match(idle, /<span>52–66<\/span><p>minutes for a genotype file from a chip\./);
  assert.doesNotMatch(idle, /whole genome: about/);
  const both = renderPage({ ...fixtureState('upload'), run: { status: 'idle', owned: false, estimates: { chip: measured, whole_genome: { low_minutes: 30, median_minutes: 36, high_minutes: 45, runs: 2 } } } }, FIXTURE_NOW);
  assert.match(both, /A whole genome: about 30–45 minutes\./);
  // No estimate (nothing measured yet): no numbers, no time bar.
  const bare = startedRun(38, { estimate: undefined });
  assert.equal(timingInfo(bare, FIXTURE_NOW).caption, 'Questions can add time.');
  const waitingWithoutEstimate = { ...bare, kind: 'whole_genome', estimates: { chip: measured }, question: { id: 'q', text: 'Example?' } };
  assert.equal(timingInfo(waitingWithoutEstimate, FIXTURE_NOW).caption, 'Questions can add time.');
  assert.doesNotMatch(renderRun(waitingWithoutEstimate), /<progress|52–66|70–100|minute run/);
  assert.doesNotMatch(renderRun(bare), /<progress|min left|70–100/);
  assert.doesNotMatch(renderPage({ ...fixtureState('upload'), run: { status: 'idle', owned: false } }, FIXTURE_NOW), /intake-time|70–100/);
});

test('a question with choices offers them to pick, and free text only beside them when the agent offered it', () => {
  const ask = extra => renderRun(startedRun(12, { status: 'waiting', question: { id: 'q', stage: 'select', text: 'May I go ahead?', why_it_matters: 'It decides the model.', options: ['Yes, go ahead', 'No'], ...extra } }));
  const strict = ask({ other: false });
  assert.match(strict, /name="answer-choice" id="answer-choice-0" value="0" required><span class="question-choice-face"><span>Yes, go ahead<\/span>/);
  assert.match(strict, /name="answer-choice" id="answer-choice-1" value="1" required><span class="question-choice-face"><span>No<\/span>/);
  assert.doesNotMatch(strict, /<textarea|answer-choice-other/);
  const open = ask({ other: true });
  assert.match(open, /id="answer-choice-other" value="other" required><span class="question-choice-face"><span>Something else<\/span>/);
  assert.match(open, /<textarea id="answer"/);
  // Escaped like every other text.
  assert.match(ask({ options: ['<b>Yes</b>', 'No'] }), /<span>&lt;b&gt;Yes&lt;\/b&gt;<\/span>/);
  // In the person's own words: no choices, the free-text answer as before.
  const words = renderRun(startedRun(12, { status: 'waiting', question: { id: 'q', stage: 'select', text: 'Which condition?', why_it_matters: 'It decides the model.' } }));
  assert.match(words, /<textarea id="answer"/);
  assert.doesNotMatch(words, /type="radio"/);
});

test('a run shows its private link, to save and come back by, before and after it ends', () => {
  const link = '/r/' + 'T'.repeat(43);
  const running = renderPage({ ...fixtureState('running'), origin: 'https://demo.example', run: startedRun(12, { link }) }, FIXTURE_NOW);
  assert.match(running, /Save this link to get back to your result/);
  assert.ok(running.includes(`value="https://demo.example${link}"`));
  for (const name of ['finished-score', 'error']) {
    const base = fixtureState(name);
    const html = renderPage({ ...base, origin: 'https://demo.example', run: { ...base.run, link } }, FIXTURE_NOW);
    assert.ok(html.includes(`value="https://demo.example${link}"`), name);
  }
});

test('an ended run says plainly when its result will be deleted', () => {
  const purge = FIXTURE_NOW / 1000 + 23 * 3600;
  for (const name of ['finished-score', 'error']) {
    const base = fixtureState(name);
    const html = renderPage({ ...base, run: { ...base.run, purge_at: purge } }, FIXTURE_NOW);
    const at = new Date(purge * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    assert.match(html, /Your result is kept until [^<]*, then deleted\./, name);
    assert.ok(html.includes(at), name);
    // How to delete it sooner, in the words of the buttons that do: 'Delete these files', 'Delete files and try again'.
    assert.match(html, /then deleted\. Deleting the files deletes it now\.<\/p>/, name);
    assert.match(html, /data-action="end"[^>]*>Delete (these files|files and try again)/, name);
  }
});

test('a private link whose result is gone says so, and offers no upload', () => {
  const base = fixtureState('upload');
  const html = renderPage({ ...base, session: { ...base.session, via_link: true }, run: { status: 'idle', owned: false } }, FIXTURE_NOW);
  assert.match(html, /no longer here/);
  assert.doesNotMatch(html, /upload-form|genome-file/);
  assert.match(html, /Opened with your private link\./);
});
