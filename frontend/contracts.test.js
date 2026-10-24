import test from 'node:test';
import assert from 'node:assert/strict';
import { STAGES, normalizeOutcome, readArtifacts, validateReport, validateArtifactBundle, getDisplayResult } from './contracts.js';

// Synthetic contract-test fixtures only. These numbers are never bundled into
// the application or shown as a person's result. They are not scientific data.
function exampleJudgment(outcome = 'score') {
  return { outcome, decision: 'Example test judgment.', reasons: ['Example test reason.'], evidence: [{ source: 'example/test-artifact.json', field: 'example' }], requirement_ids: ['EXAMPLE-1'] };
}
function exampleBundle() {
  return {
    score: { ...exampleJudgment(), raw_score: 0.25, model: 'PGS000000' },
    coverage: { tool: 'weight_coverage.py measure', resolved_weight_fraction: 0.97 },
    interpret: { ...exampleJudgment(), percentile_range: [20.77, 93.85], z_score: 0.25, caveats_for_report: ['Example interpretation caveat.'] },
    percentile_range: { tool: 'rank_range.py', n: 3, n_below: 2, n_equal: 0, n_above: 1, ties: 'half', level: 0.95, percentile_range: [20.7655, 93.8492], z_score: { value: 0.25, patient_raw_score: 0.25, mean: 0, sd: 1, sd_ddof: 1, n: 3 } },
    reference_response: {
      status: 'reference_valid', interpret_can_proceed: true, warnings_for_report: ['Example adapter warning.'],
      reference: {
        usable: true, source: 'Example test reference', version_string: 'example-version',
        ancestry_grouping_definition: 'Example grouping', cohort_construction_rule: 'Example rule',
        n_participants: 3, too_few_threshold_used: 2, assay_layer: 'Example assay', scoring_parity: 'same-resolved-set',
        distribution: { raw_scores: [-1, 0, 1], mean: 0, sd: 1, n: 3 },
      },
    },
    report: {
      ...exampleJudgment(), delivered: { summary: 'Example trait. Example place in the group, with its range. Example everyday comparison. Not your chance of getting it.', summary_parts: { trait: 'Example trait.', where: 'Example place in the group, with its range.', comparison: 'Example everyday comparison.', not_a_chance: 'Not your chance of getting it.' }, limits: ['Example plain limit.'], score: 0.25, placement: { percentile_range: [21, 94], z_score: 0.25 }, resolved_weight_fraction: 0.97, personalized_limit: 'Example personalized limit.', disclaimer: 'Example disclaimer: not a diagnosis.', comparison_group: 'Example: who you are compared with, from which study, chosen how.' },
    },
  };
}
function exampleReferenceBundle() {
  const bundle = exampleBundle();
  const modelHash = 'a'.repeat(64);
  const scoreInputHash = 'b'.repeat(64);
  bundle.model_choice = {
    ...exampleJudgment('proceed'), chosen_model: 'PGS000000', trait_id: 'EXAMPLE_1000', gate_type: 'transparency',
    candidates_considered: [{ pgs_id: 'PGS000000', why_ranked_here: 'Example ranking.', cost_of_choosing_it: 'Example limitation.' }],
    coverage_estimate: { resolved_weight_fraction: 0.99, is_an_estimate: true },
  };
  bundle.score.score_input_sha256 = scoreInputHash;
  bundle.interpret.reference = {
    source: 'Example cohort summary', version_string: 'Example release summary', n: 3,
    scoring_parity: 'same-resolved-set: all example scoring variants were used.',
  };
  bundle.reference_response.request_echo = {
    trait: { id: [{ id: 'EXAMPLE_1000', label: 'Example catalog label' }], label: 'Example request label' },
    model: { pgs_id: 'PGS000000', sha256: modelHash },
    patient: { raw_score: 0.25, resolved_weight_fraction: 0.97, score_input_sha256: scoreInputHash },
  };
  bundle.reference_response.reference.model = { pgs_id: 'PGS000000', sha256: modelHash };
  bundle.reference_response.reference.scoring_parity_detail = {
    score_input_sha256: scoreInputHash, score_input_sha256_in_request: scoreInputHash,
  };
  return bundle;
}
function exampleRefusal() {
  return {
    ...exampleJudgment('refuse'),
    refusal_envelope: {
      headline: 'Example: you do not receive a score here.', detail: 'Example: your result needs a usable reference.',
      what_was_tried: 'Example: your file and model were examined.', why_this_is_honest: 'Example: you deserve a comparison supported by data.',
      what_would_change_it: 'Example: you could be represented in better reference data.', is_about_the_person: false,
    },
  };
}

