import { getDisplayResult, readArtifacts } from './contracts.js';
import { projectRunSession } from './session.js';
import { helixView } from './helix.js';
import { referenceChart } from './result-chart.js';
import { questionView } from './question.js';
import { consentView, intakeView } from './intake.js';
import { escapeHTML } from './html.js';

const list = value => Array.isArray(value) ? value : [];
const text = value => typeof value === 'string' ? value : '';
// Words the report delivered for the person, if it said any.
const said = value => text(value).trim().length > 0;
const e = escapeHTML;
const bytes = value => value >= 1024 ** 3 && value % 1024 ** 3 === 0 ? `${value / 1024 ** 3} GiB` : `${value / (1024 * 1024)} MiB`;
const arrow = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>';
const check = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="m5 12 4 4L19 6" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>';
const descriptions = {
  'read-and-reason': ['Understand your file', 'Checking what your file contains and how it can be used.'],
  ancestry: ['Find genetic similarity', 'Checking genetic similarity to guide the analysis.'],
  select: ['Choose how to calculate', 'Choosing a scoring method for your question.'],
  score: ['Calculate the score', 'Calculating the score using the chosen method.'],
  interpret: ['Compare your score', 'Comparing your score with a group of people.'],
  report: ['Explain the answer', 'Preparing the result and its limits.'],
};
const statuses = { pending: 'Not yet reported', running: 'In progress', complete: 'Complete', ask: 'Needs your answer', 'waiting-agent': 'Answer sent', refuse: 'No score', stop: 'Stopped', blocked: 'Waiting', unknown: 'Status not available', 'session-ended-by-person': 'Ended' };
export function stageInfo(run = {}) {
  const projected = projectRunSession(run);
  const terminal = ['completed', 'failed'].includes(run.status);
  const questionStage = run.question?.stage === 'door' ? 'read-and-reason' : run.question?.stage;
  const stages = projected.stages.map((stage, index) => ({ ...stage, index,
    label: descriptions[stage.id][0], description: descriptions[stage.id][1],
    status: !terminal && questionStage === stage.id ? 'ask' : stage.status,
  }));
  return { stages, current: terminal ? null : stages.find(stage => stage.id === (questionStage || projected.currentStage)) || null };
}
// The worker's estimate from measured runs, in whole minutes, low to high. The time a run waits
// for its person's answers is in neither the estimate nor what is counted against it.
function estimateOf(run) {
  const low = run?.estimate?.low_minutes, high = run?.estimate?.high_minutes;
  return Number.isInteger(low) && Number.isInteger(high) && low > 0 && low <= high ? { low, high } : null;
}
export function timingInfo(run = {}, now = Date.now()) {
  const start = list(run.events ?? run.progress).find(event => event.kind === 'run_started');
  const started = typeof start?.ts === 'number' ? start.ts * 1000 : Date.parse(start?.ts);
  const elapsed = Number.isFinite(started) && started <= now ? Math.floor((now - started) / 60000) : null;
  const range = estimateOf(run), usual = range && `${range.low}–${range.high}`;
  const worked = elapsed === null ? null : Math.max(0, elapsed - Math.floor(Math.max(0, Number(run.waited_seconds) || 0) / 60));
  const kind = { chip: 'genotype-file ', whole_genome: 'whole-genome ' }[run.kind] || '';
  const measuredCaption = usual ? `An estimate from recent ${kind}runs, which took ${usual} minutes. Questions can add time.` : 'Questions can add time.';
  if (run.question) return { elapsed, worked, range, primary: 'Waiting for your answer', caption: measuredCaption };
  if (!range) return { elapsed, worked, range, primary: 'Some steps take a while', caption: 'Questions can add time.' };
  if (elapsed === null) return { elapsed, worked, range, primary: `Usually ${usual} minutes`, caption: 'Start time is unavailable, so we cannot estimate how long is left.' };
  const high = Math.max(0, range.high - worked), low = Math.max(0, range.low - worked);
  return { elapsed, worked, range, primary: high ? `About ${low ? `${low}–` : 'up to '}${high} min left` : 'Taking longer than usual', caption: measuredCaption };
}
function timeView(run, now) {
  const timing = timingInfo(run, now), range = timing.range;
  return `<div class="time-track"><div class="time-heading"><div><p class="time-remaining">${e(timing.primary)}</p><p class="time-caption">${e(timing.caption)}</p></div>${timing.elapsed !== null ? `<p class="elapsed"><strong>${timing.elapsed}</strong> min so far</p>` : ''}</div>${timing.elapsed !== null && range ? `<div class="elapsed-track"><progress max="${range.high}" value="${Math.min(range.high, timing.worked)}" aria-label="Elapsed time against the usual ${range.low} to ${range.high} minute estimate" aria-valuetext="${timing.elapsed} minutes elapsed. Time passed, not work completed."></progress><span class="estimate-range" data-at="${Math.round(100 * range.low / range.high)}" aria-hidden="true"></span><span class="estimate-threshold" data-at="${Math.round(100 * range.low / range.high)}" aria-hidden="true"></span></div><div class="time-scale"><span>Time passed, not work completed</span><span>${range.low}–${range.high} min estimate</span></div>` : ''}</div>`;
}
function progressView(run) {
  const { stages, current } = stageInfo(run);
  const notes = list(run.events ?? run.progress).filter(event => text(event.text)).slice(-40);
  return `<section class="run-path" aria-labelledby="path-title"><div class="path-heading"><h2 id="path-title">From file to understanding.</h2><p>Follow each step as it happens.</p></div><ol class="timeline">${stages.map(stage => `<li class="${e(stage.status)}" ${current?.id === stage.id ? 'aria-current="step"' : ''}><span class="step-number" aria-hidden="true">${stage.status === 'complete' ? check : String(stage.index + 1).padStart(2, '0')}</span><div><h3>${e(stage.label)}</h3><p>${statuses[stage.status] || 'Update received'}</p></div></li>`).join('')}</ol>${notes.length ? `<details id="technical-notes" class="technical-details"><summary>Show step details</summary><ol>${notes.map(event => `<li>${e(event.stage === 'report' && event.kind === 'stage_completed' ? run.status === 'completed' && ['score','refuse'].includes(event.outcome) ? 'Your report is ready.' : 'Report step updated.' : event.text)}</li>`).join('')}</ol></details>` : ''}</section>`;
}
const fact = (label, value) => `<div><dt>${e(label)}</dt><dd>${e(value)}</dd></div>`;
const paragraph = (label, value) => text(value) ? `<section class="report-section"><h2>${e(label)}</h2><p class="report-prose">${e(value)}</p></section>` : '';
const fullDetails = content => `<details id="full-report-details" class="full-report-details"><summary id="full-report-toggle"><span class="details-closed">Show full details</span><span class="details-open">Hide full details</span>${arrow}</summary><div class="full-report-content">${content}</div></details>`;
function summaryView(result) {
  const keys = ['trait', 'where', 'comparison', 'not_a_chance'];
  const parts = result.summaryParts;
  if (parts && keys.every(key => text(parts[key])) && keys.map(key => parts[key]).join(' ') === result.summary) {
    return `<div class="report-summary">${keys.map(key => `<p class="summary-${key}${key === 'where' ? ' result-sentence' : ''}">${e(parts[key])}</p>`).join('')}</div>`;
  }
  const [low, high] = result.report.delivered.placement.percentile_range;
  return `<p class="result-sentence">You scored higher than about <span class="result-range">${e(low)} to ${e(high)}</span> out of every 100 people in your comparison group.</p><p class="report-summary">${e(result.summary)}</p>`;
}
/** Explicit recorded labels only; never infer a trait from prose or a model ID. */
export function recordedTraitName(bundle = {}) {
  const artifacts = readArtifacts(bundle);
  const request = artifacts.reference_response?.request_echo?.trait;
  const reference = artifacts.reference_response?.reference?.trait;
  const id = value => typeof value === 'string' && value.trim() ? value
    : Array.isArray(value) && value.length === 1 && typeof value[0]?.id === 'string' && value[0].id.trim() ? value[0].id : null;
  const recordedIds = [artifacts.model_choice?.trait_id, request?.id, reference?.id].filter(value => value != null);
  const ids = recordedIds.map(id);
  if (ids.some(value => value === null) || new Set(ids).size > 1) return null;
  const label = value => typeof value === 'string' && value.trim() ? value : null;
  return label(request?.label) || label(Array.isArray(request?.id) && request.id.length === 1 ? request.id[0]?.label : null)
    || label(reference?.label) || label(Array.isArray(reference?.id) && reference.id.length === 1 ? reference.id[0]?.label : null);
}
/** Presentation of an already checked score; reportView owns the validation boundary. */
export function scoreReportView(result, bundle = {}) {
  if (result?.kind !== 'score') return '';
  const delivered = result.report.delivered;
  const trait = recordedTraitName(bundle);
  const headlineTrait = trait?.replace(/^(\s*)(\p{L})/u, (_, space, first) => space + first.toUpperCase());
  const artifacts = readArtifacts(bundle);
  const comparisonScore = artifacts.reference_response?.placement?.patient_raw_score ?? artifacts.reference_response?.request_echo?.patient?.raw_score ?? artifacts.score?.raw_score ?? result.score;
  const details = `<div class="report-reading"><section class="report-section report-limits"><h2>What this can tell you</h2><p class="report-prose">${e(delivered.personalized_limit)}</p>${said(delivered.ancestry_note) ? `<h3 class="report-subhead">Your ancestry and your DNA</h3><p class="report-prose">${e(delivered.ancestry_note)}</p>` : ''}${result.limits.length ? `<ul>${result.limits.map(limit => `<li>${e(limit)}</li>`).join('')}</ul>` : ''}<p class="report-prose report-disclaimer">${e(delivered.disclaimer)}</p></section><section class="report-section"><h2>Behind the comparison</h2>${said(delivered.model_choice) ? `<h3 class="report-subhead">Why this scoring method</h3><p class="report-prose">${e(delivered.model_choice)}</p>` : ''}<dl class="report-facts z-score-fact">${fact('z-score in this group', String(delivered.placement.z_score))}</dl><p class="z-score-help">This shows how far your score is above or below the group’s average, measured in its usual spread. It is a position, not a risk.</p><dl class="report-facts">${fact('Recorded score', String(result.score))}${fact('People in the group', String(result.reference.n_participants))}${text(delivered.comparison_group) ? fact('Where the group comes from', delivered.comparison_group) : ''}${fact('Scoring method', result.model)}${fact('Share of the model’s weight measured', String(result.coverage))}</dl></section></div>`;
  return `<article class="report report-score"><header class="answer-lead score-lead"><h1 class="result-title">${trait ? `${e(headlineTrait)}: your genetic score` : 'Your genetic score'}</h1>${trait ? '' : '<p class="trait-unavailable">A trait name is not available for this result.</p>'}${summaryView(result)}</header>${referenceChart(result, [artifacts.percentile_range, artifacts.reference_response?.placement], comparisonScore)}${said(delivered.doctor_note) ? `<section class="doctor-note" aria-labelledby="doctor-note-title"><h2 id="doctor-note-title">A note for your doctor</h2><p class="report-prose">${e(delivered.doctor_note)}</p></section>` : ''}${fullDetails(details)}</article>`;
}
export function reportView(report = {}) {
  const bundle = report.bundle || {};
  const result = getDisplayResult(bundle);
  const envelope = result.kind === 'refuse' && result.envelope ? result.envelope
    : result.kind !== 'score' && bundle.report?.outcome !== 'score' && text(report.refusal?.headline) && text(report.refusal?.detail) ? report.refusal : null;
  if (envelope) return `<article class="report report-refusal"><header class="answer-lead"><p class="section-label">Your answer</p><h1>${e(envelope.headline)}</h1><p class="answer-context">${e(envelope.detail)}</p></header>${fullDetails(`<div class="refusal-body">${paragraph('What was tried', envelope.what_was_tried)}${paragraph('Why this is an honest answer', envelope.why_this_is_honest)}${paragraph('What would change it', envelope.what_would_change_it)}${envelope.is_about_the_person === false ? '<p class="person-limit">This limitation is about the available evidence, not a finding about your body.</p>' : ''}</div>`)}</article>`;
  if (result.kind === 'score') return scoreReportView(result, bundle);
  const refusal = report.refusal || {};
  const explanation = text(refusal.explanation) || text(refusal.message) || text(refusal.text) || (!bundle.report ? text(report.result?.why) : '');
  return `<article class="report report-incomplete"><header class="answer-lead"><p class="section-label">Your report</p><h1>A complete score report is not available.</h1><p class="answer-context">${e(explanation || 'The analysis did not produce all the information needed for a complete report.')}</p></header>${result.issues?.length ? `<details id="report-issues" class="technical-details"><summary>Why the summary is incomplete</summary><ul>${[...new Set(result.issues.map(issue => issue.message))].map(message => `<li>${e(message)}</li>`).join('')}</ul></details>` : ''}</article>`;
}
function samplesView(samples, disabled = false) {
  const files = list(samples).filter(sample => /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(text(sample?.name)) && Number(sample.bytes) > 0);
  if (!files.length) return '';
  // Each sample can be run here directly — the phone case, where downloading a file and choosing
  // it back is the hard part — or downloaded to use your own way. Answer the questions first.
  const row = sample => `<li class="sample-row"><div class="sample-name">${e(sample.name)}</div>`
    + `<button type="button" class="button secondary sample-use" data-action="use-sample" data-sample="${e(sample.name)}"${disabled ? ' disabled' : ''}>Use this sample ${arrow}</button>`
    + `<a class="sample-download" href="/samples/${encodeURIComponent(sample.name)}" download>Download</a></li>`;
  return `<details class="sample-files" id="sample-files"><summary>Show sample files</summary>`
    + `<p class="field-help">Answer the questions below, then run a sample here — no download needed — or download it to choose your own way.</p>`
    + `<ul>${files.map(row).join('')}</ul></details>`;
}
function uploadLabel(state) {
  if (state.pending !== 'upload') return 'Start analysis';
  if (!Number.isInteger(state.uploaded)) return 'Uploading and starting…';
  return state.uploaded < 100 ? `Uploading… ${state.uploaded}%` : 'Checking the file and starting…';
}
// While no analysis can start (the worker's sandbox_problem): said before a file is chosen, in the page's own words.
export const PAUSED_TEXT = 'The demo can’t start a new analysis right now because of a problem on its side. Please try again later.';
const pausedView = () => `<div class="paused-notice" role="status"><strong>New analyses are paused</strong><p>${PAUSED_TEXT} This page checks again by itself.</p></div>`;
function uploadView(state) {
  // Each kind of file its own measured estimate: a chip's genotype file, and a whole genome once one is measured.
  const chip = estimateOf({ estimate: state.run?.estimates?.chip }), genome = estimateOf({ estimate: state.run?.estimates?.whole_genome });
  const range = chip || genome;
  const limit = Number(state.session?.limits?.upload_bytes), expanded = Number(state.session?.limits?.expanded_bytes);
  const paused = !!state.run?.paused;
  return `<section class="intake"><div class="intake-intro"><p class="section-label">A conversation with your DNA</p><h1>A little more<br>understanding.</h1><p class="intro-deck">Answer a few questions and choose a supplied DNA file. We’ll only ask you something if your answers and your file can’t settle it.</p>${range ? `<div class="intake-time"><span>${range.low}–${range.high}</span><p>${chip ? 'minutes for a genotype file from a chip.' : 'minutes for a whole genome.'}${chip && genome ? `<br>A whole genome: about ${genome.low}–${genome.high} minutes.` : ''}</p></div>` : ''}</div><section class="upload-area" aria-labelledby="upload-title"><h2 id="upload-title">Your file. Your questions.</h2><p>Use a supplied sample or development file. It goes to the lab’s analysis server.</p>${paused ? pausedView() : ''}${samplesView(state.session?.samples, !!state.pending || paused)}<form id="upload-form">${intakeView(!!state.pending)}<label class="file-drop" for="genome-file"><svg viewBox="0 0 48 48" width="40" height="40" aria-hidden="true"><path d="M24 31V9m-8 8 8-8 8 8M10 29v10h28V29" fill="none" stroke="currentColor" stroke-width="1.5"/></svg><span>Choose your DNA file</span><input id="genome-file" name="genome" type="file" aria-describedby="file-formats file-limits selected-file" ${state.pending ? 'disabled' : ''}></label><p id="selected-file" class="field-help" role="status"></p><p id="file-formats" class="field-help">A genotype table (23andMe, AncestryDNA and the like), a VCF from a whole genome, or a Complete Genomics var file; plain, or in ZIP, gzip or bzip2. A whole genome takes longer to check and to analyse. Exomes can’t be analysed yet.</p><p id="file-limits" class="field-help">${Number.isFinite(limit) ? `Upload limit: ${bytes(limit)}.` : ''}${Number.isFinite(expanded) ? ` Size limit after unpacking: ${bytes(expanded)}.` : ''}</p>${consentView(!!state.pending)}<p id="intake-problem" class="field-help intake-problem" role="alert"></p><button class="button wide" id="upload-submit" type="submit" ${state.pending || paused ? 'disabled' : ''}>${uploadLabel(state)} ${arrow}</button>${state.pending === 'upload' && Number.isInteger(state.uploaded) ? `<progress class="upload-progress" max="100" value="${state.uploaded}" aria-label="Upload progress">${state.uploaded}%</progress>` : ''}</form><p class="retention-note">Your file is deleted when the analysis ends. Your result is kept for 24 hours, then deleted. Stopping an analysis deletes it at once.</p></section></section>`;
}
function busyView(run, now) {
  const at = Number(run.retry_at) * 1000;
  const minutes = Math.max(1, Math.round((at - now) / 60000));
  const wait = minutes < 60 ? `${minutes} minute${minutes === 1 ? '' : 's'}` : `${Math.floor(minutes / 60)} h${minutes % 60 ? ` ${minutes % 60} min` : ''}`;
  const retry = Number.isFinite(at) && at > 0 ? `Please try again around ${new Date(at).toLocaleTimeString([], {hour:'numeric',minute:'2-digit'})} (in about ${wait}).` : 'Please try again later.';
  const slots = Number(run.slots);
  return `<section class="empty-state"><div class="empty-mark" aria-hidden="true">${helixView({waiting:true})}</div><p class="section-label">${Number.isInteger(slots) && slots > 1 ? `All ${slots} places are taken` : 'One analysis at a time'}</p><h1>Good things take<br>a little time.</h1><p class="intro-deck">${e(retry)}</p><p class="empty-help">This page checks again by itself and shows the upload form when there’s room for another analysis.</p></section>`;
}
function alertsView(state) {
  const enabled = !!state.alertsEnabled;
  return `<div class="attention-bar"><div>${state.run.question ? '<strong>We’re ready when you are.</strong><p>The analysis is waiting for your answer.</p>' : '<strong>Nothing needed from you right now.</strong><p>Keep this page open. We’ll tell you when we need you.</p>'}</div><div class="alert-settings"><button class="button secondary" type="button" data-action="alerts" aria-pressed="${enabled}"><svg viewBox="0 0 24 24" width="17" height="17" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>${enabled ? 'Turn off alerts' : 'Turn on alerts'}</button><p class="field-help">${e(state.alertMessage || 'A soft sound and notifications, if you allow them.')}</p></div></div>`;
}
function runningView(state, now) {
  const run = state.run, { current } = stageInfo(run), waiting = !!run.question;
  const settled = current && ['complete', 'refuse', 'stop', 'unknown'].includes(current.status);
  const heading = settled && !waiting ? 'Waiting for the next step' : current?.label || 'Your analysis is underway';
  const description = settled && !waiting ? `Latest recorded step: ${current.label}. ${statuses[current.status]}.` : current?.description || 'The next recorded step will appear here.';
  return `${waiting ? questionView(run.question, !!state.pending) : ''}<section class="analysis-hero ${waiting ? 'is-waiting' : ''}" aria-labelledby="analysis-step"><div class="hero-copy"><p class="section-label">${current ? `Step ${current.index + 1} of 6` : 'Your analysis'}<span class="live-indicator"></span>${state.error ? 'Last confirmed update' : waiting ? 'Waiting for you' : 'In progress'}</p><${waiting ? 'h2' : 'h1'} id="analysis-step">${e(heading)}</${waiting ? 'h2' : 'h1'}><p class="hero-description">${e(description)}</p></div><div class="hero-art" data-helix>${helixView({waiting:waiting || !!state.error})}</div>${timeView(run,now)}</section>${alertsView(state)}${linkView(state)}${progressView(run)}`;
}
// A run's private link: the way back to its progress and result from any browser, until it is deleted.
function linkView(state) {
  const run = state.run;
  if (!text(run?.link)) return '';
  return `<section class="private-link" aria-labelledby="private-link-title"><h2 id="private-link-title">Your private link</h2><p>Save this link to get back to your result, from any browser, until it is deleted. Anyone who has it can see your result, so keep it to yourself.</p><div class="private-link-row"><input id="private-link" readonly value="${e(`${state.origin || ''}${run.link}`)}" aria-labelledby="private-link-title"><button type="button" class="button secondary" data-action="copy-link">Copy link</button></div></section>`;
}
// When an ended run's result is deleted, said plainly.
function keptUntil(run) {
  const at = Number(run?.purge_at) * 1000;
  if (!Number.isFinite(at) || at <= 0) return '';
  const when = new Date(at).toLocaleString([], { weekday: 'long', month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  return `<p class="kept-until">Your result is kept until ${e(when)}, then deleted. Deleting the files deletes it now.</p>`;
}
const linkGoneView = () => '<section class="empty-state"><p class="section-label">Private link</p><h1>This result is<br>no longer here.</h1><p class="intro-deck">A result is kept for 24 hours after its run ends, then deleted. To run another analysis, sign in with the password.</p></section>';
export function renderPage(state, now = Date.now()) {
  const run = state.run, disabled = !!state.pending;
  let content = '<section class="connecting-state"><p role="status">Opening your analysis…</p></section>';
  if (state.ready && run) {
    if (state.session?.via_link && !run.owned) content = linkGoneView();
    else if (run.status === 'busy' || run.busy && !run.owned) content = busyView(run, now);
    else if (run.status === 'idle') content = uploadView(state);
    else if (run.status === 'failed') content = `<section class="failure-state"><p class="section-label">Analysis stopped</p><h1>We couldn’t finish<br>this analysis.</h1><p class="intro-deck">${e(run.error || 'The analysis stopped before it could finish. Delete its files to try again.')}</p><div class="failure-action"><button type="button" class="button" data-action="end" ${disabled ? 'disabled' : ''}>${state.pending === 'end' ? 'Deleting files…' : 'Delete files and try again'} ${arrow}</button><p>Deletes this analysis’s files so you can start again.</p></div></section>${keptUntil(run)}${linkView(state)}${run.report ? reportView(run.report) : ''}${progressView(run)}`;
    else if (run.status === 'completed') content = `<div class="completion-line">${check}<span>Your analysis is complete</span></div>${reportView(run.report)}${keptUntil(run)}${linkView(state)}<details class="completed-path" id="completed-path"><summary>Show the steps taken</summary>${progressView(run)}</details>`;
    else content = runningView(state, now);
  }
  const past = Number(state.session?.expires_at) * 1000 <= now;
  const expires = state.session?.expires_at && !past ? new Date(Number(state.session.expires_at) * 1000).toLocaleString([], { weekday: 'long', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }) : '';
  const kept = state.session && (past || state.session.kept_for_run) ? 'Your sign-in has ended: you stay signed in for this analysis until it ends and its result is gone. ' : '';
  const owned = run?.owned && !['idle','busy','failed'].includes(run.status);
  return `<header class="site-header"><div class="header-content"><div class="brand"><img src="/mark.svg" alt=""><span>prs-agent</span></div><span class="brand-line">Genetics, with context.</span><button class="sign-out" data-action="logout" ${!state.ready || disabled ? 'disabled' : ''}>${state.pending === 'logout' ? 'Signing out…' : 'Sign out'} ${arrow}</button></div></header><main class="workspace-main" id="main" tabindex="-1">${state.error ? `<div class="connection-banner" role="alert"><strong>We can’t connect right now</strong><p>${e(state.error)}</p><button class="text-button" data-action="retry">Try again ${arrow}</button></div>` : ''}${state.notice ? `<div class="notice-banner" role="status">${e(state.notice)}</div>` : ''}${content}${owned ? `<div class="end-run"><button class="text-button" type="button" data-action="end" ${disabled ? 'disabled' : ''}>${state.pending === 'end' ? 'Deleting files…' : run.status === 'completed' ? 'Delete these files' : 'Stop and delete files'} ${arrow}</button><p>${run.status === 'completed' ? 'Deletes this analysis and its files.' : 'Stops this analysis and deletes its files.'}</p></div>` : ''}<footer class="site-footer"><p class="footer-signature">Genetics, with context.</p><p>Research tool. Not medical advice.</p><p>${state.session?.via_link ? 'Opened with your private link. ' : kept || (expires ? `Signed in until ${e(expires)}. ` : '')}Your analysis data is not saved in this browser.</p></footer></main>`;
}
