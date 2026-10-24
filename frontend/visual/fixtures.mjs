// SYNTHETIC VISUAL TEST FIXTURES ONLY. Never imported by the product entry points.
// No genome is analyzed here. The score bundle below is copied verbatim from
// frontend/contracts.test.js; question/refusal copy is imported from examples.js.
import { exampleQuestion, exampleRefusal } from '../examples.js';

export const FIXTURE_NOW = Date.UTC(2026, 9, 14, 18, 0);
export const FIXED_NOW = FIXTURE_NOW;
export const VIEWPORTS = {
  desktop: { width: 1440, height: 1000 },
  phone: { width: 390, height: 844 },
};

// Verbatim copies of exampleJudgment and exampleBundle in contracts.test.js.
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
// API/session envelope: expiry is solely a fixed screenshot clock fixture.
// Limits are verbatim from demo_site/input-limits.json; sample entries are the
// valid entries from frontend/demo.test.js. No sample file is read or uploaded.
export const sessionFixture = {
  authenticated: true, csrf: 'test-csrf-token', expires_at: FIXTURE_NOW / 1000 + 3600,
  limits: { upload_bytes: 1073741824, expanded_bytes: 8589934592 },
  samples: [{ name: 'sample-genome-1.txt', bytes: 15703366 }, { name: 'sample-genome-10.zip', bytes: 8247233 }],
};

// Scenario event scaffolding uses existing test event kinds/outcomes. The only
// event text is copied from demo_site/test_worker_progress.py. No invented
// scientific findings, percentages, or successful analysis are added. The run
// start is a synthetic clock offset (38 minutes), solely to exercise elapsed time.
const runningProgress = [
  { kind: 'run_started', text: 'Demo started', ts: new Date(FIXTURE_NOW - 38 * 60 * 1000).toISOString() },
  { kind: 'stage_started', stage: 'door', text: 'door started' },
  { kind: 'stage_completed', stage: 'door', outcome: 'proceed', text: 'Recognized a supported genotype file.' },
  { kind: 'stage_progress', stage: 'door', text: 'Saved provenance.' },
  { kind: 'stage_started', stage: 'ancestry' },
  { kind: 'stage_completed', stage: 'ancestry', outcome: 'proceed' },
  { kind: 'stage_started', stage: 'select' },
  { kind: 'stage_progress', stage: 'select', text: 'inspect_scorefile' },
  { kind: 'stage_completed', stage: 'select', outcome: 'proceed' },
  { kind: 'stage_started', stage: 'score' },
];
// Measured chip bootstrap estimate at review-fixes 8459945: working durations
// 3658s and 2690s from demo_site/run-times-measured.json. Runs._estimate()
// supplies floor(min/60), rounded median minutes, ceil(max/60), and the count.
// Review fixture only: production always reads its current API run.estimate.
const estimate = { low_minutes: 44, median_minutes: 53, high_minutes: 61, runs: 2 };
// A run's private link and, once it ends, when its kept result is deleted: synthetic, the
// link no token any run has, the time 24 hours after the fixture's now.
const link = '/r/' + 'example-synthetic-link-'.padEnd(43, '0');
const kept = { link, purge_at: FIXTURE_NOW / 1000 + 24 * 3600 };
const running = { status: 'running', owned: true, kind: 'chip', run_id: 'synthetic-visual-run', progress: runningProgress, estimate, estimates: { chip: estimate }, link };
const terminalProgress = ['door', 'ancestry', 'select', 'score', 'interpret', 'report'].map(stage => ({
  kind: 'stage_completed', stage, outcome: ['door', 'ancestry', 'select'].includes(stage) ? 'proceed' : 'score',
}));

export const runFixtures = {
  'sign-in': null,
  upload: { status: 'idle', owned: false, estimates: { chip: estimate } },
  busy: { status: 'busy', busy: true, owned: false, full: true, slots: 2, retry_at: FIXTURE_NOW / 1000 + 42 * 60, estimates: { chip: estimate } },
  running,
  'waiting-answer': {
    ...running, status: 'waiting', progress: runningProgress.slice(0, -2),
    question: { ...exampleQuestion, id: exampleQuestion.question_id, text: exampleQuestion.question },
  },
  offline: running,
  'finished-score': {
    status: 'completed', owned: true, run_id: 'synthetic-visual-run', ...kept,
    progress: terminalProgress, report: { bundle: exampleBundle() },
  },
  'finished-refusal': {
    status: 'completed', owned: true, run_id: 'synthetic-visual-run', ...kept,
    progress: [...terminalProgress.slice(0, 4), { kind: 'stage_completed', stage: 'interpret', outcome: 'refuse' }, { kind: 'stage_completed', stage: 'report', outcome: 'refuse' }],
    report: { bundle: { report: exampleRefusal } },
  },
  error: {
    status: 'failed', owned: true, run_id: 'synthetic-visual-run', error: 'Example process failure.', ...kept,
    progress: [{ kind: 'stage_started', stage: 'score' }, { kind: 'run_failed', error: 'Example process failure.' }],
  },
};

export const fixtureState = name => ({
  ready: true, session: structuredClone(sessionFixture), run: structuredClone(runFixtures[name]),
  error: '', notice: '', pending: '',
});

/** Intercept every API request; no worker or real account is contacted. */
export async function installFixtureRoutes(page, name) {
  if (!(name in runFixtures)) throw new Error(`Unknown visual fixture: ${name}`);
  let reads = 0;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/session') {
      await route.fulfill({ json: { ...sessionFixture, authenticated: name !== 'sign-in' } });
    } else if (path === '/api/run' && route.request().method() === 'GET') {
      if (name === 'offline' && reads++ > 0) await route.abort('internetdisconnected');
      else await route.fulfill({ json: runFixtures[name] });
    } else {
      await route.fulfill({ status: 405, json: { error: 'Visual fixture does not perform mutations.' } });
    }
  });
}

// Stable browser-importable aliases for design previews.
export const session = sessionFixture;
export const fixtures = runFixtures;