test('all six stages are explicit; a continuation is never a delivered score', () => {
  assert.equal(STAGES.length, 6);
  assert.equal(normalizeOutcome('ancestry', 'proceed'), 'continue');
  assert.equal(normalizeOutcome('score', 'score'), 'continue');
  assert.equal(normalizeOutcome('report', 'score'), 'score');
  assert.equal(normalizeOutcome('report', 'proceed'), 'unknown');
  for (const state of ['refuse', 'stop', 'ask', 'blocked', 'session-ended-by-person']) assert.equal(normalizeOutcome('select', state), state);
});

test('verified artifacts expose only recorded numbers and carry every warning', () => {
  const bundle = exampleBundle();
  assert.deepEqual(validateArtifactBundle(bundle), []);
  const display = getDisplayResult(bundle);
  assert.equal(display.kind, 'score');
  assert.equal(display.score, bundle.report.delivered.score);
  assert.deepEqual(display.percentileRange, bundle.interpret.percentile_range);
  assert.equal(display.coverage, bundle.coverage.resolved_weight_fraction);
  assert.deepEqual(display.warnings, ['Example adapter warning.', 'Example interpretation caveat.']);
  assert.ok(display.provenance.every(value => value.source));
});

test('no reference means no raw score or percentile display', () => {
  const bundle = exampleBundle();
  delete bundle.reference_response;
  const display = getDisplayResult(bundle);
  assert.equal(display.kind, 'incomplete');
  assert.equal(display.score, null);
  assert.equal(display.percentileRange, null);
  assert.ok(display.issues.some(value => value.code === 'validated_reference_required'));
});

test('an estimate with the same fraction cannot masquerade as measured coverage', () => {
  const bundle = exampleBundle();
  bundle.coverage.tool = 'weight_coverage.py estimate';
  bundle.coverage.measured_not_estimated = true;
  bundle.coverage.source = 'runs/example/select/coverage_est_PGS000000.json';
  assert.equal(getDisplayResult(bundle).kind, 'incomplete');
  assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'measured_coverage_required'));
});

test('explicit measured source bridge remains usable without rewriting values', () => {
  const bundle = exampleBundle();
  bundle.coverage = { resolved_weight_fraction: 0.97, measured_not_estimated: true, source: 'runs/example/score/coverage_PGS000000.json' };
  assert.equal(getDisplayResult(bundle).kind, 'score');
});

test('a measured tool label does not override a contradictory estimate flag', () => {
  const bundle = exampleBundle(); bundle.coverage.is_an_estimate = true;
  assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'measured_coverage_required'));
  assert.equal(getDisplayResult(bundle).score, null);
});

test('cross-artifact conflicts suppress the result instead of choosing a number', () => {
  for (const mutate of [
    bundle => { bundle.report.delivered.score = 0.3; },
    bundle => { bundle.report.delivered.placement.percentile_range = [30, 94]; },
    bundle => { bundle.report.delivered.resolved_weight_fraction = 0.96; },
    bundle => { bundle.score.internal_consistency = 'INCONSISTENT -- resolved variants were not used'; },
  ]) {
    const bundle = exampleBundle(); mutate(bundle);
    assert.equal(getDisplayResult(bundle).kind, 'incomplete');
    assert.equal(getDisplayResult(bundle).score, null);
  }
});

test('numeric strings, NaN and infinity never become result values', () => {
  for (const value of ['60', NaN, Infinity, -1, 101]) {
    const bundle = exampleBundle(); bundle.interpret.percentile_range = [value, 93.85];
    assert.equal(getDisplayResult(bundle).kind, 'incomplete');
  }
  const bundle = exampleBundle(); bundle.report.delivered.score = '0.25'; delete bundle.score;
  assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'score_required'));
});

