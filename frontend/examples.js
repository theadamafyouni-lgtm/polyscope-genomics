// EXAMPLE COPY ONLY. No genome, persona, clinical metric, or calculated result.
// These examples are never used to interpret a selected file or a person's answer.
export const exampleRefusal = {
  outcome: 'refuse',
  decision: 'Example of a reference-related refusal; no analysis performed.',
  refusal_envelope: {
    headline: 'You deserve a comparison we can stand behind.',
    detail: 'We cannot give you a meaningful percentile from the reference data available for this analysis.',
    what_was_tried: 'We tried to build a comparison group of people genetically similar to you, scored using the same model.',
    why_this_is_honest: 'There are not enough sufficiently similar participants to make that comparison reliable. Giving you a number would suggest more certainty than the data supports.',
    what_would_change_it: 'A reference that includes more people genetically similar to you could make a meaningful comparison possible. A future analysis would need to check this again.',
    is_about_the_person: false,
  },
  delivered: { score: null, placement: null, resolved_weight_fraction: null, personalized_limit: null, disclaimer: 'This is not a diagnosis or a prediction of whether you will develop a condition.' },
  follow_up_state: 'open',
  requirement_ids: ['REP-6', 'REP-7'],
  reasons: ['Illustrative reference refusal, authored for interface review. No analysis was performed.'],
  evidence: [{ source: 'frontend/examples.js', field: 'exampleRefusal (illustrative copy only)' }],
};

export const exampleQuestion = {
  kind: 'question', question_id: 'example-trait-question', stage: 'select',
  question: 'Are you asking about developing a condition, or its severity after a diagnosis?',
  why_it_matters: 'Those are different questions. A model that studies severity after diagnosis cannot tell you about developing the condition.',
  hard_blocker: true,
  options: ['Developing the condition', 'Severity after a diagnosis', 'I’m not sure'],
};

export const SCREEN_OPTIONS = [
  ['home', 'About prs-agent'],
  ['upload', 'Start · Your file'], ['file', 'File findings'], ['question', 'Your question'],
  ['ancestry', 'Ancestry disagreement'], ['model', 'Model recommendation'],
  ['progress', 'Progress & conversation'], ['result', 'Score report · empty'],
  ['refusal', 'Refusal · example'], ['stop', 'Stop · example'], ['connection', 'Connection options'],
  ['artifacts', 'Open a saved report'],
];
