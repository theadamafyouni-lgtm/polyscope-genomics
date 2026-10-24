// SYNTHETIC VISUAL TEST FIXTURES ONLY. Never imported by product entry points.
// Documentation fields are copied from /root/reviews/ui-fields.md. That example
// is not a complete run: it supplies neither raw scores nor reference quantiles.
// Its presentation preview must not be sent through the production validator or
// combined with a different group's scores. The complete fixture below uses the
// existing contract-test run instead, and exercises the real product entry.
import { runFixtures } from './fixtures.mjs';

export const documentedSummaryParts = {
  trait: 'This is about condition X, a made-up skin condition.',
  where: 'Your genetic score is higher than about 95 to 100 out of every 100 people whose DNA is like yours.',
  comparison: 'It is like being among the tallest people in a room: a tendency, not a prediction.',
  not_a_chance: 'It is not your chance of getting condition X.',
};
export const documentedSummary = Object.values(documentedSummaryParts).join(' ');
export const documentedLimits = [
  'The people you are compared with are all of European ancestry, and yours is partly from outside Europe, so part of where you land may reflect ancestry rather than your skin.',
  'The people you are compared with had their DNA read in full; yours was read by a chip and the rest estimated. That can shift where you land, and we cannot say which way.',
  'The range only covers the size of the comparison group; the real uncertainty is wider.',
];
export const documentedDelivery = {
  summary: documentedSummary,
  summary_parts: documentedSummaryParts,
  score: -1.39661,
  placement: { percentile_range: [95, 100], z_score: 2.08 },
  resolved_weight_fraction: 0.98,
  personalized_limit: 'It cannot tell you whether or when you will need treatment; how your skin changes over time says far more.',
  disclaimer: 'This is a comparison with other people, not a diagnosis and not your chance of getting the condition.',
  comparison_group: 'You are compared with 150 people from the 1000 Genomes Project, a public study of people from around the world, chosen as the ones whose DNA is most like yours; all are of European ancestry.',
  limits: documentedLimits,
};
export const documentedZScore = {
  value: 2.0822, patient_raw_score: -1.39661, mean: -1.6521, sd: 0.1227, sd_ddof: 1, n: 150,
  computed_from: 'reference.distribution.raw_scores: the comparison group the range is counted in',
  note: "How many of the group's standard deviations the score lies from the group's mean: a position in this group, not a risk. …",
};
const notSupplied = 'Not supplied in the documentation example';
const documentedReference = {
  source: notSupplied, version_string: notSupplied, n_participants: 150,
  // Only the supplied summary statistics. Never create a raw vector or quantiles.
  distribution: { mean: -1.6521, sd: 0.1227, n: 150 },
};
export const documentationBundle = {
  report: { delivered: documentedDelivery },
  reference_response: {
    // Explicit synthetic fixture label, taken literally from the example trait
    // sentence. The product never extracts names from prose.
    request_echo: { trait: { label: 'condition X' } },
    reference: documentedReference,
  },
};
export const documentationResult = {
  kind: 'score', report: documentationBundle.report,
  score: documentedDelivery.score,
  percentileRange: documentedDelivery.placement.percentile_range,
  zScore: documentedZScore.value,
  summary: documentedSummary, summaryParts: documentedSummaryParts,
  limits: documentedLimits, coverage: documentedDelivery.resolved_weight_fraction,
  model: notSupplied, reference: documentedReference,
};

export const documentedQuestion = {
  id: 'demo-…-q4', stage: 'select', hard_blocker: false,
  text: 'You told us your family is South Asian, but your DNA is closest to the European reference group, and the score was built in people of European ancestry. Is this file yours?',
  why_it_matters: 'If the file is someone else’s, the result would be about them, not you.',
  options: ['Yes, it is mine', 'No, it may not be mine'],
  other: true,
};

const score = structuredClone(runFixtures['finished-score']);
// Existing exact synthetic reference labels, from exampleReferenceBundle() in
// contracts.test.js. This fixture proves the headline uses explicit metadata.
score.report.bundle.reference_response.request_echo = {
  trait: { id: [{ id: 'EXAMPLE_1000', label: 'Example catalog label' }], label: 'Example request label' },
};
const waiting = structuredClone(runFixtures['waiting-answer']);
const question = { ...waiting, question: documentedQuestion };
export const round3Runs = {
  score,
  refusal: structuredClone(runFixtures['finished-refusal']),
  question,
  'question-options-only': { ...waiting, question: { ...documentedQuestion, id: 'synthetic-options-only', other: false } },
  'question-freeform': { ...waiting, question: {
    id: 'synthetic-freeform', stage: documentedQuestion.stage, hard_blocker: false,
    text: documentedQuestion.text, why_it_matters: documentedQuestion.why_it_matters,
  } },
};
export const provenance = {
  syntheticTestFixturesOnly: true, noAnalysisPerformed: true, noRealRunData: true,
  documentationSource: '/root/reviews/ui-fields.md',
  documentationPreview: 'Exact documented report fields, rendered by the same presentation component. Not a complete or validated run. No raw comparison vector or quantiles were supplied; no comparison figures are invented.',
  documentationTrait: 'Explicit synthetic label condition X, copied from the documented trait sentence. Production uses only explicit recorded metadata.',
  integrationFixture: 'Existing complete synthetic contract-test bundle; its three recorded comparison scores are unchanged. Exact exampleReferenceBundle trait label added for the headline.',
  question: 'Exact question example from ui-fields.md. Options-only and freeform variants exercise permitted API shapes; no scientific answer is invented.',
};