test('raw zero is retained as a real recorded value, not treated as absent', () => {
  const bundle = exampleBundle(); bundle.score.raw_score = 0; bundle.report.delivered.score = 0;
  assert.equal(getDisplayResult(bundle).score, 0);
});

test('partial reference metadata cannot silently authorize a percentile', () => {
  for (const key of ['source', 'version_string', 'cohort_construction_rule', 'assay_layer', 'scoring_parity', 'n_participants', 'distribution']) {
    const bundle = exampleBundle(); delete bundle.reference_response.reference[key];
    assert.equal(getDisplayResult(bundle).kind, 'incomplete', key);
  }
  const bundle = exampleBundle(); bundle.reference_response.reference.usable = false;
  assert.equal(getDisplayResult(bundle).kind, 'incomplete');
});

test('reference participant conflicts and unfinished earlier stages suppress a final score', () => {
  const conflict = exampleBundle();
  conflict.interpret.reference = { n: 4 };
  assert.ok(validateArtifactBundle(conflict).some(value => value.code === 'reference_mismatch'));
  assert.equal(getDisplayResult(conflict).kind, 'incomplete');
  for (const outcome of ['refuse', 'stop', 'ask', 'session-ended-by-person']) {
    const bundle = exampleBundle(); bundle.model_choice = exampleJudgment(outcome);
    assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'unfinished_before_report'));
    assert.equal(getDisplayResult(bundle).score, null);
  }
});

test('reference summaries may paraphrase provenance and explain the same parity value', () => {
  const bundle = exampleReferenceBundle();
  assert.notEqual(bundle.interpret.reference.source, bundle.reference_response.reference.source);
  assert.notEqual(bundle.interpret.reference.version_string, bundle.reference_response.reference.version_string);
  assert.deepEqual(validateArtifactBundle(bundle), []);
  assert.equal(getDisplayResult(bundle).kind, 'score');
  assert.equal(getDisplayResult(bundle).reference, bundle.reference_response.reference);
});

test('trait comparisons accept a bare identifier or exactly one catalog trait object', () => {
  for (const choice of ['EXAMPLE_1000', [{ id: 'EXAMPLE_1000', label: 'Example choice label' }]]) {
    for (const request of ['EXAMPLE_1000', [{ id: 'EXAMPLE_1000', label: 'Different wording for the same trait' }]]) {
      const bundle = exampleReferenceBundle();
      bundle.model_choice.trait_id = choice;
      bundle.reference_response.request_echo.trait.id = request;
      assert.deepEqual(validateArtifactBundle(bundle), []);
      assert.equal(getDisplayResult(bundle).kind, 'score');
    }
  }
});

test('different, ambiguous or malformed requested traits cannot authorize a score', () => {
  for (const trait of [
    'EXAMPLE_1001', [{ id: 'EXAMPLE_1001' }], [],
    [{ id: 'EXAMPLE_1000' }, { id: 'EXAMPLE_1001' }],
    ['EXAMPLE_1000'], { id: 'EXAMPLE_1000' }, [{ label: 'Example missing id' }], [{ id: 1000 }], null,
  ]) {
    const bundle = exampleReferenceBundle();
    bundle.reference_response.request_echo.trait.id = trait;
    assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'trait_identity_mismatch'), JSON.stringify(trait));
    assert.equal(getDisplayResult(bundle).score, null);
  }
  const ambiguousChoice = exampleReferenceBundle();
  ambiguousChoice.model_choice.trait_id = [{ id: 'EXAMPLE_1000' }, { id: 'EXAMPLE_1000' }];
  assert.ok(validateArtifactBundle(ambiguousChoice).some(value => value.code === 'trait_id_required'));
});

