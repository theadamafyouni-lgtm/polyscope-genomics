// Local screenshots and interaction checks; every API request is intercepted.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/check-round3.mjs
// UI_ROUND3_OUTPUT changes only the review-output directory.
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { startPreviewServer } from './server.mjs';
import { FIXED_NOW, sessionFixture } from './fixtures.mjs';
import { round3Runs, documentationResult, documentationBundle, documentedDelivery, documentedQuestion, provenance } from './round3-fixtures.mjs';
import { getDisplayResult } from '../contracts.js';
import { renderPage, reportView, scoreReportView } from '../workspace.js';

const tools = process.env.UI_REVIEW_TOOLS || '/tmp/ui-refresh-tools/node_modules';
const { chromium } = await import(pathToFileURL(`${tools}/playwright/index.mjs`).href);
const { default: AxeBuilder } = await import(pathToFileURL(`${tools}/@axe-core/playwright/dist/index.mjs`).href);
const output = resolve(process.env.UI_ROUND3_OUTPUT || '/root/reviews/ui-shots/round3');
const layouts = { desktop: { width: 1440, height: 1000 }, phone: { width: 390, height: 844 }, narrow: { width: 320, height: 844 } };
const states = (process.env.UI_ROUND3_STATES || 'documentation,score,refusal,question,question-options-only,question-freeform').split(',');
const devices = (process.env.UI_ROUND3_DEVICES || Object.keys(layouts).join(',')).split(',');
const web = await readFile(new URL('../../demo_site/web.py', import.meta.url), 'utf8');
const csp = web.match(/self\.send_header\("Content-Security-Policy", "([^"]+)"\)/)?.[1];
if (!csp) throw new Error('Production CSP is missing.');
const allowedAssets = new Set([...web.matchAll(/^(?:PUBLIC|PRIVATE) = \{([^\n]+)\}/gm)]
  .flatMap(match => [...match[1].matchAll(/"([^"]+)"/g)].map(name => `/${name[1]}`)));
if (getDisplayResult(round3Runs.score.report.bundle).kind !== 'score') throw new Error('The integrated score fixture must pass the current production contract.');
if (getDisplayResult(documentationBundle).kind === 'score') throw new Error('The incomplete documentation example must not pass as a run.');
const documentationNotice = '<p class="field-help">Documentation example · layout preview. No analysis was performed. The example supplies no individual comparison scores.</p>';
const shell = renderPage({ ready: true, session: sessionFixture, run: round3Runs.score, pending: '', notice: '', error: '' }, FIXED_NOW);
const documentationBody = shell.replace(reportView(round3Runs.score.report), `${documentationNotice}${scoreReportView(documentationResult, documentationBundle)}`);
const documentationHTML = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Documentation example · layout preview</title><link rel="icon" href="/mark.svg"><link rel="stylesheet" href="/demo.css"><script type="module" src="/login.js"></script></head><body>${documentationBody}</body></html>`;

const report = { ...provenance, checkedAt: new Date().toISOString(), productionContentSecurityPolicy: csp,
  checks: [], screenshots: [], failures: [], browser: '', documentationPreviewIsNotAnAPIResponse: true };
const check = (name, passed, detail = {}) => {
  const entry = { name, passed: Boolean(passed), ...detail };
  report.checks.push(entry);
  if (!passed) { report.failures.push(entry); console.error(`FAIL ${name}: ${JSON.stringify(detail)}`); }
};
await mkdir(output, { recursive: true });
await mkdir(resolve(output, 'contract-fixture'), { recursive: true });
const { server, baseURL } = await startPreviewServer();
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
report.browser = browser.version();

async function openPage(state, viewport) {
  const documentation = state === 'documentation';
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, locale: 'en-US', timezoneId: 'UTC',
    reducedMotion: 'reduce', colorScheme: 'light', isMobile: viewport.width < 600, hasTouch: viewport.width < 600 });
  const page = await context.newPage();
  let current = structuredClone(round3Runs[state] || round3Runs.score), reads = 0;
  const errors = [], blocked = [], posts = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.addInitScript(() => {
    window.__round3Violations = [];
    document.addEventListener('securitypolicyviolation', event => window.__round3Violations.push({ directive: event.violatedDirective, blockedURI: event.blockedURI }));
  });
  await page.clock.install({ time: FIXED_NOW });
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== baseURL) { blocked.push(url.href); return route.abort(); }
    if (documentation && url.pathname === '/__round3_documentation.html') return route.fulfill({ contentType: 'text/html', headers: { 'Content-Security-Policy': csp }, body: documentationHTML });
    if (url.pathname.startsWith('/api/')) {
      if (documentation) { blocked.push(url.href); return route.abort(); }
      if (url.pathname === '/api/session' && request.method() === 'GET') return route.fulfill({ json: sessionFixture });
      if (url.pathname === '/api/run' && request.method() === 'GET') { reads++; return route.fulfill({ json: current }); }
      if (url.pathname === '/api/answer' && request.method() === 'POST') {
        posts.push({ path: url.pathname, body: request.postDataJSON(), csrf: request.headers()['x-csrf-token'] });
        current = { ...current, status: 'running', question: null };
        return route.fulfill({ json: current });
      }
      blocked.push(`${request.method()} ${url.href}`); return route.fulfill({ status: 405, json: { error: 'Local fixture accepts only its intercepted answer.' } });
    }
    if (request.isNavigationRequest() && url.pathname === '/demo.html') {
      const response = await route.fetch();
      return route.fulfill({ response, headers: { ...response.headers(), 'content-security-policy': csp } });
    }
    if (!allowedAssets.has(url.pathname) || url.search) { blocked.push(url.href); return route.abort(); }
    return route.continue();
  });
  await page.goto(`${baseURL}/${documentation ? '__round3_documentation' : 'demo'}.html`, { waitUntil: 'networkidle' });
  await page.locator('h1').first().waitFor();
  await page.evaluate(() => document.fonts.ready);
  async function redraw() {
    const oldReads = reads;
    await page.evaluate(() => { window.__round3BeforeHTML = document.querySelector('#app').innerHTML; });
    // Change a recorded progress note to exercise DOM replacement without
    // changing the measured estimate shown in screenshots.
    current = { ...current, progress: [...current.progress, {
      kind: 'stage_progress', stage: current.question?.stage || (current.status === 'completed' ? 'report' : 'score'),
      text: 'Example test progress note.',
    }] };
    // A pending fetch schedules its next timer only after settling. Advance in
    // bounded steps and wait for the actual new response and changed DOM, rather
    // than treating elapsed mock time as proof that a redraw occurred.
    for (let attempt = 0; attempt < 3 && reads === oldReads; attempt++) {
      await page.clock.runFor(2200);
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    await page.waitForFunction(() => document.querySelector('#app').innerHTML !== window.__round3BeforeHTML);
    check(`${state}: polling continues`, reads > oldReads, { before: oldReads, after: reads });
  }
  return { page, context, errors, blocked, posts, redraw, current: () => current };
}

async function fonts(page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('DOM.enable'); await cdp.send('CSS.enable');
  const { root } = await cdp.send('DOM.getDocument');
  const { nodeIds } = await cdp.send('DOM.querySelectorAll', { nodeId: root.nodeId, selector: 'body *' });
  const visible = await page.evaluate(() => [...document.querySelectorAll('body *')].flatMap((element, index) => {
    if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(element.tagName) || !element.checkVisibility({ checkVisibilityCSS: true, checkOpacity: true })) return [];
    const text = [...element.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim();
    const control = element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement;
    return text || control ? [{ index, tag: element.tagName, text: text.slice(0, 80) }] : [];
  }));
  const usage = [];
  for (let i = 0; i < visible.length; i += 20) usage.push(...await Promise.all(visible.slice(i, i + 20).map(async element => {
    const result = await cdp.send('CSS.getPlatformFontsForNode', { nodeId: nodeIds[element.index] });
    return { ...element, fonts: result.fonts.filter(font => font.glyphCount) };
  })));
  await cdp.detach();
  return { families: [...new Set(usage.flatMap(item => item.fonts.map(font => font.familyName)))], fallbacks: usage.filter(item => item.fonts.some(font => !font.isCustomFont)), checked: usage.length };
}

async function snapshot(opened, name, device) {
  const { page, errors, blocked } = opened;
  const dimensions = await page.evaluate(() => ({ viewport: innerWidth, width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight }));
  const axe = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
  const violations = axe.violations.map(({ id, impact, nodes }) => ({ id, impact, targets: nodes.map(node => ({ target: node.target, failureSummary: node.failureSummary })) }));
  const usedFonts = await fonts(page);
  const cspViolations = await page.evaluate(() => window.__round3Violations);
  check(`${name}/${device}: no overflow`, dimensions.width <= dimensions.viewport, dimensions);
  check(`${name}/${device}: wcag aa`, !violations.length, { violations });
  check(`${name}/${device}: all visible text self-hosted`, usedFonts.checked > 0 && !usedFonts.fallbacks.length, usedFonts);
  check(`${name}/${device}: no browser or CSP errors or external requests`, !errors.length && !blocked.length && !cspViolations.length, { errors, blocked, cspViolations });
  const visibleText = await page.locator('body').innerText();
  check(`${name}/${device}: no internal codes visible`, !/\b(?:INT-\d+|SEL-\d+|R1|EXAMPLE-1)\b/.test(visibleText));
  await page.evaluate(() => scrollTo(0, 0));
  const filename = `${name}-${device}.png`;
  await page.screenshot({ path: resolve(output, filename), fullPage: true, animations: 'disabled', caret: 'hide' });
  await page.screenshot({ path: resolve(output, `${name}-${device}-viewport.png`), fullPage: false, animations: 'disabled', caret: 'hide' });
  report.screenshots.push({ name, device, file: filename, ...dimensions, fonts: usedFonts.families, axeViolations: violations.length });
  console.log(`${name}/${device}: ${dimensions.width} × ${dimensions.height}; axe=${violations.length}; fallback=${usedFonts.fallbacks.length}`);
}

// The private link: shown whole, at the page's own address, with its copy button, inside the
// page; and once the run has ended, when its result is deleted.
async function linkChecks(page, state, device, viewport, run) {
  const card = page.locator('.private-link'), kept = page.locator('.kept-until');
  const box = await card.count() === 1 ? await card.boundingBox() : null;
  check(`${state}/${device}: the private link shows whole, with its copy button, inside the page`,
    !!box && box.x >= 0 && box.x + box.width <= viewport.width
      && await page.locator('#private-link').inputValue() === `${new URL(page.url()).origin}${run.link}`
      && await page.locator('[data-action="copy-link"]').isVisible());
  if (run.purge_at) check(`${state}/${device}: the page says when the result is deleted`,
    await kept.count() === 1 && /^Your result is kept until .+, then deleted\./.test(await kept.innerText()));
}

async function reportChecks(state, device, viewport) {
  const opened = await openPage(state, viewport), { page, redraw, context } = opened;
  const documentation = state === 'documentation', refusal = state === 'refusal';
  const name = documentation ? 'finished-score' : refusal ? 'finished-refusal' : 'contract-fixture/finished-score';
  const reportElement = page.locator('article.report');
  const details = page.locator('#full-report-details');
  const toggle = page.locator('#full-report-toggle');
  await toggle.waitFor();
  check(`${state}/${device}: full report begins closed`, !await details.evaluate(element => element.open));
  check(`${state}/${device}: disclosure uses requested plain wording and arrow`, (await toggle.innerText()).trim() === 'Show full details' && await toggle.locator('svg').count() === 1);
  if (!refusal) {
    const delivered = documentation ? documentedDelivery : round3Runs.score.report.bundle.report.delivered;
    const expectedHeadline = documentation ? 'Condition X: your genetic score' : 'Example request label: your genetic score';
    check(`${state}/${device}: explicit trait is the main headline`, await page.locator('h1').innerText() === expectedHeadline);
    const headlineStyle = await page.locator('h1').evaluate(element => ({ family: getComputedStyle(element).fontFamily, size: parseFloat(getComputedStyle(element).fontSize) }));
    check(`${state}/${device}: headline uses large display face`, headlineStyle.family.includes('Newsreader') && headlineStyle.size >= 32, headlineStyle);
    const visible = await reportElement.innerText();
    check(`${state}/${device}: summary is first and verbatim`, Object.values(delivered.summary_parts).every(part => visible.includes(part))
      && visible.indexOf(delivered.summary_parts.trait) < visible.indexOf(delivered.summary_parts.where)
      && visible.indexOf(delivered.summary_parts.where) < visible.indexOf(delivered.summary_parts.comparison)
      && visible.indexOf(delivered.summary_parts.comparison) < visible.indexOf(delivered.summary_parts.not_a_chance));
    check(`${state}/${device}: z-score and personal limit are in closed details`, !/z-score/i.test(visible) && !visible.includes(delivered.personalized_limit));
    if (documentation) check(`${state}/${device}: no invented group figures`, await page.locator('.comparison-person').count() === 0 && await page.locator('.reference-chart-unavailable').count() === 1);
    else {
      check(`${state}/${device}: original group is unchanged`, await page.locator('.comparison-person').count() === 4 && await page.locator('.comparison-lower').count() === 2 && await page.locator('.comparison-higher').count() === 1);
      const style = await page.locator('.comparison-higher .comparison-person').evaluate(element => ({ color: getComputedStyle(element).color, fill: getComputedStyle(element).fill, width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height }));
      check(`${state}/${device}: higher group and figure sizes unchanged`, style.color === 'rgb(138, 142, 136)' && style.fill === 'none' && style.width === (viewport.width >= 1000 ? 22 : 14) && style.height === (viewport.width >= 1000 ? 34 : 22), style);
    }
  } else {
    const envelope = round3Runs.refusal.report.bundle.report.refusal_envelope;
    const visible = await reportElement.innerText();
    check(`${state}/${device}: refusal headline and detail remain up front`, visible.includes(envelope.headline) && visible.includes(envelope.detail));
    check(`${state}/${device}: longer refusal explanation begins hidden`, !visible.includes(envelope.what_was_tried) && !visible.includes(envelope.why_this_is_honest) && !visible.includes(envelope.what_would_change_it));
  }
  if (!documentation) await linkChecks(page, state, device, viewport, refusal ? round3Runs.refusal : round3Runs.score);
  await snapshot(opened, name, device);
  await toggle.focus(); await page.keyboard.press('Enter');
  check(`${state}/${device}: full report opens by keyboard`, await details.evaluate(element => element.open));
  if (!documentation) {
    const before = await toggle.elementHandle();
    await redraw();
    check(`${state}/${device}: report disclosure and focus survive redraw`, !await before.evaluate(element => element.isConnected)
      && await details.evaluate(element => element.open) && await page.evaluate(() => document.activeElement?.id === 'full-report-toggle'));
    await before.dispose();
  }
  if (!refusal) {
    const delivered = documentation ? documentedDelivery : round3Runs.score.report.bundle.report.delivered;
    const visible = await details.innerText();
    check(`${state}/${device}: every delivered limit is preserved`, delivered.limits.every(limit => visible.includes(limit)) && visible.includes(delivered.personalized_limit) && visible.includes(delivered.disclaimer));
    const zLabel = details.locator('dt').filter({ hasText: /z-score/i });
    check(`${state}/${device}: displayed z-score is delivered value`, await zLabel.count() === 1 && await zLabel.evaluate(element => element.nextElementSibling.textContent) === String(delivered.placement.z_score));
    check(`${state}/${device}: z-score has a plain position-not-risk explanation`, /(?:above|below|average)/i.test(visible) && /not (?:a |your )?risk/i.test(visible));
    check(`${state}/${device}: coded raw warnings are not the report limits`, !visible.includes('Example adapter warning.') && !visible.includes('Example interpretation caveat.'));
  } else {
    const envelope = round3Runs.refusal.report.bundle.report.refusal_envelope;
    const visible = await details.innerText();
    check(`${state}/${device}: all refusal details are preserved`, [envelope.what_was_tried, envelope.why_this_is_honest, envelope.what_would_change_it].every(value => visible.includes(value)));
  }
  await snapshot(opened, `${name}-details`, device);
  await toggle.focus(); await page.keyboard.press('Space');
  check(`${state}/${device}: full report closes by keyboard`, !await details.evaluate(element => element.open));
  await context.close();
}

async function questionChecks(state, device, viewport) {
  const opened = await openPage(state, viewport), { page, redraw, posts, context } = opened;
  const question = round3Runs[state].question;
  const estimate = round3Runs[state].estimate;
  // The caption names the kind of file the estimate was measured on, as the worker reports it.
  const kind = { chip: 'genotype-file ', whole_genome: 'whole-genome ' }[round3Runs[state].kind] || '';
  const expectedCaption = `An estimate from recent ${kind}runs, which took ${estimate.low_minutes}–${estimate.high_minutes} minutes. Questions can add time.`;
  const choices = page.locator('input[name="answer-choice"]');
  const options = question.options || [];
  check(`${state}/${device}: exact question and reason`, await page.locator('#live-question').innerText() === documentedQuestion.text
    && await page.locator('.question-why').innerText() === documentedQuestion.why_it_matters);
  check(`${state}/${device}: exact choices and conditional text box`, await choices.count() === options.length + (question.other === true ? 1 : 0)
    && await page.locator('#answer').count() === (!options.length || question.other === true ? 1 : 0));
  check(`${state}/${device}: no answer is sent or preselected`, await page.locator('#answer-submit').isDisabled() && !posts.length && await page.locator('input[name="answer-choice"]:checked').count() === 0);
  check(`${state}/${device}: question title still alerts`, (await page.title()).includes('Answer needed'));
  check(`${state}/${device}: card uses the supplied measured estimate`, await page.locator('.time-caption').innerText() === expectedCaption);
  await linkChecks(page, state, device, viewport, round3Runs[state]);
  await snapshot(opened, state === 'question' ? 'waiting-answer' : state, device);
  if (options.length) {
    const first = page.locator('#answer-choice-0');
    await first.focus(); await page.keyboard.press('Space');
    check(`${state}/${device}: keyboard selects offered choice`, await first.isChecked() && !await page.locator('#answer-submit').isDisabled());
    const old = await first.elementHandle();
    await redraw();
    check(`${state}/${device}: choice and focus survive redraw`, !await old.evaluate(element => element.isConnected) && await first.isChecked()
      && await page.evaluate(() => document.activeElement?.id === 'answer-choice-0'));
    await old.dispose();
    await snapshot(opened, `${state}-selected`, device);
  }
  const ownWords = 'Please explain the model before I decide.';
  if (question.other === true || !options.length) {
    const answer = page.locator('#answer');
    await answer.fill(ownWords);
    await answer.evaluate(element => element.setSelectionRange(7, 19));
    check(`${state}/${device}: typing chooses Other only when offered`, question.other !== true || await page.locator('#answer-choice-other').isChecked());
    const old = await answer.elementHandle();
    await redraw();
    check(`${state}/${device}: typed draft and selection survive redraw`, !await old.evaluate(element => element.isConnected)
      && await answer.inputValue() === ownWords && await answer.evaluate(element => document.activeElement === element && element.selectionStart === 7 && element.selectionEnd === 19));
    await old.dispose();
    await snapshot(opened, question.other === true ? 'waiting-answer-other' : 'question-freeform-filled', device);
    // Return to an option after typing: the exact option must win over old text.
    if (question.other === true) { await page.locator('#answer-choice-1').focus(); await page.keyboard.press('Space'); }
  }
  check(`${state}/${device}: redraws preserve the measured estimate`, await page.locator('.time-caption').innerText() === expectedCaption);
  const expected = options.length ? options[question.other === true ? 1 : 0] : ownWords;
  await page.locator('#answer-submit').click();
  await page.locator('#answer-form').waitFor({ state: 'hidden' });
  check(`${state}/${device}: exact answer transport`, posts.length === 1 && JSON.stringify(posts[0].body) === JSON.stringify({ question_id: question.id, text: expected })
    && posts[0].csrf === sessionFixture.csrf, { posts, expected });
  check(`${state}/${device}: reply confirmation remains visible`, await page.locator('.notice-banner').innerText().then(text => text.includes('Your answer was sent.')));
  await context.close();
  if (question.other === true) {
    // Exercise the other permitted transport separately: free text must reach
    // the worker unchanged when the question explicitly offers that route.
    const other = await openPage(state, viewport);
    await other.page.locator('#answer').fill(ownWords);
    await other.page.locator('#answer-submit').click();
    await other.page.locator('#answer-form').waitFor({ state: 'hidden' });
    check(`${state}/${device}: Other sends the person's own words`, other.posts.length === 1
      && JSON.stringify(other.posts[0].body) === JSON.stringify({ question_id: question.id, text: ownWords }), { posts: other.posts });
    await other.context.close();
  }
}

try {
  for (const device of devices) {
    const viewport = layouts[device];
    if (!viewport) throw new Error(`Unknown review viewport: ${device}`);
    for (const state of states) {
      if (['documentation', 'score', 'refusal'].includes(state)) await reportChecks(state, device, viewport);
      else if (['question', 'question-options-only', 'question-freeform'].includes(state)) await questionChecks(state, device, viewport);
      else throw new Error(`Unknown review state: ${state}`);
    }
  }
} catch (error) {
  check('browser checks complete', false, { error: error.stack || String(error) });
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
  report.summary = { total: report.checks.length, passed: report.checks.filter(entry => entry.passed).length, failed: report.failures.length, screenshots: report.screenshots.length };
  await writeFile(resolve(output, 'checks.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report.summary));
  if (report.failures.length) process.exitCode = 1;
}
