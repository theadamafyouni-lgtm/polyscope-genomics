// Review-only presentation. The production renderer and scientific contracts are unchanged.
import { getDisplayResult } from '../contracts.js';
import { projectRunSession } from '../session.js';

export const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const text = value => typeof value === 'string' ? value : '';
const list = value => Array.isArray(value) ? value : [];
const descriptions = {
  'read-and-reason': ['Understand your file', 'Checking what your file contains and how it can be used.'],
  ancestry: ['Find genetic similarity', 'Checking genetic similarity to guide the analysis.'],
  select: ['Choose a model', 'Choosing a model for your question.'],
  score: ['Calculate the score', 'Calculating the score using the selected model.'],
  interpret: ['Build a comparison', 'Comparing the score with a reference group.'],
  report: ['Explain the answer', 'Preparing the result and its limits.'],
};
const statuses = { pending: 'Not yet reported', running: 'In progress', complete: 'Complete', ask: 'Your answer is needed', 'waiting-agent': 'Answer sent', refuse: 'No score', stop: 'Stopped', blocked: 'Waiting', unknown: 'Status not available', 'session-ended-by-person': 'Ended' };
export function stageInfo(run = {}) {
  const projected = projectRunSession(run);
  const questionStage = run.question?.stage === 'door' ? 'read-and-reason' : run.question?.stage;
  const terminal = ['completed', 'failed'].includes(run.status);
  const currentId = !terminal && (questionStage || projected.currentStage);
  const stages = projected.stages.map((stage, index) => ({ ...stage,
    index, label: descriptions[stage.id][0], description: descriptions[stage.id][1],
    status: !terminal && questionStage === stage.id ? 'ask' : stage.status,
  }));
  return { stages, current: stages.find(stage => stage.id === currentId) || null };
}

function timestamp(value) {
  return typeof value === 'number' ? value * 1000 : typeof value === 'string' ? Date.parse(value) : NaN;
}
export function timingInfo(run = {}, now = Date.now()) {
  const start = list(run.events ?? run.progress).find(event => event.kind === 'run_started');
  const started = timestamp(start?.ts);
  if (!Number.isFinite(started) || started > now) return {
    primary: run.question ? 'Waiting for you' : '70–100 min',
    caption: run.question ? 'The analysis needs your answer before it can continue.' : 'Typical total time. Remaining time is not available.', elapsed: '',
  };
  const elapsed = Math.floor((now - started) / 60000);
  const low = Math.max(0, 70 - elapsed);
  const high = Math.max(0, 100 - elapsed);
  return {
    primary: run.question ? 'Waiting for you' : high > 0 ? `About ${low ? `${low}–` : 'up to '}${high} min left` : 'Taking longer than usual',
    caption: run.question ? 'The analysis needs your answer before it can continue.' : 'Estimate based on a 70–100 minute run. Questions can add time.',
    elapsed: `${elapsed} min elapsed`,
  };
}

export function timelineView(run) {
  const { stages, current } = stageInfo(run);
  const notes = list(run.events ?? run.progress).filter(event => text(event.text));
  return `<ol class="run-timeline">${stages.map(stage => `<li class="${escapeHTML(stage.status)}" ${current?.id === stage.id ? 'aria-current="step"' : ''}><span class="step-number" aria-hidden="true">${stage.status === 'complete' ? '✓' : String(stage.index + 1).padStart(2, '0')}</span><div class="step-copy"><h3>${stage.label}</h3><p>${escapeHTML(statuses[stage.status] || 'Update received')}</p></div></li>`).join('')}</ol>${notes.length ? `<details class="technical-details"><summary>Technical details</summary><ol>${notes.map(event => `<li>${escapeHTML(event.stage === 'report' && event.kind === 'stage_completed' ? 'Report step updated.' : event.text)}</li>`).join('')}</ol></details>` : ''}`;
}