test('participant numbers and the leading parity value remain strict despite explanatory prose', () => {
  for (const [key, value] of [
    ['n', 4], ['n', '3'],
    ['scoring_parity', 'full-model-set: example explanation'],
    ['scoring_parity', 'other: example explanation'],
    ['scoring_parity', 'same-resolved-set-extra: example explanation'],
    ['scoring_parity', 'an explanation mentioning same-resolved-set'],
    ['scoring_parity', null],
  ]) {
    const bundle = exampleReferenceBundle(); bundle.interpret.reference[key] = value;
    assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'reference_mismatch'), `${key}: ${value}`);
    assert.equal(getDisplayResult(bundle).score, null);
  }
  for (const parity of ['same-resolved-set', 'same-resolved-set: example explanation', 'same-resolved-set (example explanation)']) {
    const bundle = exampleReferenceBundle(); bundle.interpret.reference.scoring_parity = parity;
    assert.equal(getDisplayResult(bundle).kind, 'score');
  }
  const providerProse = exampleReferenceBundle();
  providerProse.reference_response.reference.scoring_parity = 'same-resolved-set: example explanation';
  assert.ok(validateArtifactBundle(providerProse).some(value => value.code === 'scoring_parity_required'));
});

test('provider model identity, model hashes and scoring-input hashes remain strict', () => {
  for (const [code, mutate] of [
    ['model_identity_mismatch', bundle => { bundle.reference_response.reference.model.pgs_id = 'PGS000001'; }],
    ['model_hash_mismatch', bundle => { bundle.reference_response.request_echo.model.sha256 = 'c'.repeat(64); }],
    ['model_hash_mismatch', bundle => { bundle.reference_response.reference.model.sha256 = 'c'.repeat(64); }],
    ['score_input_hash_mismatch', bundle => { bundle.score.score_input_sha256 = 'c'.repeat(64); }],
    ['score_input_hash_mismatch', bundle => { bundle.reference_response.request_echo.patient.score_input_sha256 = 'c'.repeat(64); }],
    ['score_input_hash_mismatch', bundle => { bundle.reference_response.reference.scoring_parity_detail.score_input_sha256 = 'c'.repeat(64); }],
    ['score_input_hash_mismatch', bundle => { bundle.reference_response.reference.scoring_parity_detail.score_input_sha256_in_request = 'c'.repeat(64); }],
  ]) {
    const bundle = exampleReferenceBundle(); mutate(bundle);
    assert.ok(validateArtifactBundle(bundle).some(value => value.code === code));
    assert.equal(getDisplayResult(bundle).kind, 'incomplete');
    assert.equal(getDisplayResult(bundle).score, null);
  }
  for (const hash of [null, '', 'a'.repeat(8), 'g'.repeat(64), { sha256: 'a'.repeat(64) }]) {
    const bundle = exampleReferenceBundle();
    bundle.reference_response.request_echo.model.sha256 = hash;
    bundle.reference_response.reference.model.sha256 = hash;
    assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'model_hash_mismatch'));
  }
});

test('a score needs a named model and cannot borrow a different model’s reference', () => {
  const missing = exampleBundle(); delete missing.score.model;
  assert.ok(validateArtifactBundle(missing).some(value => value.code === 'score_model_required'));
  assert.equal(getDisplayResult(missing).score, null);
  const conflict = exampleBundle();
  conflict.reference_response.request_echo = { model: { pgs_id: 'PGS000001' } };
  assert.ok(validateArtifactBundle(conflict).some(value => value.code === 'model_identity_mismatch'));
  assert.equal(getDisplayResult(conflict).score, null);
  conflict.reference_response.request_echo.model.pgs_id = 'PGS000000';
  assert.equal(getDisplayResult(conflict).model, 'PGS000000');
});

test('the reference request must agree with the recorded score and measured coverage', () => {
  for (const patient of [{ raw_score: 3 }, { resolved_weight_fraction: 0.1 }]) {
    const bundle = exampleBundle(); bundle.reference_response.request_echo = { patient };
    assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'reference_request_mismatch'));
    assert.equal(getDisplayResult(bundle).score, null);
  }
});

