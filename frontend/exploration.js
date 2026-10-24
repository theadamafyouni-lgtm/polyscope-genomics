/**
 * Small, source-labelled explanations for a report that the artifact boundary
 * has already accepted. This module never validates or calculates a result.
 * It preserves report wording and keeps general education explicitly separate.
 */
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = value => typeof value === 'string' && value.trim().length > 0;

function topic(example, id, label, title, sourceLabel, paragraphs, suggestedQuestion) {
  return {
    id, label, title,
    sourceLabel: `${example === true ? 'Example · ' : ''}${sourceLabel}`,
    paragraphs,
    suggestedQuestion,
  };
}

function supportTopic(example) {
  return topic(example, 'support', 'Talk it through', 'A starting point for a conversation', 'General explanation', [
    'You can bring this report and your questions to a clinician or genetic counselor. They can discuss what it means alongside your health and family history.',
    'If you want to contact someone, you can use this appointment request: “I have a genetic score report, or an explanation of why a score could not be given. Could we arrange a time to discuss what it can and cannot tell me?”',
    'Questions about your chance of developing a condition, medication, or screening belong in that conversation. This explanation cannot answer them for you.',
  ], 'What would I like a clinician or genetic counselor to help me understand about this report?');
}

function modelTopic(example, modelChoice) {
  const chosen = object(modelChoice) && text(modelChoice.chosen_model)
    && Array.isArray(modelChoice.candidates_considered)
    ? modelChoice.candidates_considered.find(candidate => object(candidate) && candidate.pgs_id === modelChoice.chosen_model)
    : null;
  if (chosen && text(chosen.why_ranked_here) && text(chosen.cost_of_choosing_it)) {
    return topic(example, 'model', 'Why this model?', 'The choice and its tradeoff', 'From this report', [
      chosen.why_ranked_here,
      chosen.cost_of_choosing_it,
    ], 'Which part of the model’s stated tradeoff would I like explained further?');
  }
  return topic(example, 'model', 'Why this model?', 'The model explanation is unavailable', 'General explanation', [
    'The complete reasoning and tradeoff for the chosen model are not included in the supplied artifacts.',
    'A model recommendation should explain why it fits your genetic context and what is lost by choosing it. Those details need to come from the recorded model selection.',
  ], 'Why was this model selected, and what is its stated tradeoff?');
}

/**
 * Accept `kind` from getDisplayResult, not an unvalidated outcome string.
 * Missing or mismatched content produces no report-specific exploration.
 */
export function buildReportTopics(input = {}) {
  if (!object(input)) return [];
  const { kind, report, modelChoice, warnings = [], example = false } = input;
  if (!object(report) || report.outcome !== kind) return [];

  if (kind === 'score') {
    if (!object(report.delivered) || !text(report.delivered.personalized_limit)) return [];
    const recordedWarnings = Array.isArray(warnings) ? warnings.filter(text) : [];
    return [
      topic(example, 'percentile', 'What does this mean?', 'A position, not a probability', 'General explanation', [
        'A percentile describes where a score sits relative to scores in its comparison group. It is not the probability that you will develop a condition.',
        'The percentile is an estimate. It can move when the calculation or comparison group changes, even for the same person.',
      ], 'What does the comparison group tell me about the meaning of this percentile?'),
      modelTopic(example, modelChoice),
      topic(example, 'limits', 'What are the limits?', 'The limits recorded for this result', 'From this report', [
        ...new Set([report.delivered.personalized_limit, ...recordedWarnings]),
      ], 'Which limitation matters most when explaining this result?'),
      supportTopic(example),
    ];
  }

  if (kind === 'refuse') {
    const envelope = report.refusal_envelope;
    if (!object(envelope)
      || !['headline', 'detail', 'what_was_tried', 'why_this_is_honest', 'what_would_change_it'].every(field => text(envelope[field]))
      || typeof envelope.is_about_the_person !== 'boolean') return [];

    const topics = [
      topic(example, 'why', 'Why no score?', 'What was tried, and why this is honest', 'From this report', [
        envelope.what_was_tried,
        envelope.why_this_is_honest,
      ], 'Which part of this explanation would I like clarified?'),
      topic(example, 'change', 'What could change?', 'What would change this answer', 'From this report', [
        envelope.what_would_change_it,
      ], 'What evidence would need to change before this question could be revisited?'),
    ];
    if (envelope.is_about_the_person === false) {
      topics.push(topic(example, 'about', 'Is this about me?', 'A limit of the data and science', 'General explanation', [
        'The report identifies this refusal as a limit of the available data, reference science, or what the assay can measure. It is not a finding about your body or your health.',
      ], 'How do the data or reference limitations explain why a score could not be given?'));
    }
    topics.push(supportTopic(example));
    return topics;
  }

  return [];
}
