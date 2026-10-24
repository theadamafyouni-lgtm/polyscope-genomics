/**
 * Read-only boundary for the JSON artifacts described in agent/.claude/skills.
 * This validates shape and cross-artifact consistency; it makes no clinical or
 * scientific judgments and never computes a score, percentile, or ancestry.
 */
import { createSessionState, applySessionEvent } from './session.js';

export const STAGES = Object.freeze([
  'read-and-reason', 'ancestry', 'select', 'score', 'interpret', 'report',
]);

const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = value => typeof value === 'string' && value.trim().length > 0;
const number = value => typeof value === 'number' && Number.isFinite(value);
const fraction = value => number(value) && value >= 0 && value <= 1;
const percentile = value => number(value) && value >= 0 && value <= 100;
// A placement is a range, low to high: never one number.
const range = value => Array.isArray(value) && value.length === 2 && value.every(percentile) && value[0] <= value[1];
const issue = (path, code, message) => ({ path, code, message });
const hasEvidence = artifact => Array.isArray(artifact?.evidence) && artifact.evidence.length > 0
  && artifact.evidence.every(item => object(item) && text(item.source));
const hasTextList = value => Array.isArray(value) && value.length > 0 && value.every(text);
// The report's summary says these, in this order (the report skill; record.py checks them).
const SUMMARY_PARTS = ['trait', 'where', 'comparison', 'not_a_chance'];
const traitId = value => typeof value === 'string' ? value
  : Array.isArray(value) && value.length === 1 && object(value[0]) && typeof value[0].id === 'string' ? value[0].id
    : null;
const parityValue = value => typeof value === 'string' ? value.trim().split(/[:\s]/, 1)[0] : value;

function validateMatchingHashes(entries, code, message) {
  const recorded = entries.filter(([, value]) => value !== undefined);
  return recorded.filter(([, value]) => typeof value !== 'string' || !/^[a-fA-F0-9]{64}$/.test(value)
    || recorded.some(([, other]) => other !== value)).map(([path]) => issue(path, code, message));
}

/** `blocked` and voluntary ending are orchestration states, not agent refusals. */
export function normalizeOutcome(stage, outcome) {
  if (['refuse', 'stop', 'ask', 'blocked', 'session-ended-by-person'].includes(outcome)) return outcome;
  if (outcome === 'proceed') return stage === 'report' ? 'unknown' : 'continue';
  if (outcome === 'score') return stage === 'report' ? 'score' : 'continue';
  return 'unknown';
}

/** Canonical bundle plus explicit aliases for the skill's file/stage names. */
export function readArtifacts(bundle = {}) {
  if (!object(bundle)) bundle = {};
  const stages = object(bundle.stages) ? bundle.stages : {};
  return {
    classification: bundle.classification ?? stages['read-and-reason'] ?? stages.door,
    ancestry: bundle.ancestry ?? stages.ancestry,
    model_choice: bundle.model_choice ?? stages.select,
    score: bundle.score ?? stages.score,
    coverage: bundle.coverage ?? bundle.measured_coverage,
    interpret: bundle.interpret ?? stages.interpret,
    report: bundle.report ?? stages.report,
    reference_response: bundle.reference_response,
    percentile_range: bundle.percentile_range,
  };
}

function validateJudgment(value, path, stage) {
  if (!object(value)) return [issue(path, 'object_required', 'The stage artifact must be a JSON object.')];
  const issues = [];
  if (normalizeOutcome(stage, value.outcome) === 'unknown') {
    issues.push(issue(`${path}.outcome`, 'unknown_outcome', 'A recognized stage outcome is required.'));
  }
  if (!text(value.decision)) issues.push(issue(`${path}.decision`, 'decision_required', 'The agent must explain its decision.'));
  if (!hasTextList(value.reasons)) issues.push(issue(`${path}.reasons`, 'reasons_required', 'The judgment must carry its reasons.'));
  if (!hasEvidence(value)) issues.push(issue(`${path}.evidence`, 'evidence_required', 'The judgment must identify its source artifacts.'));
  if (['refuse', 'stop', 'ask'].includes(value.outcome) && !hasTextList(value.requirement_ids)) {
    issues.push(issue(`${path}.requirement_ids`, 'requirements_required', 'This outcome must identify the requirements behind it.'));
  }
  return issues;
}