test('a rounded delivery and a float round trip are the recorded numbers; a real gap is still a conflict', () => {
  // Coverage delivered to four places, and the raw score echoed through a float.
  const bundle = exampleReferenceBundle();
  bundle.score.raw_score = -0.8271304;
  bundle.report.delivered.score = -0.8271304;
  bundle.coverage.resolved_weight_fraction = 0.947316258104925;
  bundle.report.delivered.resolved_weight_fraction = 0.9473;
  bundle.reference_response.request_echo.patient.raw_score = -0.8271303999999999;
  bundle.reference_response.request_echo.patient.resolved_weight_fraction = 0.947316258104925;
  assert.deepEqual(validateArtifactBundle(bundle), []);
  assert.equal(getDisplayResult(bundle).kind, 'score');
  for (const mutate of [
    value => { value.report.delivered.resolved_weight_fraction = 0.93; },              // two places, 0.0173 apart
    value => { value.report.delivered.resolved_weight_fraction = 1; },                 // no places: exact or nothing
    value => { value.reference_response.request_echo.patient.raw_score = -0.83; },     // a raw score to two places
    value => { value.report.delivered.score = -0.8271; value.score.raw_score = -0.8272304; },  // four places, 0.00013 apart
    value => { value.report.delivered.placement.percentile_range = [21.4, 94]; },       // 21.4 is not 20.77
  ]) {
    const changed = structuredClone(bundle); mutate(changed);
    assert.equal(getDisplayResult(changed).kind, 'incomplete');
    assert.equal(getDisplayResult(changed).score, null);
  }
});

test('the percentile is a range from the range tool, delivered as recorded, and never one number', () => {
  const bundle = exampleBundle();
  assert.deepEqual(validateArtifactBundle(bundle), []);
  const display = getDisplayResult(bundle);
  assert.deepEqual([display.kind, display.percentileRange], ['score', [20.77, 93.85]]);
  assert.equal(display.percentile, undefined);
  for (const [why, mutate] of [
    ['one number is not a placement', b => { delete b.interpret.percentile_range; b.interpret.percentile = 60; }],
    ['delivered as one number', b => { b.report.delivered.placement = { percentile: 60 }; }],
    ['backwards', b => { b.interpret.percentile_range = [93.85, 20.77]; }],
    ['past 100', b => { b.interpret.percentile_range = [20.77, 101]; }],
    ['not the range the report delivers', b => { b.report.delivered.placement.percentile_range = [18, 94]; }],
    ['no tool behind it', b => { delete b.percentile_range; }],
    ['not the tool\'s range', b => { b.percentile_range.percentile_range = [10, 90]; }],
    ['the tool counted another group', b => { b.percentile_range.n = 150; }],
  ]) {
    const changed = exampleBundle(); mutate(changed);
    assert.equal(getDisplayResult(changed).kind, 'incomplete', why);
  }
});

test('event history cannot authorize a stale score while the run is asking, blocked or ended', () => {
  for (const event of [
    { kind: 'question', stage: 'select', question: 'Example question', question_id: 'example' },
    { kind: 'blocked', stage: 'interpret' },
    { kind: 'run_completed', outcome: 'refuse' },
    { kind: 'run_completed', outcome: 'stop' },
    { kind: 'run_failed' },
    { kind: 'session_end' },
  ]) {
    const bundle = exampleBundle(); bundle.events = [event];
    assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'report_event_conflict'));
    assert.equal(getDisplayResult(bundle).score, null);
  }
  const resumed = exampleBundle(); resumed.events = [{ kind: 'blocked' }, { kind: 'run_completed', outcome: 'score' }];
  assert.equal(getDisplayResult(resumed).kind, 'score');
});

test('a refusal renders the direct envelope, without requiring scored artifacts', () => {
  const report = exampleRefusal();
  assert.deepEqual(validateReport(report), []);
  const display = getDisplayResult({ report });
  assert.equal(display.kind, 'refuse');
  assert.equal(display.envelope, report.refusal_envelope);
  assert.equal(display.score, null);
});

test('refusals with absent delivery or obvious log copy are held for correction', () => {
  const missing = exampleRefusal(); delete missing.refusal_envelope;
  assert.ok(validateReport(missing).some(value => value.code === 'envelope_required'));
  const logged = exampleRefusal(); logged.refusal_envelope.detail = 'REP-4: told them that your file was checked.';
  assert.ok(validateReport(logged).some(value => value.code === 'log_register'));
});

test('blocked provider transport, stop, ask and voluntary ending stay separate', () => {
  assert.equal(getDisplayResult({ reference_response: { status: 'reference_unavailable' } }).kind, 'blocked');
  assert.equal(getDisplayResult({ score: exampleJudgment('stop') }).kind, 'stop');
  assert.equal(getDisplayResult({ model_choice: exampleJudgment('ask') }).kind, 'ask');
  assert.equal(getDisplayResult({ ancestry: exampleJudgment('session-ended-by-person') }).kind, 'session-ended-by-person');
  assert.equal(getDisplayResult({ status: 'session-ended-by-person' }).kind, 'session-ended-by-person');
  assert.ok(validateReport(exampleJudgment('ask')).some(value => value.code === 'final_report_outcome'));
});

