import { readArtifacts, getDisplayResult, validateArtifactBundle, normalizeOutcome } from './contracts.js';
import { createSessionState, applySessionEvent } from './session.js';
import { exampleRefusal, exampleQuestion, SCREEN_OPTIONS } from './examples.js';
import { buildReportTopics } from './exploration.js';
import { readNotebook, updateQuestion } from './notebook.js';

// Intentionally no fetch, sockets, persistence, telemetry, credential input, or agent import.
// A future transport supplies artifact bundles and ordered session events at this boundary.
const state = {
  screen: 'home', toolsOpen: false, file: null, trait: '', selfReport: '', bundle: null,
  session: createSessionState(), previewEvents: false, ancestrySeen: false,
  acknowledged: false, gateReply: '', followupDraft: '', savedQuestions: [],
  explanation: null, importError: '', notice: '', exampleMismatch: false,
  reportOrigin: 'result', explorerView: 'explore', activeTopic: null,
  openedTopics: [], editingQuestion: null, notebookFeedback: '', notebookExample: false, importVersion: 0,
};
const app = document.querySelector('#app');
const esc = value => (value !== null && typeof value === 'object' ? JSON.stringify(value) : String(value ?? '')).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const list = value => Array.isArray(value) ? value : [];
const asText = value => typeof value === 'string' ? value : value == null ? '' : JSON.stringify(value);
const prose = value => esc(asText(value));
const artifacts = () => readArtifacts(state.bundle || {});
const display = () => getDisplayResult(state.bundle || {});
const finite = value => typeof value === 'number' && Number.isFinite(value);
const percent = value => finite(value) ? `${(value * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%` : 'Not available';
const stageLabels = { 'read-and-reason': 'Understand your file', ancestry: 'Find genetic similarity', select: 'Choose a model', score: 'Calculate the score', interpret: 'Build a comparison', report: 'Explain the answer' };
const stageDescriptions = { 'read-and-reason': 'Check where your file came from and how to read its DNA positions.', ancestry: 'Find comparison participants with similar DNA patterns, and explain the uncertainty.', select: 'Choose a research formula that fits your question and DNA, and explain its limits.', score: 'Match your DNA to the formula, check how much can be used, and calculate the score.', interpret: 'Compare your score with a suitable group, and explain differences in how the scores were calculated.', report: 'Explain the score and its limits, or why a meaningful score cannot be given.' };
const icons = {
  arrow: '<path d="M4 12h15m-6-6 6 6-6 6"/>', upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  file: '<path d="M6 3h8l4 4v14H6V3Zm8 0v5h4M9 12h6m-6 4h6"/>',
  check: '<path d="m5 12 4 4 10-10"/>', close: '<path d="m6 6 12 12M6 18 18 6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.1"/>',
  chat: '<path d="M21 11a8 8 0 0 1-8 8H7l-5 3 2-6a8 8 0 1 1 17-5Z"/><path d="M8 11h8m-8 4h4"/>',
  leaf: '<path d="M20 3c0 13-4 16-10 16C3 19 1 9 8 6c4-2 7-1 12-3ZM4 21 16 9"/>',
  branch: '<path d="M6 3v11a4 4 0 0 0 4 4h8M6 8h8a4 4 0 0 0 4-4m-3 11 3 3-3 3"/><circle cx="6" cy="3" r="1"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.info}</svg>`;
const button = (label, action, extra = '', cls = '') => `<button type="button" class="button ${cls}" data-action="${action}" ${extra}>${label}</button>`;
const badge = (label, cls = '') => `<span class="badge ${cls}">${label}</span>`;
const exampleBanner = (text = 'Example screen · No genome has been analyzed. This is illustrative copy, not your result.') => `<div class="banner example">${icon('info')}<span>${text}</span></div>`;
const intro = (_eyebrow, title, subtitle = '') => `<header class="intro"><h1>${title}</h1>${subtitle ? `<p>${subtitle}</p>` : ''}</header>`;
const dl = entries => `<dl class="definition-list">${entries.map(([key, value]) => `<div><dt>${esc(key)}</dt><dd${value == null || value === '' ? ' class="empty-label"' : ''}>${value == null || value === '' ? 'Not established' : prose(value)}</dd></div>`).join('')}</dl>`;
const evidence = source => list(source?.evidence).length ? `<details class="report-details"><summary>Evidence behind this finding</summary>${list(source.evidence).filter(item => item && typeof item === 'object').map(item => `<p class="wrap">${prose(item.source)}${item.field ? ` · ${prose(item.field)}` : ''}</p>`).join('')}</details>` : '';

function navigationIndex() {
  return ({ upload: 0, file: 0, question: 1, ancestry: 1, model: 2, progress: 2, result: 3, refusal: 3, stop: 3, followup: 3 })[state.screen];
}
function shell(content) {
  const home = state.screen === 'home';
  const names = ['Your file', 'Your question', 'Review', 'Your report'];
  const destinations = ['upload', 'question', 'model', state.bundle ? display().kind === 'refuse' ? 'refusal' : 'result' : state.reportOrigin];
  const active = navigationIndex();
  return `<header class="topbar ${home ? 'site-topbar' : ''}"><div class="header-inner">
    <button class="brand" data-action="nav" data-screen="home" aria-label="prs-agent home"><img src="./mark.svg" alt=""><span>prs-agent</span></button>${home ? '<nav class="site-nav" aria-label="About prs-agent"><a href="#how-it-works">How it works</a><a href="#what-you-need">What you need</a><a href="#common-questions">FAQs</a></nav>' : '<span class="brand-line">Genetics, with context.</span>'}
    <div class="header-actions"><span class="preview-label">Preview ${home ? '' : '<span>· No live analysis</span>'}</span>${home ? button('Try the preview', 'nav', 'data-screen="upload"', 'header-start') : `<details class="help-menu"><summary>Help ${icon('info')}</summary><div class="help-options"><button data-action="nav" data-screen="home">About prs-agent</button><button data-action="nav" data-screen="how">How it works</button><button data-action="nav" data-screen="privacy">Your privacy</button><button data-action="nav" data-screen="connection">Connection options</button></div></details>`}</div>
  </div></header><div class="layout"><main class="main screen-${state.screen}" id="main" tabindex="-1">
    ${home ? '' : active === undefined ? '<button class="back-link" data-action="nav" data-screen="upload">← Back to your file</button>' : `<nav class="journey" aria-label="Analysis screens">${names.map((name, i) => `<button class="nav-step ${active === i ? 'active' : ''}" data-action="nav" data-screen="${destinations[i]}" ${active === i ? 'aria-current="page"' : ''}><span class="step-num">${i + 1}</span>${name}</button>`).join('')}</nav>`}
    ${state.bundle && !home ? `<div class="source-label">${icon('file')}${state.bundle.example === true ? 'Example artifacts · Not a personal result' : 'Viewing a locally imported report'}</div>` : ''}
    ${state.notice ? `<div class="banner" role="status">${icon('info')}<span>${esc(state.notice)}</span></div>` : ''}${content}
  </main><footer class="page-footer"><div class="footer-row"><span>${icon('lock')}Your data stays in this tab.</span><button class="text-button" data-action="nav" data-screen="home">About</button><button class="text-button" data-action="nav" data-screen="privacy">Privacy</button><button class="text-button" data-action="nav" data-screen="connection">Connection</button></div>
    <details class="preview-tools" id="preview-tools" ${state.toolsOpen ? 'open' : ''}><summary>Preview tools</summary><div class="preview-controls"><label for="screen-preview">Explore a screen</label><select id="screen-preview" aria-label="Preview a screen">${SCREEN_OPTIONS.map(([id, name]) => `<option value="${id}" ${state.screen === id ? 'selected' : ''}>${name}</option>`).join('')}</select><button class="text-button" data-action="nav" data-screen="artifacts">Open a saved report ${icon('arrow')}</button></div><p>Examples are labeled. No agent is connected and no data is sent.</p></details>
  </footer></div>`;
}

function homeScreen() {
  return `<section class="site-hero" aria-labelledby="home-title"><div class="site-hero-copy"><p class="brand-kicker"><span aria-hidden="true"></span>DNA insights, explained plainly</p><h1 id="home-title">Make sense of<br><span>your DNA.</span></h1><p class="hero-description">prs-agent helps you explore a health question using a DNA file you already have. It checks whether the research can support a useful genetic score, then explains the answer and its limits.</p><div class="button-row">${button(`Try the preview ${icon('arrow')}`, 'nav', 'data-screen="upload"')}${button('See an example', 'nav', 'data-screen="refusal"', 'secondary')}</div><p class="preview-disclosure">This is an interactive preview. You can explore the screens, but DNA analysis is not connected yet.</p><button class="text-button" data-action="nav" data-screen="artifacts">Already have a saved report? Open it ${icon('arrow')}</button></div>
  <aside class="answer-overview" aria-labelledby="answer-overview-title"><p class="small-label">What the report is designed to give you</p><h2 id="answer-overview-title">An answer with context.</h2><div class="outcome-intro"><span class="outcome-symbol">${icon('branch')}</span><div><h3>A genetic score, explained</h3><p>Where your score stands in a suitable comparison group, alongside the limits that matter for you.</p></div></div><div class="outcome-divider"><span>or</span></div><div class="outcome-intro"><span class="outcome-symbol">${icon('chat')}</span><div><h3>A clear reason no score can be given</h3><p>What was tried, why a number would be misleading, and what would need to change.</p></div></div><p class="outcome-note">Both are valid results. A score is not a diagnosis or your chance of developing a condition.</p></aside></section>
  <section class="site-section" id="how-it-works" aria-labelledby="how-title"><div class="section-intro"><p class="small-label">How it works</p><h2 id="how-title">Your question. A careful check.<br>A clearer explanation.</h2><p>You don’t need to know genetics or choose a research method. The assistant works through the checks and explains what matters.</p></div><ol class="simple-steps"><li><span class="step-number">1</span><h3>Bring your DNA file</h3><p>A file you already downloaded from a DNA testing company or sequencing service. No new sample is collected here.</p></li><li><span class="step-number">2</span><h3>Ask in your own words</h3><p>Say what you want to understand. The assistant may need to ask a follow-up before it can continue.</p><p class="question-example">Example question: “What can my DNA tell me about type 2 diabetes?”</p></li><li><span class="step-number">3</span><h3>Understand the answer</h3><p>The analysis checks whether your data and the research fit the question. Your report explains a score with its limits, or why one cannot be given.</p></li></ol><div class="timing-note">${icon('clock')}<p>A connected analysis can take hours. You would see which step is running and whether it needs an answer from you. This preview runs no analysis.</p></div></section>
  <section class="site-section score-explainer" aria-labelledby="score-explainer-title"><div><p class="small-label">The name, explained</p><h2 id="score-explainer-title">What is a genetic score?</h2></div><div><p><strong>PRS means polygenic risk score.</strong> It combines many small differences in DNA into one score using a formula from research. prs-agent is the assistant designed to guide that process and explain the result.</p><p>The score can be compared with a suitable group of people. The report calls that position a <strong>percentile</strong>. It describes where a score sits in that group; it does not tell you the probability of developing a condition.</p><p>It does not diagnose a condition or tell you which medication or screening to choose. Those are questions to discuss with a clinician or genetic counselor.</p></div></section>
  <section class="site-section needs-section" id="what-you-need" aria-labelledby="needs-title"><div class="section-intro"><p class="small-label">Before you begin</p><h2 id="needs-title">A file you already have.<br>A question you care about.</h2><p>This is for people who already have their DNA data and want to understand what research can say about a condition or trait.</p></div><div class="needs-list"><div>${icon('file')}<div><h3>Your original DNA data</h3><p>A raw data download from 23andMe or AncestryDNA, or a VCF/gVCF file from whole-genome sequencing. “Raw data” means the DNA entries themselves, rather than a PDF report or a screenshot.</p></div></div><div>${icon('chat')}<div><h3>A question, in your words</h3><p>You don’t need a technical term. The assistant would first check what you mean and whether there is suitable research for it.</p></div></div><div>${icon('leaf')}<div><h3>No DNA file yet?</h3><p>You can explore the example without one. This site does not sell or carry out a DNA test.</p><button class="text-button" data-action="nav" data-screen="refusal">Read the example explanation ${icon('arrow')}</button></div></div></div></section>
  <section class="privacy-feature" aria-labelledby="home-privacy-title"><span class="privacy-feature-icon">${icon('lock')}</span><div><p class="small-label">Your data, your choice</p><h2 id="home-privacy-title">Know where your DNA goes.</h2><p>In this preview, selecting a DNA file only shows its name and size. Its contents are not read or uploaded. A saved report you choose to open is read locally in this tab.</p><p>Before a future connected version sends anything, it must explain what leaves your device, where it goes, and how it is kept or deleted.</p><button class="text-button" data-action="nav" data-screen="privacy">Read how your data is handled ${icon('arrow')}</button></div></section>
  <section class="site-section faq-section" id="common-questions" aria-labelledby="faq-title"><div class="section-intro"><p class="small-label">Common questions</p><h2 id="faq-title">A few things you might<br>be wondering.</h2></div><div class="faq-list"><details><summary>Can this tell me whether I will get a condition?</summary><p>No. A genetic score is one kind of comparison. It is not a diagnosis or your personal probability of developing a condition. A clinician or genetic counselor can help put a report alongside your health and family history.</p></details><details><summary>Why might I not get a score?</summary><p>Your file may not contain enough usable information, or the research and comparison data may not fit your situation. A refusal explains why the evidence cannot support a meaningful score. A data problem can also stop the analysis; that should come with an explanation of what needs resolving.</p></details><details><summary>Do I need to understand genetics?</summary><p>No. You can ask in everyday language. The interface explains the checks, the recommendation, and the result. You can keep your questions and download them with the report for a later conversation.</p></details><details><summary>Can I use it with any health question?</summary><p>You can describe what you want to know, but a useful score depends on whether suitable research and comparison data exist for your question and your DNA. Some questions cannot be answered with a polygenic score.</p></details><details><summary>Does it analyze DNA right now?</summary><p>No. This version is an interface preview. You can select a file locally, explore clearly labeled examples, or open a saved report. It does not calculate a score, send your DNA anywhere, or run an AI assistant.</p></details><details><summary>Will I need a Claude or OpenAI account?</summary><p>The intended version uses your own provider access. How that connection will work, and whether a subscription or separate API billing would cover it, are still being worked out. This preview does not ask for a password or API key.</p><button class="text-button" data-action="nav" data-screen="connection">See the connection options ${icon('arrow')}</button></details></div></section>
  <section class="site-closing" aria-labelledby="closing-title"><div><h2 id="closing-title">Get a feel for it.</h2><p>Explore the steps or read an example. No account or DNA file needed to look around.</p></div><div class="button-row">${button('Try the preview', 'nav', 'data-screen="upload"')}${button('See an example', 'nav', 'data-screen="refusal"', 'secondary')}</div></section>`;
}

function uploadScreen() {
  const file = state.file;
  return `<div class="welcome-layout"><div class="welcome-message"><p class="brand-kicker"><span aria-hidden="true"></span>First, your file</p>${intro('', 'Start with the DNA<br><span>data you have.</span>', 'Use the original data file from a DNA testing company or sequencing service. You can explore this preview without analyzing it.')}<details class="file-help"><summary>What is a DNA data file?</summary><p>It contains the DNA entries recorded by your test. It is often called “raw data.” Use the downloaded file, rather than a screenshot or a PDF summary of your results.</p><p>A VCF or gVCF is a type of file that records DNA differences, often supplied by a sequencing service.</p></details><button class="text-button" data-action="nav" data-screen="home">Learn what prs-agent does ${icon('arrow')}</button></div>
  <div class="welcome-action"><section class="card upload-card"><h2>Start with your DNA file</h2>
    <div class="privacy-note">${icon('lock')}<p>This preview keeps your file in this tab. Its contents are not read or uploaded. Remove it or close the tab to release it.</p></div>
    ${file ? `<div class="selection">${icon('file')}<div><strong>${esc(file.name)}</strong><small>${formatBytes(file.size)} · Selected locally · Not analyzed</small></div><button data-action="remove-file" aria-label="Remove selected file">${icon('close')}</button></div><div class="button-row">${button(`Continue to file review ${icon('arrow')}`, 'nav', 'data-screen="file"', 'wide')}</div>` : `<div class="dropzone" id="dropzone"><div class="upload-symbol">${icon('upload')}</div><h3>Drop your file here</h3>${button('Choose a file', 'choose-file')}<p>23andMe, AncestryDNA, VCF or gVCF<br>Text, ZIP and GZ files</p></div>`}
    <input type="file" id="genome-file" class="sr-only" tabindex="-1" aria-label="Select a local DNA file">
  </section><div class="example-link"><span>Just looking around?</span><button class="text-button" data-action="nav" data-screen="refusal">See an example explanation ${icon('arrow')}</button></div><div class="return-link"><button class="text-button" data-action="nav" data-screen="artifacts">Open a saved report ${icon('file')}</button></div></div></div>`;
}

function fileScreen() {
  const a = artifacts().classification;
  return `${intro('', 'About your file.', 'Before scoring, the agent needs to understand what your file contains.')}
  ${!a ? `<div class="banner">${icon('clock')}<span>Awaiting review. This preview does not analyze your file.</span></div>` : `<div class="banner">${icon('file')}<span>${prose(a.decision)}</span></div>`}
  <section class="card"><div class="notice-heading"><h2>File details</h2>${badge(a ? `Recorded: ${esc(a.outcome)}` : 'Not analyzed')}</div>${state.file ? `<p class="subtle file-name">${esc(state.file.name)}</p>` : ''}
  ${dl([['Type of DNA test', a?.assay_class], ['Testing company', a?.vendor], ['DNA map version (genome build)', a?.genome_build?.value]])}<p class="field-help">A genome build is the version of the reference map used to locate positions in DNA. The analysis needs the right version to match your data correctly.</p>
  <div class="section-gap"><h3>What gaps in your file mean</h3><p class="subtle">${prose(a?.absence_means) || 'Not established yet. An absent DNA entry cannot automatically be treated as a typical result. The analysis must first check what missing entries mean for this kind of file.'}</p></div>
  <details class="report-details"><summary>How this is determined</summary><p>The file’s contents and coordinates establish its origin and genome build. If these cannot be established, the analysis stops and explains why.</p>${dl([['Chip generation', a?.chip_generation], ['Header declaration', a?.genome_build?.declared_in_header]])}<p>${prose(a?.confidence) || 'The agent must explain its confidence and the evidence behind it.'}</p>${list(a?.genome_build?.verified_by).map(v => `<p>${prose(v)}</p>`).join('')}${evidence(a)}</details>
  <div class="button-row">${button(`Continue to your question ${icon('arrow')}`, 'nav', 'data-screen="question"')}${button('Back', 'nav', 'data-screen="upload"', 'ghost')}</div></section>`;
}

function questionScreen() {
  return `${intro('', 'What would you like to know?', 'Tell us about the condition or trait you’re curious about. In a connected analysis, the AI assistant may ask a follow-up to understand what you mean.')}
  <form class="card" id="trait-form"><div class="form-group"><label for="trait">Your question</label><textarea id="trait" name="trait" required maxlength="2000" placeholder="Example: What can my DNA tell me about type 2 diabetes?">${esc(state.trait)}</textarea></div>
  <div class="form-group"><label for="self-report">How would you describe your ancestry? <span>Optional</span></label><input id="self-report" name="selfReport" type="text" maxlength="1000" value="${esc(state.selfReport)}" placeholder="Your family background or heritage, in your words"><p class="field-help">You can describe your family background or heritage, or say “I don’t know.” This won’t change the calculation or its confidence. It helps explain any difference between your self-description and the DNA comparison.</p></div>
  <div class="form-footer"><p class="field-help">Your answers stay in this tab.</p><button class="button" type="submit">Continue ${icon('arrow')}</button></div></form>`;
}

function ancestryScreen() {
  const a = artifacts().ancestry;
  const mismatch = a?.agreement === 'disagree' || (!a && state.exampleMismatch);
  return `${intro('', 'Your ancestry, in context.', mismatch ? 'Your identity and genetic similarity describe different things. A difference does not correct who you are.' : 'Genetic similarity helps the agent find a model that applies to you.')}
  ${!a && mismatch ? exampleBanner('Example disagreement · No ancestry has been inferred.') : !a ? `<div class="banner">${icon('clock')}<span>No genetic comparison is available in this preview.</span></div>` : ''}
  <section class="card"><div class="two-facts"><div><p class="small-label">Your self-description</p><h3>${prose(a?.self_reported || state.selfReport) || 'Not provided'}</h3><p>Your social, cultural, and family context.</p></div><div><p class="small-label">Genetic similarity</p><h3>${prose(a?.inferred_group) || 'Not established'}</h3><p>Similarity to the DNA of people in a research comparison group.</p></div></div>
  <div class="reading"><h3>What the calculation uses</h3><p>The analysis looks for research and comparison participants with similar DNA patterns. Your self-description does not change that calculation or its confidence.</p><p>${prose(a?.decision) || 'The agent must explain its uncertainty. This is not an ethnicity percentage or a fine-grained ancestry test.'}</p>${mismatch ? '<p>You can continue with this difference made visible, or end the session.</p>' : ''}</div>${evidence(a)}
  <div class="button-row">${button(`Continue to model review ${icon('arrow')}`, 'ancestry-continue')}${button('End session', 'end-session', '', 'ghost')}</div></section>`;
}

function modelScreen() {
  const a = artifacts().model_choice;
  const choiceIssues = state.bundle ? validateArtifactBundle(state.bundle).filter(i => i.path.startsWith('model_choice')) : [];
  const ready = a && normalizeOutcome('select', a.outcome) === 'continue' && choiceIssues.length === 0;
  const chosen = list(a?.candidates_considered).find(c => c?.pgs_id === a.chosen_model);
  const decision = a?.gate_type === 'decision';
  return `${intro('05 / THE MODEL & ITS TRADEOFFS', 'The research behind your score.', 'A scoring model is a research formula that combines DNA differences into a score. It is separate from the AI assistant that explains the analysis. The recommendation should explain why this formula fits you, and its limits.')}
  ${!a ? `<div class="banner">${icon('clock')}<span>No model has been recommended yet. This preview cannot choose one.</span></div>` : !ready ? `<div class="banner example">${icon('info')}<span>This saved information does not establish a model that can be used. ${prose(a.decision)}</span></div>` : ''}<div class="grid"><section class="card"><div class="notice-heading"><h2>${ready ? prose(a.chosen_model) : 'Recommended model'}</h2>${badge(ready ? 'From your saved report' : 'Awaiting review')}</div>${dl([['Your question', state.trait || null], ['Estimated share of the model’s weight we can use', ready && finite(a.coverage_estimate?.resolved_weight_fraction) ? `${percent(a.coverage_estimate.resolved_weight_fraction)} · estimate only` : null]])}<div class="section-gap"><h3>Why this model</h3><p class="subtle">${prose(chosen?.why_ranked_here || a?.decision) || 'The agent’s reasoning and evidence in your genetic context will appear here.'}</p></div><div class="section-gap"><h3>Its limits</h3><p class="subtle">${prose(chosen?.cost_of_choosing_it) || 'The agent must explain the limitations before you can continue.'}</p></div><p class="field-help section-gap">Some DNA differences contribute more to the formula than others. Coverage describes how much of that total contribution your data can support. This is only an estimate until the data has been processed. If less can be used than expected, the assistant may need to recommend a different model.</p>${evidence(a)}${list(a?.candidates_considered).length ? `<details class="report-details"><summary>Models considered, in the agent’s order</summary>${list(a.candidates_considered).filter(c => c && typeof c === 'object').map(c => `<div class="section-gap"><h3>${prose(c.pgs_id)}</h3><p>${prose(c.why_ranked_here)}</p><p><strong>Tradeoff:</strong> ${prose(c.cost_of_choosing_it)}</p></div>`).join('')}</details>` : ''}</section><aside><section class="support-card"><p class="eyebrow">${decision ? 'A value decision' : 'Your acknowledgment'}</p><h2 class="section-gap">${decision ? 'Your decision' : 'Review the recommendation'}</h2><p class="subtle">${decision ? 'Tell the agent what matters to you in this tradeoff. It must consider your reply before continuing.' : 'Confirm that you’ve read why this formula was chosen and its limits. The assistant remains responsible for whether it is scientifically suitable.'}</p>${decision ? `<form id="gate-form" class="section-gap"><label class="field-label" for="gate-answer">Your response to the tradeoff</label><textarea id="gate-answer" required maxlength="2000" ${!ready ? 'disabled' : ''}>${esc(state.gateReply)}</textarea><button class="button wide section-gap" ${!ready ? 'disabled' : ''}>Save response locally</button></form>` : `<label class="check-label section-gap"><input id="model-ack" type="checkbox" ${state.acknowledged ? 'checked' : ''} ${!ready ? 'disabled' : ''}>I have read why this model was chosen and its limits.</label>${button('Acknowledge', 'acknowledge', !ready || !state.acknowledged ? 'disabled' : '', 'wide')}`}<p class="field-help">Saved in this tab. No model will run.</p></section></aside></div><div class="button-row">${button('Explore progress', 'nav', 'data-screen="progress"', 'secondary')}${button('End this session', 'end-session', '', 'ghost')}</div>${choiceIssues.length ? issuesPanel(choiceIssues) : ''}`;
}

function issuesPanel(issues) {
  return `<details class="report-details" open><summary>Missing or inconsistent artifacts</summary><ul class="issues">${issues.map(i => `<li><strong>${esc(i.path)}</strong>: ${esc(i.message)}</li>`).join('')}</ul></details>`;
}

function progressScreen() {
  const s = state.session;
  const states = { idle: ['Waiting to begin.', 'No analysis is running. These steps update when the agent reports progress.'], running: ['The analysis is in progress.', 'Completion is reported by the job. A quiet period does not mean it has failed.'], ask: ['A question before continuing.', 'The agent needs your perspective. Take your time; the analysis is waiting for your reply.'], 'waiting-agent': ['Your reply is recorded.', 'Waiting for the agent to consider your answer. No stage is marked complete just because you replied.'], blocked: ['Waiting for a reference provider.', 'The provider has not supplied a usable response. No scientific judgment has been made; this is not a refusal or a data stop.'], refuse: ['The analysis has an honest limit.', 'The agent has recorded a refusal. The report must deliver its person-facing explanation.'], stop: ['The analysis has stopped.', 'A recorded data problem prevents the next step. The reason should identify what needs resolving.'], error: ['The connection needs attention.', 'An execution or transport issue is not a scientific refusal. A backend would need to restore the job status.'] };
  const [heading, detail] = states[s.status] || ['Waiting for the next event.', 'A completed run still needs its report artifacts before a score can be displayed.'];
  const q = s.pendingQuestion;
  return `${intro('06 / YOUR ANALYSIS', 'Your analysis, step by step.', 'Some stages take several hours. You’ll see updates here, and any questions the agent needs to ask.')}
  ${state.previewEvents ? exampleBanner('Example progress · No computation is taking place.') : ''}<div class="grid"><section class="card"><div class="notice-heading"><h2>Analysis steps</h2>${badge(s.status === 'idle' ? 'Not started' : esc(s.status), s.status === 'ask' ? 'blue' : '')}</div><ol class="timeline">${s.stages.map((stage, i) => `<li class="${esc(stage.status)}"><span class="timeline-mark">${stage.status === 'complete' ? icon('check') : i + 1}</span><div class="timeline-text"><h3>${stageLabels[stage.id]}</h3><p>${stageDescriptions[stage.id]}</p>${badge(`${esc(stage.status)}${stage.outcome ? ` · outcome: ${esc(stage.outcome)}` : ''}${stage.attempt > 1 ? ` · attempt ${stage.attempt}` : ''}`)}</div></li>`).join('')}</ol>${s.lastEventAt ? `<p class="field-help section-gap">Last reported event: ${esc(s.lastEventAt)}. This is a source timestamp, not a prediction.</p>` : '<p class="field-help section-gap">Waiting for the first update.</p>'}</section><section class="card"><p class="eyebrow">${q ? 'A conversation with the agent' : 'Current state'}</p><h2 class="status-heading">${heading}</h2><p class="subtle">${detail}</p>${s.status === 'ask' && !q ? '<p class="field-help section-gap">The question text has not been supplied. Import the corresponding question event to reply; no answer is assumed.</p>' : ''}${s.messages.length ? `<div class="section-gap">${s.messages.map(m => `<div class="${m.role === 'agent' ? 'question-bubble' : 'answer-bubble'}"><div class="message-meta">${m.role === 'agent' ? 'Agent question' : 'Your reply · held locally'}</div><p>${prose(m.text)}</p>${m.whyItMatters ? `<p class="section-gap"><strong>Why this matters:</strong> ${prose(m.whyItMatters)}</p>` : ''}</div>`).join('')}</div>` : ''}${q ? `<form id="answer-form" class="conversation-input">${Array.isArray(q.options) ? `<div class="option-row">${q.options.map(option => `<button type="button" class="option-button" data-action="answer-option" data-value="${esc(asText(option))}">${prose(option)}</button>`).join('')}</div>` : ''}<label class="field-label" for="answer">Your reply</label><textarea id="answer" required maxlength="2000" placeholder="You can say you don’t know, or answer in your own words."></textarea><div class="button-row"><button class="button">Save reply locally ${icon('arrow')}</button>${button('Prefer not to answer', 'skip-answer', '', 'secondary')}</div><p class="field-help">${q.hard_blocker ? 'The agent marked this as essential. If you cannot answer, it must judge whether to refuse; this interface will not guess.' : 'The agent can assess whether to continue with uncertainty stated.'}</p></form>` : ''}${['refuse', 'stop', 'score'].includes(s.status) ? `<div class="button-row">${button('View report screen', 'nav', `data-screen="${s.status === 'refuse' ? 'refusal' : s.status === 'stop' ? 'stop' : 'result'}"`, 'secondary')}</div>` : ''}<div class="section-gap"><p class="field-help">Closing this preview clears the local session. There is no background job to resume.</p></div></section></div><details class="report-details" ><summary>Preview progress states</summary><p>Manually load an example state. This does not advance an analysis or use your selected file.</p><div class="option-row">${[['question', 'Agent asks'], ['reselect', 'Choose another model'], ['blocked', 'Reference unavailable'], ['stop', 'Data stop'], ['idle', 'Reset preview']].map(([id, label]) => `<button class="option-button" data-action="progress-example" data-value="${id}">${label}</button>`).join('')}</div></details>${s.events.length ? `<details class="report-details"><summary>Event history · ${s.events.length} recorded events</summary><div class="event-log">${s.events.map(e => `<div>${esc(e.ts || 'No timestamp')} · ${esc(e.kind)}${e.stage ? ` · ${esc(e.stage)}` : ''}${e.outcome ? ` · ${esc(e.outcome)}` : ''}${e.detail ? `<br>${esc(e.detail)}` : ''}</div>`).join('')}</div></details>` : ''}<div class="button-row">${button('End this session', 'end-session', '', 'secondary')}</div>`;
}

function reportContext() {
  const result = display();
  if (['score', 'refuse'].includes(result.kind) && result.report) {
    return { ...result, example: state.bundle?.example === true, modelChoice: artifacts().model_choice };
  }
  if (!state.bundle && (state.screen === 'refusal' || state.screen === 'followup' && state.reportOrigin === 'refusal')) {
    return { kind: 'refuse', report: exampleRefusal, example: true, warnings: [] };
  }
  return { kind: 'pending', report: null, example: state.bundle?.example === true };
}
function currentTopics() { return buildReportTopics(reportContext()); }
function sameQuestion(a, b) { return a.trim().replace(/\s+/g, ' ').toLowerCase() === b.trim().replace(/\s+/g, ' ').toLowerCase(); }

function reportExplorer() {
  const context = reportContext();
  const topics = buildReportTopics(context);
  const topic = topics.find(item => item.id === state.activeTopic);
  const next = topics.find(item => !state.openedTopics.includes(item.id));
  const notebook = state.explorerView === 'questions';
  const saved = topic && state.savedQuestions.some(q => sameQuestion(q, topic.suggestedQuestion));
  return `<section class="report-explorer" id="report-explorer" aria-labelledby="explorer-title">
    <div class="explorer-heading"><div><h2 id="explorer-title">A little more clarity.</h2><p>Start with what you’re curious about.</p></div>${context.example ? badge('Example report') : state.notebookExample ? badge('Questions from example') : ''}</div>
    <div class="explorer-tabs" role="tablist" aria-label="Explore and keep questions">
      <button type="button" id="explore-tab" role="tab" aria-selected="${!notebook}" aria-controls="explorer-body" tabindex="${notebook ? '-1' : '0'}" data-action="explorer-view" data-value="explore">Explore</button>
      <button type="button" id="questions-tab" role="tab" aria-selected="${notebook}" aria-controls="explorer-body" tabindex="${notebook ? '0' : '-1'}" data-action="explorer-view" data-value="questions">My questions${state.savedQuestions.length ? ` <span class="question-count">${state.savedQuestions.length}</span>` : ''}</button>
    </div><div id="explorer-body" role="tabpanel" aria-labelledby="${notebook ? 'questions-tab' : 'explore-tab'}">
      ${notebook ? questionNotebook() : topics.length ? `<div class="topic-choices">${topics.map(item => `<button type="button" class="topic-choice ${item.id === state.activeTopic ? 'selected' : ''}" data-action="explore-topic" data-value="${item.id}" aria-expanded="${item.id === state.activeTopic}" aria-controls="topic-answer"><span>${esc(item.label)}</span>${icon('arrow')}</button>`).join('')}</div>
      ${topic ? `<article class="topic-answer" id="topic-answer" tabindex="-1" aria-labelledby="topic-title"><p class="topic-source">${esc(topic.sourceLabel)}</p><h3 id="topic-title">${esc(topic.title)}</h3>${topic.paragraphs.map(p => `<p>${esc(p)}</p>`).join('')}<div class="question-prompt"><p>A question you could keep</p><blockquote>${esc(topic.suggestedQuestion)}</blockquote><button type="button" class="text-button" data-action="keep-topic-question" data-value="${topic.id}" ${saved ? 'disabled' : ''}>${saved ? `${icon('check')} Saved to my questions` : `${icon('chat')} Keep this question`}</button></div></article>
      ${next ? `<div class="explorer-next"><span>If you’d like to explore further</span><button class="text-button" type="button" data-action="explore-topic" data-value="${next.id}">${esc(next.label)} ${icon('arrow')}</button></div>` : `<p class="explorer-done">You’ve opened all these explanations. Keep any questions, or finish whenever you’re ready.</p>`}` : '<div id="topic-answer" hidden></div><p class="explorer-caption">Open an explanation here. Your report stays above.</p>'}` : `<div class="explorer-empty"><p>There isn’t a completed report to explore yet. You can still keep questions for later.</p>${!state.bundle ? '<button class="text-button" type="button" data-action="nav" data-screen="refusal">Explore an example explanation →</button>' : ''}</div>`}
    </div></section>`;
}

function questionNotebook() {
  const editing = state.editingQuestion !== null;
  return `<div class="question-notebook"><p class="notebook-intro">Keep what you want to understand, in your own words.</p>
    ${state.savedQuestions.length ? `<ol class="saved-questions">${state.savedQuestions.map((q, index) => `<li><p>${esc(q)}</p><div class="question-actions"><button type="button" class="text-button" data-action="edit-question" data-index="${index}" aria-label="Edit question ${index + 1}">Edit</button><button type="button" class="text-button" data-action="remove-question" data-index="${index}" aria-label="Remove question ${index + 1}">Remove</button></div></li>`).join('')}</ol>` : '<p class="notebook-empty">No questions saved yet. Add your own, or keep one while exploring.</p>'}
    <form id="notebook-form"><label class="field-label" for="notebook-question">${editing ? 'Edit your question' : 'Add a question'}</label><textarea id="notebook-question" required maxlength="2000" placeholder="What would you like to understand better?" aria-describedby="notebook-privacy">${esc(state.followupDraft)}</textarea>
    <div class="notebook-form-actions"><button class="button small" type="submit">${editing ? 'Save changes' : 'Keep question'}</button>${editing ? button('Cancel edit', 'cancel-edit', '', 'ghost small') : ''}</div></form>
    ${state.notebookFeedback ? `<p class="notebook-feedback" role="status">${esc(state.notebookFeedback)}</p>` : ''}
    <p class="field-help" id="notebook-privacy">Only in this tab until you choose to download. No question is sent to an agent. Closing the tab clears this list. Save the report and questions together to reopen them later.</p>
    <div class="notebook-export"><div><h3>Take it into a conversation.</h3><p>Save an appointment note with your questions, ready to bring to a clinician or genetic counselor.</p></div>${button('Download my questions', 'download-questions', state.savedQuestions.length ? '' : 'disabled', 'secondary small')}</div></div>`;
}

function resetNotebook() {
  state.savedQuestions = [];
  state.followupDraft = '';
  state.editingQuestion = null;
  state.notebookFeedback = '';
  state.explorerView = 'explore';
  state.activeTopic = null;
  state.openedTopics = [];
  state.notebookExample = false;
  state.reportOrigin = 'result';
}
function refreshExplorer(focusSelector) {
  const region = document.querySelector('#report-explorer');
  if (region) region.outerHTML = reportExplorer();
  else render();
  if (focusSelector) document.querySelector(focusSelector)?.focus({ preventScroll: true });
}
function openTopic(id) {
  const topic = currentTopics().find(item => item.id === id);
  if (!topic) return;
  state.activeTopic = id;
  state.explorerView = 'explore';
  if (!state.openedTopics.includes(id)) state.openedTopics.push(id);
  refreshExplorer('#topic-answer');
  document.querySelector('#topic-answer')?.scrollIntoView({ block: 'nearest' });
  announce(topic.title);
}
function keepQuestion(text, index = null, preserveDraft = false) {
  const updated = updateQuestion(state.savedQuestions, text, index);
  state.savedQuestions = updated.questions;
  if (['added', 'updated'].includes(updated.status) && reportContext().example) state.notebookExample = true;
  const messages = {
    added: 'Question kept in this tab.', updated: 'Question updated.', duplicate: 'That question is already in your list.',
    empty: 'Write a question first.', 'too-long': 'Please keep the question within 2,000 characters.',
    full: 'Your list has 30 questions. You can edit or remove one before adding another.', 'invalid-index': 'That question is no longer available to edit.',
  };
  state.notebookFeedback = messages[updated.status];
  if (!preserveDraft && ['added', 'updated'].includes(updated.status)) { state.followupDraft = ''; state.editingQuestion = null; }
  announce(state.notebookFeedback);
  return updated.status;
}

function handoff() {
  return `<section class="support-handoff"><div><h3>Need help putting this in context?</h3><p>A clinician or genetic counselor can discuss what this means for your health, medication, or screening. You can bring this report to an appointment.</p></div><button class="text-button" data-action="support">Prepare for that conversation ${icon('arrow')}</button></section>`;
}

function resultScreen() {
  const d = display();
  if (d.kind === 'refuse') return refusalScreen();
  const ready = d.kind === 'score';
  const isExample = state.bundle?.example === true;
  return `${intro('07 / YOUR REPORT', ready ? isExample ? 'Example score, in context.' : 'Your score, explained.' : 'Your report will appear here.', ready ? 'Where your score stands, and what it can and cannot tell you.' : 'A score needs measured coverage and a valid comparison group. Neither is available in this preview.')}
  ${!ready ? `<div class="banner">${icon('clock')}<span>${state.bundle ? 'The imported artifacts do not yet support a complete score report. No score or percentile is displayed.' : 'No result is available. This preview has not calculated a score.'}</span></div>` : `<div class="banner">${icon('file')}<span>${isExample ? 'EXAMPLE REPORT · Illustrative values, not a person’s result.' : 'Imported report · The source and scientific claims have not been independently verified.'}</span></div>`}<div class="report-grid"><section class="result-panel"><p class="eyebrow">${ready ? 'Relative to your reference group' : 'Awaiting a valid comparison'}</p><h2>Where your score stands</h2>${ready ? `<div class="score-value">${esc(d.percentile.toLocaleString(undefined, { maximumFractionDigits: 2 }))}<small>percentile</small></div>` : '<div class="no-score">No percentile yet</div>'}<p><strong>A percentile is not a probability.</strong> It describes where your score sits relative to the comparison group. It does not tell you your chance of developing a condition.</p><p>The percentile is an estimate. It can change with the computation and the reference group, even for the same person.</p><div class="result-disclaimer"><p>${ready ? prose(d.report.delivered.disclaimer) : 'A polygenic score combines many small DNA differences using a formula from research. It is not a certainty or a diagnosis.'}</p></div></section><section class="card"><h2>What to keep in mind</h2><p>${ready ? prose(d.report.delivered.personalized_limit) : 'Your report will explain the evidence for your actual file and genetic context.'}</p><div class="measure"><div><span>Measured weight coverage</span><strong>${ready ? percent(d.coverage) : 'Not available'}</strong></div><p>The share of the model’s total weight resolved from your data. This is transparency about the score, not a correction applied to it.</p></div><div class="measure"><div><span>Comparison group size</span><strong>${ready ? esc(d.reference.n_participants.toLocaleString()) : 'Not available'}</strong></div><p>${ready ? prose(d.reference.cohort_construction_rule) : 'A comparison group must be built for your analysis.'}</p></div></section></div>
  ${ready && d.warnings.length ? `<section class="card section-gap"><h2>Other limits to know</h2><ul class="warning-list">${d.warnings.map(w => `<li>${prose(w)}</li>`).join('')}</ul></section>` : ''}${ready ? `<details class="report-details"><summary>Score, reference, and evidence</summary>${dl([['Raw weighted score', d.score], ['Scoring model', d.model], ['Reference source', d.reference.source], ['Reference release', d.reference.version_string], ['Reference assay', d.reference.assay_layer], ['Scoring parity', d.reference.scoring_parity], ['Ancestry grouping rule', d.reference.ancestry_grouping_definition], ['Cohort membership rule', d.reference.cohort_construction_rule], ['Minimum cohort size chosen by provider', d.reference.too_few_threshold_used]])}<p>Source records provide an audit trail, not a guarantee that the same reference can be rebuilt later.</p><pre>${esc(JSON.stringify(d.provenance, null, 2))}</pre></details>` : d.issues.length ? issuesPanel(d.issues) : ''}${reportExplorer()}<div class="button-row">${button(state.savedQuestions.length ? 'Save report & questions' : 'Save report', 'download-report', !ready ? 'disabled' : '')}${button('Exit & clear', 'end-session', '', 'ghost')}</div>${ready ? '<p class="field-help">Your download includes this report and your saved questions.</p>' : ''}${handoff()}`;
}
function refusalScreen() {
  const d = display();
  const localExample = !state.bundle;
  const isExample = localExample || state.bundle.example === true;
  const report = localExample ? exampleRefusal : d.kind === 'refuse' && d.envelope ? d.report : null;
  const envelope = report?.refusal_envelope;
  if (!envelope) return `${intro('', 'Your explanation is still needed.', 'The recorded refusal does not yet include a complete explanation written to you.')}<div class="banner">${icon('clock')}<span>No replacement explanation has been generated.</span></div>${d.issues.length ? issuesPanel(d.issues) : ''}${evidence(d.artifact)}${handoff()}<div class="button-row">${button('Back to progress', 'nav', 'data-screen="progress"', 'secondary')}</div>`;
  return `${isExample ? exampleBanner('Example explanation · No genome has been analyzed. This is not your result.') : ''}<div class="report-status">${badge('Outcome · Refuse')}</div>${intro('', prose(envelope.headline), prose(envelope.detail))}
  ${envelope.is_about_the_person === false ? `<div class="person-note">${icon('leaf')}<p>This is a limit of the available science, data, or assay — not a finding about your body, and not your fault.</p></div>` : ''}
  <section class="report-body"><section class="report-section"><h2>What was tried</h2><p>${prose(envelope.what_was_tried)}</p></section><section class="report-section"><h2>Why this is the honest answer</h2><p>${prose(envelope.why_this_is_honest)}</p></section><section class="report-section"><h2>What could change the answer</h2><p>${prose(envelope.what_would_change_it)}</p></section></section>
  ${evidence(report)}${reportExplorer()}<div class="button-row">${button(isExample ? 'Save example & questions' : 'Save report & questions', 'download-report')}${button('Exit', 'end-session', '', 'ghost')}</div>${handoff()}`;
}

function stopScreen() {
  const d = display();
  const actual = d.kind === 'stop' ? d.artifact : null;
  return `${intro('07 / THE ANALYSIS PAUSED HERE', 'Your file needs a closer look.', 'A stop means the data could not support the next step. It is a finding about the input, not a risk result.')}${actual ? `<div class="banner">${icon('file')}<span>Imported outcome · Stop</span></div>` : exampleBanner('Example stop screen · No selected file has been inspected or found to have a problem.')}<section class="card reading"><p class="eyebrow">${actual ? 'Recorded decision' : 'Example · unresolved genome build'}</p><h2>${actual ? prose(actual.decision) : 'The genome build could not be established.'}</h2><p>${actual ? list(actual.reasons).map(prose).join('<br>') : 'In this example, the header and the coordinate checks do not establish which genome build the file uses. The analysis cannot safely match positions to a scoring model.'}</p><h2>What needs resolving</h2><p>${actual ? 'The recorded evidence below identifies the unresolved input. A connected agent needs to explain which original file or source detail would address it.' : 'The original export and its provenance would need to be reviewed. Renaming the file would not resolve its contents.'}</p><p>No score or percentile is presented. This is different from a scientific refusal after evaluating an otherwise usable file.</p>${evidence(actual)}<div class="button-row">${button('Return to your file', 'nav', 'data-screen="upload"')}${button('End this session', 'end-session', '', 'secondary')}</div></section>`;
}
function connectionScreen() {
  return `${intro('', 'Bring your own provider.', 'Connection isn’t set up yet. Both options are still being considered; this preview accepts no credentials.')}
  <div class="auth-options"><section class="card">${badge('Option A')}<h2>Use an API key</h2><p>You supply a Claude or OpenAI API key. The app would become responsible for handling that secret.</p><details class="report-details"><summary>What this would involve</summary><p>Browser code and any server proxy would need a secure design, including protection from logs, storage, errors, and other scripts.</p><p>“We never store it” must be supported by the actual architecture. Scope, expiry, and revocation must be explained.</p></details></section><section class="card">${badge('Option B')}<h2>Sign in to your provider</h2><p>A supported provider sign-in could avoid pasting a key. The app would still need to protect its access tokens.</p><details class="report-details"><summary>What this would involve</summary><p>This requires a provider-supported authorization flow for this use, with clear permissions, expiry, and revocation.</p><p>The current harness uses Claude CLI authentication. It does not establish browser sign-in or an OpenAI integration.</p></details></section></div>
  <div class="plain-note"><h3>Before either option is available</h3><p>Subscription coverage and API charges need verification. Before any transfer, you must be told where your DNA goes, who can access it, and how it is retained or deleted.</p></div>`;
}

function privacyScreen() {
  return `${intro('YOUR DATA / YOUR CONTROL', 'Your data stays yours.', 'Your DNA is deeply identifying. You should know where your file goes and who can access it before you choose it.')}<section class="card reading"><h2>What this preview does</h2><p>When you select or drop a DNA file, the browser holds a reference to that file and displays its name and size. This app does not read its contents, upload it, classify it, or run an analysis.</p><p>Your question and optional self-description are kept in memory in this tab. The app uses no browser storage, cookies, analytics, or third-party assets. Its security policy disables network connections from application code.</p><h2>What importing a report does</h2><p>If you choose a saved report, the browser reads that file locally so you can see the explanation and any questions saved with it. Nothing is uploaded. A report can contain sensitive information even without the original DNA file.</p><h2>How to clear it</h2><p>Remove a selected file to release its file reference. Exit the session to clear the file reference, questions, imported artifacts, and local conversation. Closing or refreshing this tab also clears this app’s in-memory state. This does not delete the original file or reports you chose to download.</p><h2>What a connected version must tell you</h2><p>Before transferring anything, it needs to name the destination, who operates it, what leaves your device, what the AI provider can see, retention and deletion rules, and any secondary services. Those architectural decisions are still open. This preview cannot give consent for a future transfer.</p><div class="button-row">${button('Back to your file', 'nav', 'data-screen="upload"')}${button('Exit & clear', 'end-session', '', 'secondary')}</div></section>`;
}
function howScreen() {
  return `${intro('THE PROCESS', 'How it works.', 'A connected assistant would work through these six checks, explaining each decision. It may ask for your input along the way. This preview does not run these checks.')}<section class="card">${Object.entries(stageLabels).map(([id, label], i) => `<div class="how-row"><span>0${i + 1}</span><div><h3>${label}</h3><p>${stageDescriptions[id]}</p></div></div>`).join('')}</section><div class="split section-gap"><section class="support-card"><h2>Refusal is a correct answer.</h2><p class="subtle">If the available evidence cannot support a meaningful result, the agent explains why and what could change it. It never offers an unreliable number “anyway.”</p></section><section class="support-card"><h2>There is room to ask.</h2><p class="subtle">A question is a conversation. A data problem is a stop. Waiting for a reference is a separate state. Ending the session is your choice, not an agent refusal.</p></section></div><div class="button-row">${button('Read an example explanation', 'nav', 'data-screen="refusal"', 'secondary')}${button('Start with your file', 'nav', 'data-screen="upload"')}</div>`;
}
function followupScreen() {
  return `${intro('', 'Make this useful to you.', 'Explore the explanation, keep your questions, and leave with something you can use.')} ${reportExplorer()}<div class="button-row">${button('Back to report', 'nav', `data-screen="${state.reportOrigin}"`, 'secondary')}${button('Finish & clear', 'end-session', '', 'ghost')}</div>`;
}

function artifactsScreen() {
  return `${intro('LOCAL ARTIFACT VIEWER', 'Open your saved report.', 'Choose a report you saved from prs-agent to read it again and restore any questions you kept with it. Nothing is uploaded or analyzed.')}<section class="card"><div class="privacy-note">${icon('lock')}<div><strong>Only the report file you choose is read.</strong><p>It stays in memory in this tab. It may contain sensitive genetic information. It is never sent to a server.</p></div></div><div class="import-area"><h2>Choose your report file</h2><p class="subtle">Saved reports use the JSON file format to keep the explanation and questions together. Choose that downloaded report here, rather than your original DNA data file.</p><div class="button-row">${button('Choose report file', 'choose-artifacts', '', 'secondary')}${state.bundle ? button('Clear this report', 'clear-artifacts', '', 'ghost') : ''}</div><input type="file" id="artifact-file" class="sr-only" tabindex="-1" accept="application/json,.json" aria-label="Choose saved report JSON file"><p class="field-help">Files up to 5 MB. If a report is incomplete, only the information it contains can be shown.</p>${state.importError ? `<div class="banner example section-gap" role="alert">${esc(state.importError)}</div>` : ''}${state.bundle ? `<p class="feedback">Report opened in this tab.</p><div class="button-row">${button('Read your report', 'view-import')}${button('Review analysis steps', 'nav', 'data-screen="progress"', 'secondary')}</div>${display().issues.length ? issuesPanel(display().issues) : ''}` : ''}</div><details class="report-details"><summary>Technical details for developers</summary><pre>{
  "classification": { /* door/classification.json */ },
  "ancestry": { /* ancestry/ancestry.json */ },
  "model_choice": { /* select/model_choice.json */ },
  "score": { /* score/score.json or raw_score.json */ },
  "coverage": { /* measured score/coverage_&lt;PGS&gt;.json */ },
  "interpret": { /* interpret/interpret.json */ },
  "reference_response": { /* validated reference response */ },
  "report": { /* report/report.json */ },
  "events": [ /* ordered session.jsonl events, if available */ ]
}</pre><p>This is a structural outline, not valid example data. Omit absent artifacts. The frontend never fills missing results.</p><p>See FRONTEND.md in the repository for the full integration contract and unresolved backend requirements.</p></details></section>`;
}
function endedScreen() {
  const imported = Boolean(state.bundle);
  return `${intro(imported ? 'RECORDED SESSION ENDING' : 'SESSION ENDED BY YOU', imported ? 'This session was ended<br>by the person.' : 'Your session is closed.', imported ? 'The imported record says the person chose to leave. These imported artifacts are still held in this tab until you clear them.' : 'Your file reference, imported artifacts, and answers have been cleared from this tab. Original files and any downloaded reports remain on your device.')}<section class="card reading"><div class="end-icon">${icon('check')}</div><h2>${imported ? 'A person’s choice is separate from an agent judgment.' : 'This was your choice.'}</h2><p>Ending a session is neither a refusal nor a data stop. No live job was running in this preview.</p><div class="button-row">${imported ? button('Exit & clear imported artifacts', 'end-session') : button('Start a new local session', 'new-session')}${button('Review data handling', 'nav', 'data-screen="privacy"', 'secondary')}</div></section>`;
}

const screens = { home: homeScreen, upload: uploadScreen, file: fileScreen, question: questionScreen, ancestry: ancestryScreen, model: modelScreen, progress: progressScreen, result: resultScreen, refusal: refusalScreen, stop: stopScreen, connection: connectionScreen, privacy: privacyScreen, how: howScreen, followup: followupScreen, artifacts: artifactsScreen, ended: endedScreen };
function render(focus = false) {
  app.innerHTML = shell((screens[state.screen] || uploadScreen)());
  document.title = `${({ home: 'Make sense of your DNA', upload: 'Start with your DNA file', refusal: 'An honest limit', result: 'Your report', question: 'Your question', connection: 'Connection options' }[state.screen] || state.screen.charAt(0).toUpperCase() + state.screen.slice(1))} · prs-agent`;
  if (focus) document.querySelector('#main').focus({ preventScroll: true });
  attachFileEvents();
  document.querySelector('#preview-tools')?.addEventListener('toggle', event => {
    if (app.contains(event.currentTarget)) state.toolsOpen = event.currentTarget.open;
  });
}
function navigate(screen) {
  if (['result', 'refusal'].includes(screen)) state.reportOrigin = screen;
  if (screen === 'model' && artifacts().ancestry?.agreement === 'disagree' && !state.ancestrySeen) {
    screen = 'ancestry';
    state.notice = 'Before reviewing a model, take a moment to read the difference in ancestry context.';
  } else state.notice = '';
  state.screen = screens[screen] ? screen : 'upload';
  render(true);
  window.scrollTo({ top: 0, behavior: 'instant' });
}
function announce(text) { document.querySelector('#announcement').textContent = text; }
function formatBytes(size) {
  if (size < 1024) return `${size} bytes`;
  const unit = size < 1024 ** 2 ? 'KiB' : size < 1024 ** 3 ? 'MiB' : 'GiB';
  const divisor = unit === 'KiB' ? 1024 : unit === 'MiB' ? 1024 ** 2 : 1024 ** 3;
  return `${(size / divisor).toLocaleString(undefined, { maximumFractionDigits: 1 })} ${unit}`;
}
function selectGenome(file) {
  if (!file) return;
  state.importVersion++;
  state.file = file;
  resetNotebook();
  // Changing the source cannot reuse conclusions from an unrelated imported run.
  state.bundle = null;
  state.session = createSessionState();
  state.previewEvents = false;
  state.acknowledged = false;
  state.ancestrySeen = false;
  state.importError = '';
  state.notice = 'Selected locally. Nothing has been read, uploaded, or analyzed.';
  render();
  announce('File selected locally. Contents have not been read or uploaded.');
}
function sessionFromBundle(bundle) {
  if (list(bundle.events).length) return bundle.events.reduce(applySessionEvent, createSessionState());
  const a = readArtifacts(bundle);
  let session = createSessionState();
  // A source judgment can establish status without a live event stream. These
  // projection events have explicit artifact provenance and no invented times.
  for (const [key, stage] of Object.entries({ classification: 'door', ancestry: 'ancestry', model_choice: 'select', score: 'score', interpret: 'interpret', report: 'report' })) {
    if (a[key]?.outcome) session = applySessionEvent(session, { kind: 'stage_completed', stage, outcome: a[key].outcome, source_artifact: key });
  }
  if (['reference_unavailable', 'reference_invalid', 'request_missing'].includes(a.reference_response?.status)) {
    session = applySessionEvent(session, { kind: 'blocked', stage: 'interpret', source_artifact: 'reference_response' });
  }
  if (bundle.session_ended_by_person === true || bundle.status === 'session-ended-by-person') session = applySessionEvent(session, { kind: 'session_end', source_artifact: 'bundle' });
  return session;
}
function attachFileEvents() {
  document.querySelector('#genome-file')?.addEventListener('change', e => selectGenome(e.target.files[0]));
  document.querySelector('#artifact-file')?.addEventListener('change', async e => {
    const file = e.target.files[0];
    if (!file) return;
    const importVersion = ++state.importVersion;
    state.importError = '';
    if (file.size > 5 * 1024 * 1024) { state.importError = 'This viewer accepts JSON artifacts up to 5 MiB. No file was read.'; render(); return; }
    try {
      const text = await file.text();
      if (importVersion !== state.importVersion) return;
      const bundle = JSON.parse(text);
      if (!bundle || typeof bundle !== 'object' || Array.isArray(bundle)) throw new Error('Choose a JSON object containing stage artifacts.');
      const keys = ['classification', 'ancestry', 'model_choice', 'score', 'coverage', 'measured_coverage', 'interpret', 'report', 'reference_response', 'stages', 'events'];
      if (!keys.some(key => Object.hasOwn(bundle, key))) throw new Error('This JSON does not contain a recognized artifact bundle.');
      // Limit repeated event rendering without touching the DNA input or performing inference.
      if (bundle.events && (!Array.isArray(bundle.events) || bundle.events.length > 5000)) throw new Error('Events must be an array of at most 5,000 entries.');
      state.bundle = bundle;
      state.file = null;
      state.trait = '';
      state.selfReport = '';
      state.ancestrySeen = false;
      state.acknowledged = false;
      state.session = sessionFromBundle(bundle);
      state.gateReply = ''; state.explanation = null;
      resetNotebook();
      state.savedQuestions = readNotebook(bundle.frontend_notes);
      state.notebookExample = bundle.example === true && state.savedQuestions.length > 0;
      state.reportOrigin = getDisplayResult(bundle).kind === 'refuse' ? 'refusal' : 'result';
      state.previewEvents = bundle.example === true;
      state.notice = 'Imported locally. These artifacts are separate from any file previously selected in this preview.';
      if (state.savedQuestions.length) state.notice += ` Restored ${state.savedQuestions.length} saved question${state.savedQuestions.length === 1 ? '' : 's'}.`;
      else if (bundle.frontend_notes !== undefined && !(bundle.frontend_notes?.schema_version === 1 && Array.isArray(bundle.frontend_notes.questions) && bundle.frontend_notes.questions.length === 0)) state.notice += ' Saved questions could not be restored: the notes format is invalid.';
    } catch (error) {
      if (importVersion !== state.importVersion) return;
      state.importError = error instanceof SyntaxError ? 'This file is not valid JSON. Nothing was imported.' : error.message;
    }
    render();
  });
  const drop = document.querySelector('#dropzone');
  if (drop) {
    drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('dragging'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('dragging'));
    drop.addEventListener('drop', e => { e.preventDefault(); selectGenome(e.dataTransfer.files[0]); });
  }
}
function previewProgress(kind) {
  state.importVersion++;
  resetNotebook();
  state.bundle = null;
  state.session = createSessionState();
  state.previewEvents = kind !== 'idle';
  const apply = event => { state.session = applySessionEvent(state.session, event); };
  if (kind !== 'idle') {
    apply({ kind: 'run_started' });
    for (const stage of ['door', 'ancestry']) apply({ kind: 'stage_completed', stage, outcome: 'proceed' });
  }
  if (kind === 'question') apply({ ...exampleQuestion });
  if (kind === 'blocked') {
    for (const stage of ['select', 'score']) apply({ kind: 'stage_completed', stage, outcome: 'score' });
    apply({ kind: 'blocked', stage: 'interpret', detail: 'Example: reference provider has not answered. No interpretation judgment exists.' });
  }
  if (kind === 'reselect') {
    apply({ kind: 'stage_completed', stage: 'select', outcome: 'proceed' });
    apply({ kind: 'stage_started', stage: 'score' });
    apply({ kind: 'stage_completed', stage: 'score', outcome: 'score', detail: 'Example: measured coverage requires reconsidering the candidate; no numeric measurement is supplied.' });
    apply({ kind: 'stage_started', stage: 'select', detail: 'Example: returning to the next ranked candidate. The agent must explain the new choice and cost.' });
  }
  if (kind === 'stop') {
    state.session = createSessionState();
    apply({ kind: 'stage_completed', stage: 'door', outcome: 'stop', detail: 'Example: the file’s genome build could not be established.' });
  }
  state.notice = '';
  render(true);
}
function answerQuestion(text, skipped = false) {
  const q = state.session.pendingQuestion;
  if (!q) return;
  state.session = applySessionEvent(state.session, { kind: 'answer', question_id: q.question_id, text, skipped });
  state.notice = 'Your reply is held locally. No backend has received it or judged what should happen next.';
  render(true);
}
function endSession() {
  state.importVersion++;
  resetNotebook();
  state.file = null; state.bundle = null; state.trait = ''; state.selfReport = '';
  state.savedQuestions = []; state.followupDraft = ''; state.gateReply = '';
  state.explanation = null; state.ancestrySeen = false; state.acknowledged = false;
  state.previewEvents = false; state.exampleMismatch = false; state.importError = ''; state.notice = '';
  state.session = applySessionEvent(createSessionState(), { kind: 'session_end', outcome: 'session-ended-by-person' });
  navigate('ended');
}
function downloadReport() {
  const d = reportContext();
  if (!['score', 'refuse'].includes(d.kind)) return;
  const content = {
    ...(d.example ? { example: true, notice: 'EXAMPLE ONLY. This is not a personal result.' } : {}),
    report: d.report,
    ...(d.kind === 'score' ? { model_choice: artifacts().model_choice, score: artifacts().score, interpret: artifacts().interpret, coverage: artifacts().coverage, reference_response: artifacts().reference_response } : {}),
    frontend_notes: { schema_version: 1, questions: [...state.savedQuestions] },
  };
  downloadFile(JSON.stringify(content, null, 2), 'application/json', d.example ? 'prs-agent-EXAMPLE-report-and-questions.json' : 'prs-agent-report-and-questions.json');
  announce(d.example ? 'Example report and questions saved with an explicit example label.' : 'Report and questions saved to your device.');
}
function downloadFile(content, type, filename) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function downloadQuestions() {
  if (!state.savedQuestions.length) return;
  const context = reportContext();
  const example = context.example || state.notebookExample;
  const source = context.kind === 'refuse' ? context.report.refusal_envelope.headline : context.kind === 'score' ? `Scoring model: ${context.model}` : 'No completed report loaded.';
  const content = [
    ...(example ? ['EXAMPLE REPORT CONTEXT — not a personal result.', ''] : []),
    'My questions for a conversation', '', source, '',
    'These are my saved questions, not answers from an agent or medical advice.',
    'I can bring my report and these questions to a clinician or genetic counselor.', '',
    ...state.savedQuestions.map((q, index) => `${index + 1}. ${q}\n`),
    'Keep this note private if it contains personal or genetic information.', '',
  ].join('\n');
  downloadFile(content, 'text/plain;charset=utf-8', example ? 'prs-agent-EXAMPLE-questions.txt' : 'prs-agent-my-questions.txt');
  announce('Your questions have been saved to your device.');
}
function switchExplorer(view) {
  if (!['explore', 'questions'].includes(view)) return;
  state.explorerView = view;
  refreshExplorer(view === 'questions' ? '#questions-tab' : '#explore-tab');
}

app.addEventListener('click', event => {
  const target = event.target.closest('[data-action]');
  if (!target || target.disabled) return;
  const action = target.dataset.action;
  if (action === 'nav') navigate(target.dataset.screen);
  else if (action === 'choose-file') document.querySelector('#genome-file')?.click();
  else if (action === 'remove-file') { state.file = null; state.notice = 'File reference removed. The original file on your device is unchanged.'; render(); announce('File reference removed.'); }
  else if (action === 'choose-artifacts') document.querySelector('#artifact-file')?.click();
  else if (action === 'clear-artifacts') { state.importVersion++; state.bundle = null; resetNotebook(); state.session = createSessionState(); state.importError = ''; state.notice = 'Imported artifacts and their questions cleared from this tab.'; render(); }
  else if (action === 'ancestry-continue') { state.ancestrySeen = true; navigate('model'); }
  else if (action === 'end-session') endSession();
  else if (action === 'new-session') { state.session = createSessionState(); navigate('upload'); }
  else if (action === 'progress-example') previewProgress(target.dataset.value);
  else if (action === 'answer-option') { const field = document.querySelector('#answer'); field.value = target.dataset.value; field.focus(); }
  else if (action === 'skip-answer') answerQuestion('I prefer not to answer.', true);
  else if (action === 'support') {
    if (!document.querySelector('#report-explorer')) navigate('followup');
    if (currentTopics().some(topic => topic.id === 'support')) openTopic('support');
    else switchExplorer('questions');
  }
  else if (action === 'explorer-view') switchExplorer(target.dataset.value);
  else if (action === 'explore-topic') openTopic(target.dataset.value);
  else if (action === 'keep-topic-question') {
    const topic = currentTopics().find(item => item.id === target.dataset.value);
    if (topic) { keepQuestion(topic.suggestedQuestion, null, true); refreshExplorer('#questions-tab'); }
  }
  else if (action === 'edit-question') {
    const index = Number(target.dataset.index);
    if (Number.isInteger(index) && index >= 0 && index < state.savedQuestions.length) {
      state.editingQuestion = index; state.followupDraft = state.savedQuestions[index];
      state.notebookFeedback = ''; refreshExplorer('#notebook-question');
    }
  }
  else if (action === 'remove-question') {
    const index = Number(target.dataset.index);
    if (Number.isInteger(index) && index >= 0 && index < state.savedQuestions.length) {
      state.savedQuestions = state.savedQuestions.filter((_, i) => i !== index);
      if (state.editingQuestion === index) { state.editingQuestion = null; state.followupDraft = ''; }
      else if (state.editingQuestion > index) state.editingQuestion--;
      state.notebookFeedback = 'Question removed.';
      refreshExplorer(state.savedQuestions.length ? `[data-action="edit-question"][data-index="${Math.min(index, state.savedQuestions.length - 1)}"]` : '#notebook-question');
      announce(state.notebookFeedback);
    }
  }
  else if (action === 'cancel-edit') { state.editingQuestion = null; state.followupDraft = ''; state.notebookFeedback = ''; refreshExplorer('#notebook-question'); }
  else if (action === 'download-report') downloadReport();
  else if (action === 'download-questions') downloadQuestions();
  else if (action === 'acknowledge') { state.notice = 'Acknowledgment recorded locally. No analysis has started.'; render(); }
  else if (action === 'view-import') { const kind = display().kind; navigate(kind === 'refuse' ? 'refusal' : kind === 'stop' ? 'stop' : kind === 'session-ended-by-person' ? 'ended' : ['ask', 'blocked'].includes(kind) ? 'progress' : 'result'); }
});
app.addEventListener('change', event => {
  if (event.target.id === 'screen-preview') { if (event.target.value === 'ancestry') state.exampleMismatch = !artifacts().ancestry; navigate(event.target.value); }
  if (event.target.id === 'model-ack') { state.acknowledged = event.target.checked; const button = app.querySelector('[data-action="acknowledge"]'); if (button) button.disabled = !state.acknowledged; }
});
app.addEventListener('input', event => {
  if (event.target.id === 'trait') state.trait = event.target.value;
  if (event.target.id === 'self-report') state.selfReport = event.target.value;
  if (event.target.id === 'gate-answer') state.gateReply = event.target.value;
  if (event.target.id === 'notebook-question') state.followupDraft = event.target.value;
});
app.addEventListener('keydown', event => {
  if (!event.target.matches('.explorer-tabs [role="tab"]') || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const view = event.key === 'Home' ? 'explore' : event.key === 'End' ? 'questions' : state.explorerView === 'explore' ? 'questions' : 'explore';
  switchExplorer(view);
});
app.addEventListener('submit', event => {
  event.preventDefault();
  if (event.target.id === 'trait-form') navigate('ancestry');
  if (event.target.id === 'answer-form') answerQuestion(document.querySelector('#answer').value.trim());
  if (event.target.id === 'gate-form') { state.notice = 'Your decision response is held locally. A connected agent must consider it before proceeding.'; render(true); }
  if (event.target.id === 'notebook-form') { keepQuestion(state.followupDraft, state.editingQuestion); refreshExplorer('#notebook-question'); }
});
// Prevent accidental navigation to a dropped genome outside the drop target.
document.addEventListener('dragover', event => event.preventDefault());
document.addEventListener('drop', event => event.preventDefault());
render();