function validateRefusal(report) {
  const envelope = report.refusal_envelope;
  if (!object(envelope)) return [issue('report.refusal_envelope', 'envelope_required', 'The refusal needs the person-facing refusal envelope.')];
  const issues = [];
  const fields = ['headline', 'detail', 'what_was_tried', 'why_this_is_honest', 'what_would_change_it'];
  for (const field of fields) {
    if (!text(envelope[field])) issues.push(issue(`report.refusal_envelope.${field}`, 'copy_required', 'The refusal must include this person-facing explanation.'));
  }
  if (typeof envelope.is_about_the_person !== 'boolean') {
    issues.push(issue('report.refusal_envelope.is_about_the_person', 'scope_required', 'The envelope must say whether the limitation is about the person.'));
  }
  const prose = fields.map(field => typeof envelope[field] === 'string' ? envelope[field] : '').join(' ');
  // Narrow checks for obvious recording language; a machine cannot certify tone.
  if (/\bREP-\d+\b|\b(?:told|explained to|informed) (?:them|the patient|the person)\b|\bnamed specifically:/i.test(prose)) {
    issues.push(issue('report.refusal_envelope', 'log_register', 'Use the envelope as direct delivery to the person, not a record of what was said.'));
  }
  if (prose && !/\byou(?:r|rs|'re|'ve|'ll)?\b/i.test(prose)) {
    issues.push(issue('report.refusal_envelope', 'direct_address_required', 'The refusal envelope should address you directly.'));
  }
  return issues;
}

function validateReference(response) {
  const path = 'reference_response';
  if (!object(response) || response.status !== 'reference_valid' || response.interpret_can_proceed !== true) {
    return [issue(path, 'validated_reference_required', 'A validated, usable reference response is required before displaying a score.')];
  }
  const ref = response.reference;
  if (!object(ref) || ref.usable !== true) {
    return [issue(`${path}.reference`, 'usable_reference_required', 'The provider must explicitly report a usable reference.')];
  }
  const issues = [];
  for (const field of ['source', 'version_string', 'ancestry_grouping_definition', 'cohort_construction_rule', 'assay_layer']) {
    const structured = !['source', 'version_string'].includes(field) && object(ref[field]) && Object.keys(ref[field]).length;
    if (!text(ref[field]) && !structured) {
      issues.push(issue(`${path}.reference.${field}`, 'reference_metadata_required', `The reference must identify ${field}.`));
    }
  }
  if (!Number.isInteger(ref.n_participants) || ref.n_participants <= 0) {
    issues.push(issue(`${path}.reference.n_participants`, 'participant_count_required', 'The provider must report its actual participant count.'));
  }
  if (!number(ref.too_few_threshold_used) || ref.too_few_threshold_used < 0) {
    issues.push(issue(`${path}.reference.too_few_threshold_used`, 'threshold_required', 'The provider must state the minimum cohort size it used.'));
  } else if (number(ref.n_participants) && ref.n_participants < ref.too_few_threshold_used) {
    issues.push(issue(`${path}.reference.n_participants`, 'reference_contradiction', 'The reported usable cohort is below the provider’s own minimum.'));
  }
  if (!['same-resolved-set', 'full-model-set', 'other'].includes(ref.scoring_parity)) {
    issues.push(issue(`${path}.reference.scoring_parity`, 'scoring_parity_required', 'The provider must state how its scoring compares with the person’s scoring.'));
  }
  const distribution = ref.distribution;
  if (!object(distribution)) {
    issues.push(issue(`${path}.reference.distribution`, 'distribution_required', 'The validated reference distribution must be present.'));
  } else {
    const raw = Array.isArray(distribution.raw_scores) && distribution.raw_scores.length > 0 && distribution.raw_scores.every(number);
    const quantiles = ['p01', 'p05', 'p10', 'p25', 'p50', 'p75', 'p90', 'p95', 'p99'].every(key => number(distribution[key]));
    if (!raw && !quantiles) issues.push(issue(`${path}.reference.distribution`, 'distribution_incomplete', 'The provider must supply its scores or the full required quantile set.'));
    for (const field of ['mean', 'sd', 'n']) {
      if (!number(distribution[field])) issues.push(issue(`${path}.reference.distribution.${field}`, 'distribution_metadata_required', 'The reference distribution must include mean, standard deviation and size.'));
    }
    if (number(distribution.sd) && distribution.sd <= 0) issues.push(issue(`${path}.reference.distribution.sd`, 'distribution_invalid', 'A usable distribution needs a positive standard deviation.'));
    if (number(distribution.n) && (!Number.isInteger(distribution.n) || distribution.n <= 0)) issues.push(issue(`${path}.reference.distribution.n`, 'distribution_invalid', 'The distribution size must be a positive integer.'));
  }
  if (!Array.isArray(response.warnings_for_report) || !response.warnings_for_report.every(text)) {
    issues.push(issue(`${path}.warnings_for_report`, 'warnings_required', 'The adapter’s report warnings must be supplied, including an empty list when there are none.'));
  }
  return issues;
}

function measuredCoverage(coverage) {
  if (!object(coverage)) return false;
  if (coverage.is_an_estimate === true || coverage.measured_not_estimated === false) return false;
  if (coverage.tool === 'weight_coverage.py measure') return true;
  // Explicit bridge form for a backend that attaches source provenance to artifacts.
  return coverage.measured_not_estimated === true && text(coverage.source)
    && /(?:^|\/)score\/coverage_(?!est_)[^/]+\.json$/.test(coverage.source)
    && (!text(coverage.tool) || !/estimate/i.test(coverage.tool));
}

function writtenPlaces(value) {
  const written = String(value);
  if (/e/i.test(written)) return 20;
  const point = written.indexOf('.');
  return point < 0 ? 0 : written.length - point - 1;
}

// Two recorded numbers are one when they differ only by a float round trip, or, where the
// coarser was written with at least minPlaces decimals, by rounding to what was written: a
// delivered 0.9473 is the measured 0.947316258104925, and -0.8271303999999999 is the
// -0.8271304 it was copied from. Anything further apart is a conflict, as before.
function sameRecorded(a, b, minPlaces = Infinity) {
  if (!number(a) || !number(b)) return a === b;
  const gap = Math.abs(a - b);
  if (gap <= 1e-9 * Math.max(1, Math.abs(a), Math.abs(b))) return true;
  const places = Math.min(writtenPlaces(a), writtenPlaces(b));
  return places >= minPlaces && gap <= 0.5 * 10 ** -places * (1 + 1e-9);
}

function numericScore(report, score) {
  const delivered = report?.delivered?.score;
  if (number(delivered)) return delivered;
  if (object(delivered) && number(delivered.value)) return delivered.value;
  return number(score?.raw_score) ? score.raw_score : null;
}

function deliveredRange(report) {
  const placement = report?.delivered?.placement;
  return object(placement) && range(placement.percentile_range) ? placement.percentile_range : null;
}

// Each bound the same recorded number: the report may round what it says to whole percents.
const sameRange = (a, b, minPlaces) => range(a) && range(b) && a.every((value, i) => sameRecorded(value, b[i], minPlaces));

export function validateReport(report, context = {}) {
  const issues = validateJudgment(report, 'report', 'report');
  if (!object(report)) return issues;
  if (!['score', 'refuse'].includes(report.outcome)) {
    issues.push(issue('report.outcome', 'final_report_outcome', 'A completed report is either a score or a refusal; questions and blocked jobs remain open.'));
    return issues;
  }
  if (report.outcome === 'refuse') return [...issues, ...validateRefusal(report)];
  const { interpret, coverage, score, reference_response, model_choice, percentile_range: ranged } = object(context) ? context : {};
  issues.push(...validateReference(reference_response));
  if (!object(report.delivered)) {
    issues.push(issue('report.delivered', 'delivery_required', 'The score needs its delivered explanation and limits.'));
  } else {
    for (const field of ['personalized_limit', 'disclaimer']) {
      if (!text(report.delivered[field])) issues.push(issue(`report.delivered.${field}`, 'explanation_required', 'The report must include its personalized limit and disclaimer.'));
    }
    // Two layers: the short plain summary first, then the details with every limit in plain words.
    if (!text(report.delivered.summary)) issues.push(issue('report.delivered.summary', 'summary_required', 'The report must lead with its short plain-words summary.'));
    if (!hasTextList(report.delivered.limits)) issues.push(issue('report.delivered.limits', 'limits_required', 'The report must carry its limits in plain words.'));
  }
  if (!object(interpret) || interpret.outcome !== 'score' || !hasEvidence(interpret)) {
    issues.push(issue('interpret', 'interpretation_required', 'A recorded interpretation with source evidence is required.'));
  }
  if (!range(interpret?.percentile_range)) issues.push(issue('interpret.percentile_range', 'percentile_range_required', 'The interpreted percentile must be a range from 0 to 100, low to high; one number is not a placement.'));
  if (!object(ranged) || ranged.tool !== 'rank_range.py' || !range(ranged.percentile_range)) {
    issues.push(issue('percentile_range', 'range_tool_required', 'The percentile range must come from rank_range.py, from the comparison group\u2019s counts.'));
  } else {
    if (range(interpret?.percentile_range) && !sameRange(interpret.percentile_range, ranged.percentile_range, 1)) issues.push(issue('interpret.percentile_range', 'range_mismatch', 'The interpreted range disagrees with rank_range.py.'));
    if (object(reference_response?.reference) && ranged.n !== reference_response.reference.n_participants) issues.push(issue('percentile_range.n', 'range_reference_mismatch', 'The range was counted in a different comparison group.'));
  }
  // The z-score: rank_range.py's, from the same group's own scores, as interpret recorded it.
  const zTool = object(ranged) ? ranged.z_score : null;
  if (!object(zTool) || !number(zTool.value) || !number(zTool.mean) || !(number(zTool.sd) && zTool.sd > 0)) {
    issues.push(issue('percentile_range.z_score', 'z_tool_required', 'The z-score must come from rank_range.py, from the comparison group\u2019s own scores.'));
  } else {
    if (zTool.n !== ranged.n) issues.push(issue('percentile_range.z_score.n', 'z_group_mismatch', 'The z-score was taken in a different group from the range.'));
    if (!number(interpret?.z_score)) issues.push(issue('interpret.z_score', 'z_required', 'The interpretation must record the z-score rank_range.py wrote.'));
    else if (!sameRecorded(interpret.z_score, zTool.value, 2)) issues.push(issue('interpret.z_score', 'z_mismatch', 'The interpreted z-score disagrees with rank_range.py.'));
  }
  if (object(interpret?.reference) && object(reference_response?.reference)) {
    for (const [summaryKey, providerKey] of [['n', 'n_participants'], ['scoring_parity', 'scoring_parity']]) {
      const value = summaryKey === 'scoring_parity' ? parityValue(interpret.reference[summaryKey]) : interpret.reference[summaryKey];
      if (interpret.reference[summaryKey] !== undefined && value !== reference_response.reference[providerKey]) {
        issues.push(issue(`interpret.reference.${summaryKey}`, 'reference_mismatch', 'The interpretation disagrees with the provider reference metadata.'));
      }
    }
  }
  const placement = deliveredRange(report);
  const deliveredZ = report.delivered?.placement?.z_score;
  if (!number(deliveredZ)) issues.push(issue('report.delivered.placement.z_score', 'delivered_z_required', 'The report must deliver the recorded z-score.'));
  else if (number(interpret?.z_score) && !sameRecorded(deliveredZ, interpret.z_score, 1)) issues.push(issue('report.delivered.placement.z_score', 'delivered_z_mismatch', 'The delivered z-score disagrees with the interpretation.'));
  if (placement === null) issues.push(issue('report.delivered.placement.percentile_range', 'delivered_range_required', 'The report must deliver the percentile as the recorded range, never one number.'));
  else if (range(interpret?.percentile_range) && !sameRange(placement, interpret.percentile_range, 0)) {
    issues.push(issue('report.delivered.placement', 'placement_mismatch', 'The delivered percentile range disagrees with the interpretation.'));
  }
  if (!measuredCoverage(coverage)) issues.push(issue('coverage', 'measured_coverage_required', 'Use the measured score/coverage artifact, never the selection estimate.'));
  if (!fraction(coverage?.resolved_weight_fraction)) issues.push(issue('coverage.resolved_weight_fraction', 'coverage_invalid', 'Measured weight coverage must be a finite fraction from 0 to 1.'));
  if (!fraction(report.delivered?.resolved_weight_fraction)) issues.push(issue('report.delivered.resolved_weight_fraction', 'coverage_required', 'The delivered report must carry measured weight coverage.'));
  else if (!sameRecorded(report.delivered.resolved_weight_fraction, coverage?.resolved_weight_fraction, 2)) {
    issues.push(issue('report.delivered.resolved_weight_fraction', 'coverage_mismatch', 'The delivered coverage disagrees with the measured artifact.'));
  }
  if (numericScore(report, score) === null) issues.push(issue('report.delivered.score', 'score_required', 'A finite recorded score is required; display never derives or invents one.'));
  if (number(score?.raw_score) && !sameRecorded(numericScore(report, score), score.raw_score, 4)) issues.push(issue('report.delivered.score', 'score_mismatch', 'The delivered score disagrees with the scoring artifact.'));
  if (score && (score.outcome && score.outcome !== 'score' || text(score.internal_consistency) && /INCONSISTENT/i.test(score.internal_consistency) || number(score.shortfall) && score.shortfall !== 0)) {
    issues.push(issue('score', 'scoring_inconsistent', 'The scoring artifact does not support a completed score.'));
  }
  const modelIds = [
    ['model_choice.chosen_model', model_choice?.chosen_model],
    ['score.model', score?.model],
    ['reference_response.request_echo.model.pgs_id', reference_response?.request_echo?.model?.pgs_id],
    ['reference_response.reference.model.pgs_id', reference_response?.reference?.model?.pgs_id],
  ].filter(([, value]) => value !== undefined && value !== null);
  if (!modelIds.length) issues.push(issue('score.model', 'score_model_required', 'A score must identify the model it was computed with.'));
  for (const [path, value] of modelIds) {
    if (!text(value) || !/^PGS\d{6}$/.test(value)) issues.push(issue(path, 'model_id_required', 'The scoring model must be one bare PGS identifier.'));
    else if (modelIds.some(([, other]) => other !== value)) issues.push(issue(path, 'model_identity_mismatch', 'The model identities disagree across the score, choice and reference request.'));
  }
  const request = reference_response?.request_echo;
  const reference = reference_response?.reference;
  issues.push(...validateMatchingHashes([
    ['reference_response.request_echo.model.sha256', request?.model?.sha256],
    ['reference_response.reference.model.sha256', reference?.model?.sha256],
  ], 'model_hash_mismatch', 'The reference request and provider must identify the same model file hash.'));
  issues.push(...validateMatchingHashes([
    ['score.score_input_sha256', score?.score_input_sha256],
    ['reference_response.request_echo.patient.score_input_sha256', request?.patient?.score_input_sha256],
    ['reference_response.reference.scoring_parity_detail.score_input_sha256', reference?.scoring_parity_detail?.score_input_sha256],
    ['reference_response.reference.scoring_parity_detail.score_input_sha256_in_request', reference?.scoring_parity_detail?.score_input_sha256_in_request],
  ], 'score_input_hash_mismatch', 'The score and reference must identify the same scoring input hash.'));
  if (object(request?.patient)) {
    for (const [key, expected] of [['raw_score', numericScore(report, score)], ['resolved_weight_fraction', coverage?.resolved_weight_fraction]]) {
      if (request.patient[key] !== undefined && !sameRecorded(request.patient[key], expected, key === 'raw_score' ? 4 : 2)) issues.push(issue(`reference_response.request_echo.patient.${key}`, 'reference_request_mismatch', 'The reference was requested for different score or coverage values.'));
    }
  }
  if (model_choice?.trait_id !== undefined && request?.trait?.id !== undefined
    && (traitId(model_choice.trait_id) === null || traitId(request.trait.id) !== traitId(model_choice.trait_id))) issues.push(issue('reference_response.request_echo.trait.id', 'trait_identity_mismatch', 'The reference request and model choice identify different traits.'));
  if (!Array.isArray(interpret?.caveats_for_report) || !interpret.caveats_for_report.every(text)) issues.push(issue('interpret.caveats_for_report', 'caveats_required', 'The interpretation’s report caveats must be supplied.'));
  return issues;
}

/** Partial bundles are valid; absent stages are pending, never fabricated. */
export function validateArtifactBundle(bundle) {
  if (!object(bundle)) return [issue('bundle', 'object_required', 'Import a JSON object containing stage artifacts.')];
  const a = readArtifacts(bundle);
  const issues = [];
  const mapping = { classification: 'read-and-reason', ancestry: 'ancestry', model_choice: 'select', score: 'score', interpret: 'interpret' };
  for (const [name, stage] of Object.entries(mapping)) {
    if (a[name] === undefined) continue;
    // raw_score.json is a mechanical facts artifact; score.json is a judgment.
    if (name === 'score' && a[name]?.tool === 'score_plink2.py' && a[name]?.outcome === undefined) continue;
    issues.push(...validateJudgment(a[name], name, stage));
  }
  const c = a.classification;
  if (c && normalizeOutcome('read-and-reason', c.outcome) === 'continue') {
    for (const key of ['assay_class', 'absence_means', 'confidence']) if (!text(c[key])) issues.push(issue(`classification.${key}`, 'classification_required', 'The file classification must explain its assay, absence semantics and confidence.'));
    if (!object(c.genome_build) || !text(c.genome_build.value) || !hasTextList(c.genome_build.verified_by)) issues.push(issue('classification.genome_build', 'build_evidence_required', 'The genome build needs its recorded verification evidence.'));
  }
  const ancestry = a.ancestry;
  if (ancestry && normalizeOutcome('ancestry', ancestry.outcome) === 'continue') {
    if (!text(ancestry.inferred_group)) issues.push(issue('ancestry.inferred_group', 'ancestry_required', 'The genetic ancestry judgment is missing.'));
    if (!['agree', 'partial', 'disagree', 'no-self-report'].includes(ancestry.agreement)) issues.push(issue('ancestry.agreement', 'agreement_required', 'State whether self-report and inferred ancestry agree, agree in part, differ, or no self-report was given.'));
    if (!text(ancestry.used_for_math) || !/^inferred\b/.test(ancestry.used_for_math)) issues.push(issue('ancestry.used_for_math', 'inferred_math_required', 'Model and reference selection must use inferred genetic similarity.'));
    // Shown before model selection; or, for a person who answered up front, told in the report.
    const toldInReport = ancestry.surface_in_report === true && text(a.report?.delivered?.ancestry_note);
    if (['partial', 'disagree'].includes(ancestry.agreement) && ancestry.surfaced_to_person !== true && !toldInReport) issues.push(issue('ancestry.surfaced_to_person', 'mismatch_not_surfaced', 'A partial or full ancestry mismatch must be shown before model selection, or told in the report.'));
  }
  const choice = a.model_choice;
  if (choice && normalizeOutcome('select', choice.outcome) === 'continue') {
    if (!text(choice.chosen_model) || !/^PGS\d{6}$/.test(choice.chosen_model)) issues.push(issue('model_choice.chosen_model', 'model_id_required', 'The chosen model must be one bare PGS identifier.'));
    if (!text(traitId(choice.trait_id)) || !/^[A-Za-z]+[_:]\d+$/.test(traitId(choice.trait_id))) issues.push(issue('model_choice.trait_id', 'trait_id_required', 'The resolved trait must identify one ontology identifier.'));
    // up-front: the person agreed on the upload form, and nobody was asked to confirm it.
    if (!['transparency', 'decision', 'up-front'].includes(choice.gate_type)) issues.push(issue('model_choice.gate_type', 'gate_required', 'The model choice must distinguish acknowledgment from a value decision.'));
    if (!Array.isArray(choice.candidates_considered) || !choice.candidates_considered.length || choice.candidates_considered.some(candidate => !object(candidate) || !text(candidate.pgs_id) || !text(candidate.why_ranked_here) || !text(candidate.cost_of_choosing_it))) issues.push(issue('model_choice.candidates_considered', 'candidate_cost_required', 'Each recommendation must show its reasoning and cost.'));
    if (Array.isArray(choice.candidates_considered) && !choice.candidates_considered.some(candidate => object(candidate) && candidate.pgs_id === choice.chosen_model)) issues.push(issue('model_choice.chosen_model', 'chosen_candidate_missing', 'The recommended model must appear in the ranked candidates with its reasoning and cost.'));
    if (!object(choice.coverage_estimate) || !fraction(choice.coverage_estimate.resolved_weight_fraction) || choice.coverage_estimate.is_an_estimate !== true) issues.push(issue('model_choice.coverage_estimate', 'estimate_label_required', 'Selection coverage must be supplied and explicitly labeled as an estimate.'));
  }
  if (a.report !== undefined) issues.push(...validateReport(a.report, a));
  if (a.report?.outcome === 'score') {
    for (const name of Object.keys(mapping)) {
      if (['refuse', 'stop', 'ask', 'blocked', 'session-ended-by-person'].includes(a[name]?.outcome)) {
        issues.push(issue(`${name}.outcome`, 'unfinished_before_report', 'A score report conflicts with an earlier stage that has not continued.'));
      }
    }
    if (Array.isArray(bundle.events) && bundle.events.length) {
      const session = bundle.events.reduce(applySessionEvent, createSessionState());
      if (['ask', 'waiting-agent', 'blocked', 'refuse', 'stop', 'error', 'session-ended-by-person'].includes(session.status)) issues.push(issue('events', 'report_event_conflict', 'The score report conflicts with the run’s latest recorded event state.'));
    }
  }
  return issues;
}

/** Safe display view. No numeric results are exposed until all report checks pass. */
export function getDisplayResult(bundle = {}) {
  if (!object(bundle)) return { kind: 'incomplete', issues: validateArtifactBundle(bundle) };
  const a = readArtifacts(bundle);
  const issues = validateArtifactBundle(bundle);
  const empty = { issues, score: null, percentileRange: null, zScore: null, summary: null, summaryParts: null, limits: [], coverage: null, reference: null, warnings: [], provenance: [] };
  if (bundle.session_ended_by_person === true || bundle.status === 'session-ended-by-person') return { ...empty, kind: 'session-ended-by-person' };
  if (a.report) {
    if (issues.length) return { ...empty, kind: 'incomplete' };
    if (a.report.outcome === 'refuse') return { ...empty, kind: 'refuse', report: a.report, envelope: a.report.refusal_envelope };
    return {
      ...empty, kind: 'score', report: a.report,
      model: a.model_choice?.chosen_model ?? a.score?.model ?? a.reference_response?.request_echo?.model?.pgs_id,
      score: numericScore(a.report, a.score), percentileRange: a.interpret.percentile_range, zScore: a.interpret.z_score,
      summary: a.report.delivered.summary, limits: a.report.delivered.limits,
      // The summary's parts in the order it says them, where the report recorded them.
      summaryParts: SUMMARY_PARTS.every(part => text(a.report.delivered.summary_parts?.[part]))
        ? Object.fromEntries(SUMMARY_PARTS.map(part => [part, a.report.delivered.summary_parts[part]])) : null,
      coverage: a.coverage.resolved_weight_fraction,
      reference: a.reference_response.reference,
      warnings: [...new Set([...a.reference_response.warnings_for_report, ...a.interpret.caveats_for_report])],
      provenance: [
        { value: 'score', source: number(a.report.delivered?.score) || number(a.report.delivered?.score?.value) ? 'report.delivered.score' : 'score.raw_score', evidence: a.report.evidence },
        { value: 'percentile_range', source: 'interpret.percentile_range', evidence: a.interpret.evidence },
        { value: 'z_score', source: 'interpret.z_score', evidence: a.interpret.evidence },
        { value: 'coverage', source: a.coverage.source ?? a.coverage.tool, evidence: a.coverage.evidence ?? [] },
        { value: 'reference', source: a.reference_response.reference.source, version: a.reference_response.reference.version_string },
      ],
    };
  }
  const artifact = [a.interpret, a.score, a.model_choice, a.ancestry, a.classification].find(value => object(value) && value.outcome);
  if (artifact && ['refuse', 'stop', 'ask', 'session-ended-by-person'].includes(artifact.outcome)) return { ...empty, kind: artifact.outcome, artifact };
  if (bundle.status === 'blocked' || ['reference_unavailable', 'reference_invalid', 'request_missing'].includes(a.reference_response?.status)) return { ...empty, kind: 'blocked' };
  return { ...empty, kind: a.score || a.interpret ? 'incomplete' : 'pending' };
}