test('partial stage imports validate contracts without inventing missing stages', () => {
  assert.deepEqual(validateArtifactBundle({}), []);
  assert.equal(getDisplayResult({}).kind, 'pending');
  const bundle = {
    stages: { door: { ...exampleJudgment('proceed'), assay_class: 'genotyping-array', absence_means: 'Example: unmeasured.', confidence: 'Example assessment.', genome_build: { value: 'GRCh37', verified_by: ['Example marker cross-check.'] } } },
  };
  assert.deepEqual(validateArtifactBundle(bundle), []);
  delete bundle.stages.door.absence_means;
  assert.ok(validateArtifactBundle(bundle).some(value => value.path === 'classification.absence_means'));
});

test('a partial ancestry match is the agent\'s judgment: the page takes it, shown before a model is chosen like a disagreement', () => {
  const bundle = { ancestry: { ...exampleJudgment('proceed'), inferred_group: 'Example genetic grouping', agreement: 'partial', used_for_math: 'inferred (ANC-4)', surfaced_to_person: true } };
  assert.deepEqual(validateArtifactBundle(bundle).filter(value => value.path?.startsWith('ancestry') || value.field?.startsWith?.('ancestry')), []);
  assert.ok(!validateArtifactBundle(bundle).some(value => value.code === 'agreement_required'));
  bundle.ancestry.surfaced_to_person = false;
  assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'mismatch_not_surfaced'));
  bundle.ancestry.agreement = 'mixed';
  assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'agreement_required'));
});

test('the z-score comes from rank_range.py in the same comparison group, and the report delivers it', () => {
  assert.deepEqual(validateArtifactBundle(exampleBundle()), []);
  assert.equal(getDisplayResult(exampleBundle()).zScore, 0.25);
  for (const [mutate, code] of [
    [b => { delete b.percentile_range.z_score; }, 'z_tool_required'],
    [b => { b.percentile_range.z_score.sd = 0; }, 'z_tool_required'],
    [b => { b.percentile_range.z_score.n = 4; }, 'z_group_mismatch'],
    [b => { delete b.interpret.z_score; }, 'z_required'],
    [b => { b.interpret.z_score = 0.4; }, 'z_mismatch'],
    [b => { delete b.report.delivered.placement.z_score; }, 'delivered_z_required'],
    [b => { b.report.delivered.placement.z_score = 1.2; }, 'delivered_z_mismatch'],
  ]) {
    const bundle = exampleBundle(); mutate(bundle);
    assert.ok(validateArtifactBundle(bundle).some(value => value.code === code), code);
    assert.equal(getDisplayResult(bundle).kind, 'incomplete', code);
  }
  // Rounded as written: 0.3 is the recorded 0.25 to one place.
  const rounded = exampleBundle(); rounded.report.delivered.placement.z_score = 0.3;
  assert.deepEqual(validateArtifactBundle(rounded), []);
});

test('a scored report leads with its plain summary and carries its limits in plain words', () => {
  const shown = getDisplayResult(exampleBundle());
  assert.equal(shown.summary, 'Example trait. Example place in the group, with its range. Example everyday comparison. Not your chance of getting it.');
  // Its parts, in their order, for a page that sets them apart.
  assert.deepEqual(Object.keys(shown.summaryParts), ['trait', 'where', 'comparison', 'not_a_chance']);
  assert.equal(shown.summaryParts.comparison, 'Example everyday comparison.');
  assert.deepEqual(shown.limits, ['Example plain limit.']);
  for (const [mutate, code] of [
    [b => { delete b.report.delivered.summary; }, 'summary_required'],
    [b => { b.report.delivered.summary = ' '; }, 'summary_required'],
    [b => { delete b.report.delivered.limits; }, 'limits_required'],
    [b => { b.report.delivered.limits = []; }, 'limits_required'],
    [b => { b.report.delivered.limits = ['Fine.', '']; }, 'limits_required'],
  ]) {
    const bundle = exampleBundle(); mutate(bundle);
    assert.ok(validateArtifactBundle(bundle).some(value => value.code === code), code);
  }
});

