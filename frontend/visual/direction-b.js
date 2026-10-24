import {
  escapeHTML, stageInfo, timingInfo, timelineView,
  reportContent, questionContent, alertControl,
} from './presentation.mjs';

const arrow = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5"/></svg>';

/** Editorial direction: the same run, presented as a quiet research journal. */
export function renderDirectionB(state, now = Date.now()) {
  const run = state.run || {};
  const complete = run.status === 'completed';
  const waiting = Boolean(run.question) && !complete;
  const { stages, current } = stageInfo(run);
  const timing = timingInfo(run, now);
  const currentNumber = current ? stages.findIndex(stage => stage.id === current.id) + 1 : 0;
  const status = complete ? 'Analysis complete' : waiting ? 'Your answer is needed' : 'Analysis in progress';
  const sidebar = `<aside class="journal-sidebar" aria-label="Workspace navigation">
    <a class="journal-brand" href="#main"><img src="/mark.svg" alt=""><span>prs-agent</span></a>
    <div class="journal-sidebar-body">
      <p class="journal-sidebar-label">Your analysis</p>
      <nav aria-label="On this page">
        <a class="journal-nav-current" href="#overview" aria-current="location">${complete ? 'Your result' : 'Workspace'}${arrow}</a>
        <a href="#run-history">Run timeline<span aria-hidden="true">↗</span></a>
      </nav>
    </div>
    <div class="journal-sidebar-bottom"><p>Genetics,<br>with context.</p><button type="button" data-action="logout">Sign out ${arrow}</button></div>
  </aside>`;
  const running = `<section id="overview" class="journal-overview" aria-labelledby="current-step">
    <div class="journal-heading-line"><p class="journal-eyebrow">${currentNumber ? `Step ${currentNumber} of ${stages.length}` : 'Your analysis'}</p><span class="journal-status ${waiting ? 'needs-answer' : ''}"><span aria-hidden="true"></span>${status}</span></div>
    ${waiting ? `<h1 id="current-step">A moment for<br>your input.</h1><p class="journal-deck">The analysis is waiting for your answer${current ? ` at “${escapeHTML(current.label)}”` : ''}.</p>` : `<h1 id="current-step">${escapeHTML(current?.label || 'Analysis in progress')}</h1><p class="journal-deck">${escapeHTML(current?.description || 'The next step will appear when the agent reports it.')}</p>`}
    ${waiting ? `<div class="journal-question" role="region" aria-label="Answer needed" aria-live="polite">${questionContent(run.question)}</div>` : ''}
    <div class="journal-timing">
      <div class="journal-time-main"><span class="journal-eyebrow">${waiting ? 'Time estimate' : 'Estimated time remaining'}</span><p>${escapeHTML(timing.primary)}</p><span class="journal-time-caption">${escapeHTML(timing.caption)}</span></div>
      <div class="journal-attention"><p>${waiting ? 'Your answer lets the analysis continue.' : 'Nothing you need to do right now.'}</p>${timing.elapsed ? `<span class="journal-elapsed">${escapeHTML(timing.elapsed)}</span>` : ''}${alertControl()}</div>
    </div>
  </section>`;
  const result = `<section id="overview" class="journal-result" aria-label="Your result"><div class="journal-heading-line"><p class="journal-eyebrow">Your result</p><span class="journal-status completed"><span aria-hidden="true">✓</span>${status}</span></div>${reportContent(run.report)}</section>`;
  return `<div class="direction-b ${complete ? 'journal-is-complete' : ''} ${waiting ? 'journal-is-waiting' : ''}">
    <a href="#main" class="journal-skip">Skip to the analysis</a>${sidebar}
    <main id="main" class="journal-main" tabindex="-1">
      ${state.error ? `<div class="journal-message" role="alert">${escapeHTML(state.error)}</div>` : ''}
      ${state.notice ? `<div class="journal-message" role="status">${escapeHTML(state.notice)}</div>` : ''}
      ${complete ? result : running}
      <section class="journal-history" id="run-history" aria-labelledby="journal-history-heading"><div class="journal-section-heading"><div><p class="journal-eyebrow">The work behind it</p><h2 id="journal-history-heading">Run timeline</h2></div><span class="journal-rule" aria-hidden="true"></span></div>${timelineView(run)}</section>
      <footer class="journal-footer"><p>Private demo · Research tool. Not medical advice.</p><button class="journal-end" type="button" data-action="end">End analysis &amp; delete files ${arrow}</button></footer>
    </main>
  </div>`;
}