const fact = (label, value) => `<div><dt>${escapeHTML(label)}</dt><dd>${escapeHTML(value)}</dd></div>`;
const paragraph = (label, value) => text(value) ? `<section class="result-section"><h3>${escapeHTML(label)}</h3><p>${escapeHTML(value)}</p></section>` : '';
export function reportContent(report = {}) {
  const bundle = report.bundle || {};
  const result = getDisplayResult(bundle);
  const envelope = result.kind === 'refuse' && result.envelope ? result.envelope
    : result.kind !== 'score' && bundle.report?.outcome !== 'score' && text(report.refusal?.headline) && text(report.refusal?.detail) ? report.refusal : null;
  if (envelope) return `<div class="result-answer refusal-answer"><p class="result-kicker">Your answer</p><h1>${escapeHTML(envelope.headline)}</h1><p class="result-context">${escapeHTML(envelope.detail)}</p></div><div class="result-body">${paragraph('What was tried', envelope.what_was_tried)}${paragraph('Why this is an honest answer', envelope.why_this_is_honest)}<div class="result-limits">${paragraph('What would change it', envelope.what_would_change_it)}${envelope.is_about_the_person === false ? '<p>This limitation is about the available evidence, not a finding about your body.</p>' : ''}</div></div>`;
  if (result.kind === 'score') {
    const delivered = result.report.delivered;
    return `<div class="result-answer"><p class="result-kicker">Your result</p><h1 class="result-value">${escapeHTML(result.percentileRange.map(String).join("–"))}<span class="result-unit">percentile range</span></h1><p class="result-context">A range of positions in a comparison group, not your probability of developing a condition.</p></div><div class="result-body"><section class="result-section result-why"><p class="result-kicker">The comparison behind your result</p><h2>What this answer is based on</h2><dl class="result-facts">${fact('Comparison source', result.reference.source)}${fact('Comparison participants', String(result.reference.n_participants))}${fact('Model', result.model)}</dl></section><section class="result-section result-limits"><p class="result-kicker">Read alongside your result</p><h2>What this can tell you</h2><p>${escapeHTML(delivered.personalized_limit)}</p><p>${escapeHTML(delivered.disclaimer)}</p>${result.warnings.length ? `<h3>Limits of this comparison</h3><ul>${result.warnings.map(warning => `<li>${escapeHTML(warning)}</li>`).join('')}</ul>` : ''}</section><details class="result-details"><summary>Recorded values & reference details</summary><dl class="result-facts">${fact('Recorded score', String(result.score))}${fact('Measured weight coverage (fraction)', String(result.coverage))}${fact('Reference version', result.reference.version_string)}</dl></details></div>`;
  }
  const refusal = report.refusal || {};
  const explanation = text(refusal.explanation) || text(refusal.message) || text(refusal.text) || (!bundle.report ? text(report.result?.why) : '');
  return `<div class="result-answer"><p class="result-kicker">Your report</p><h1>A complete score report is not available.</h1><p class="result-context">${escapeHTML(explanation || 'The agent did not produce all the information needed for a complete report.')}</p></div>`;
}

export function questionContent(question, { headingTag = 'h2' } = {}) {
  headingTag = headingTag === 'h1' ? 'h1' : 'h2';
  const options = list(question.options).map(option => typeof option === 'string' ? option : text(option?.label) || text(option?.text)).filter(Boolean);
  return `<section class="question-card" aria-labelledby="live-question"><p class="eyebrow">Your answer is needed</p><${headingTag} id="live-question">${escapeHTML(question.text)}</${headingTag}>${question.why_it_matters ? `<p>${escapeHTML(question.why_it_matters)}</p>` : ''}${options.length ? `<ul>${options.map(option => `<li>${escapeHTML(option)}</li>`).join('')}</ul>` : ''}<form id="answer-form"><label for="answer">Your answer</label><textarea id="answer" name="answer" required maxlength="8000" aria-describedby="live-question"></textarea><button class="ui-button" type="submit">Send answer <span aria-hidden="true">↗</span></button><p class="answer-help">Use your own words.</p></form></section>`;
}
export function alertControl() {
  return '<button class="ui-button secondary alert-control" type="button" data-action="alerts"><svg viewBox="0 0 24 24" aria-hidden="true" width="18" height="18"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg> Enable question alerts</button>';
}