test('ancestry disagreement cannot be silently hidden before selecting a model', () => {
  const bundle = { ancestry: { ...exampleJudgment('proceed'), inferred_group: 'Example genetic grouping', agreement: 'disagree', used_for_math: 'inferred (ANC-4)', surfaced_to_person: false } };
  assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'mismatch_not_surfaced'));
  bundle.ancestry.surfaced_to_person = true;
  assert.deepEqual(validateArtifactBundle(bundle), []);
});

test('for a person who answered up front, a mismatch told in the report and a model chosen without asking are taken', () => {
  // The demo's upload form: the agent judged the mismatch does not change the result and told it
  // in the report, and nobody confirmed the model: they agreed on the form.
  const report = { ...exampleJudgment('score'), delivered: { ancestry_note: 'Example: what your DNA shows beside what you told us.' } };
  const bundle = { ancestry: { ...exampleJudgment('proceed'), inferred_group: 'Example genetic grouping', agreement: 'partial',
    used_for_math: 'inferred (ANC-4)', surfaced_to_person: false, surface_in_report: true }, report };
  assert.ok(!validateArtifactBundle(bundle).some(value => value.code === 'mismatch_not_surfaced'));
  report.delivered.ancestry_note = ' ';
  assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'mismatch_not_surfaced'));
  delete bundle.ancestry.surface_in_report;
  report.delivered.ancestry_note = 'Example note.';
  assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'mismatch_not_surfaced'));
  const choice = { model_choice: {
    ...exampleJudgment('proceed'), chosen_model: 'PGS000000', trait_id: 'EXAMPLE_0000', gate_type: 'up-front',
    candidates_considered: [{ pgs_id: 'PGS000000', why_ranked_here: 'Example ranking.', cost_of_choosing_it: 'Example evidence limitation.' }],
    coverage_estimate: { resolved_weight_fraction: 0.99, is_an_estimate: true },
  } };
  assert.deepEqual(validateArtifactBundle(choice), []);
  choice.model_choice.gate_type = 'asked-nobody';
  assert.ok(validateArtifactBundle(choice).some(value => value.code === 'gate_required'));
});

test('model recommendation requires a cost, a bare id and the correct gate', () => {
  const bundle = { model_choice: {
    ...exampleJudgment('proceed'), chosen_model: 'PGS000000', trait_id: 'EXAMPLE_0000', gate_type: 'transparency',
    candidates_considered: [{ pgs_id: 'PGS000000', why_ranked_here: 'Example ranking.', cost_of_choosing_it: 'Example evidence limitation.' }],
    coverage_estimate: { resolved_weight_fraction: 0.99, is_an_estimate: true },
  } };
  assert.deepEqual(validateArtifactBundle(bundle), []);
  delete bundle.model_choice.candidates_considered[0].cost_of_choosing_it;
  assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'candidate_cost_required'));
  bundle.model_choice.chosen_model = 'PGS000001';
  assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'chosen_candidate_missing'));
  delete bundle.model_choice.coverage_estimate;
  assert.ok(validateArtifactBundle(bundle).some(value => value.code === 'estimate_label_required'));
});

test('untrusted non-object artifacts return issues rather than throwing', () => {
  for (const value of [null, [], 'report']) {
    assert.ok(validateArtifactBundle(value).length);
    assert.equal(getDisplayResult(value).kind, 'incomplete');
    assert.ok(validateReport(value).length);
    assert.doesNotThrow(() => readArtifacts(value));
  }
});

test('malformed JSON fields cannot invoke coercion hooks or crash validation', () => {
  const poison = { toString: null, valueOf: null };
  const bundle = exampleBundle();
  bundle.score.model = poison;
  bundle.score.internal_consistency = poison;
  bundle.model_choice = { ...exampleJudgment('proceed'), chosen_model: poison, trait_id: poison, candidates_considered: poison };
  assert.doesNotThrow(() => validateArtifactBundle(bundle));
  assert.equal(getDisplayResult(bundle).kind, 'incomplete');
  assert.doesNotThrow(() => validateReport(exampleBundle().report, null));
});
