// Focused people-chart browser checks. No worker, analysis, or API mutation.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/check-round2.mjs
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { startPreviewServer } from './server.mjs';
import { FIXED_NOW, runFixtures, sessionFixture } from './fixtures.mjs';
import { referenceChart } from '../result-chart.js';
import { getDisplayResult } from '../contracts.js';
import { reportView } from '../workspace.js';

const toolRoot = process.env.UI_REVIEW_TOOLS || '/tmp/ui-refresh-tools/node_modules';
const { chromium } = await import(pathToFileURL(`${toolRoot}/playwright/index.mjs`).href);
const output = resolve(process.env.UI_ROUND2_OUTPUT || '/root/reviews/ui-shots/round2b');
const syntheticOutput = resolve(output, 'synthetic-checks');
const webSource = await readFile(new URL('../../demo_site/web.py', import.meta.url), 'utf8');
const csp = webSource.match(/self\.send_header\("Content-Security-Policy", "([^"]+)"\)/)?.[1];
if (!csp) throw new Error('Could not read the production Content-Security-Policy.');
const allowedAssets = new Set([...webSource.matchAll(/^(?:PUBLIC|PRIVATE) = \{([^\n]+)\}/gm)]
  .flatMap(match => [...match[1].matchAll(/"([^"]+)"/g)].map(name => `/${name[1]}`)));
const fixture = runFixtures['finished-score'];
const bundle = fixture.report.bundle;
const reference = bundle.reference_response.reference;
const rawScores = reference.distribution.raw_scores;
const recordedScore = bundle.score.raw_score;
const deliveredRange = bundle.report.delivered.placement.percentile_range;
const displayedResult = getDisplayResult(bundle);
const summaryKeys = ['trait', 'where', 'comparison', 'not_a_chance'];
const hasSummaryParts = displayedResult.summaryParts
  && summaryKeys.map(key => displayedResult.summaryParts[key]).join(' ') === displayedResult.summary;
const expected = {
  group: rawScores.length,
  lower: rawScores.filter(value => value < recordedScore).length,
  equal: rawScores.filter(value => value === recordedScore).length,
  higher: rawScores.filter(value => value > recordedScore).length,
  range: deliveredRange.join(' to '),
  interpretationRange: bundle.interpret.percentile_range.join('–'),
  summary: displayedResult.summary,
  summarySentence: hasSummaryParts ? displayedResult.summaryParts.where
    : `You scored higher than about ${deliveredRange.join(' to ')} out of every 100 people in your comparison group.`,
};
const layouts = {
  desktop: { width: 1440, height: 1000 },
  phone: { width: 390, height: 844 },
  'narrow-phone': { width: 320, height: 844 },
  // Browser zoom changes the CSS viewport: 1440×1000 at 200% = 720×500.
  // This checks reflow at that layout size, not browser chrome or raster zoom.
  'desktop-200-percent-layout': { width: 720, height: 500 },
};
const report = {
  syntheticTestFixturesOnly: true,
  noAnalysisPerformed: true,
  checkedAt: new Date().toISOString(),
  productionContentSecurityPolicy: csp,
  productionStaticAllowlist: [...allowedAssets],
  fixture: 'frontend/visual/fixtures.mjs: finished-score (unchanged)',
  expected,
  zoomMethod: '1440×1000 divided by 2: 720×500 CSS-pixel viewport; tests layout equivalence only.',
  checks: [], failures: [],
  syntheticLayout: {
    label: 'Synthetic layout check. 150 repeated test values; no analysis performed.',
    scores: 'The existing test scores [-1, 0, 1], repeated 50 times.',
    scoreAndRange: 'Original fixture score and reported range are reused unchanged, solely to render the isolated component. No run or report is created.',
    total: 150, figures: 151,
  },
};
function check(name, passed, detail = {}) {
  const entry = { name, passed: Boolean(passed), ...detail };
  report.checks.push(entry);
  if (!passed) { report.failures.push(entry); console.error(`FAIL ${name}: ${JSON.stringify(detail)}`); }
}

await mkdir(output, { recursive: true });
await mkdir(syntheticOutput, { recursive: true });
const { server, baseURL } = await startPreviewServer();
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
report.browser = browser.version();

async function openPage(viewport) {
  const context = await browser.newContext({
    viewport, deviceScaleFactor: 1, locale: 'en-US', timezoneId: 'UTC',
    reducedMotion: 'reduce', colorScheme: 'light', isMobile: viewport.width < 600,
    hasTouch: viewport.width < 600,
  });
  const page = await context.newPage();
  const errors = [], blocked = [], mutations = [];
  let runReads = 0;
  let offline = false, intentionalNetworkFailures = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.addInitScript(() => {
    window.__round2Violations = [];
    document.addEventListener('securitypolicyviolation', event => {
      window.__round2Violations.push({ directive: event.violatedDirective, blockedURI: event.blockedURI });
    });
  });
  await page.clock.install({ time: FIXED_NOW });
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== baseURL) { blocked.push(url.href); return route.abort(); }
    if (url.pathname.startsWith('/api/')) {
      if (request.method() !== 'GET') {
        mutations.push({ method: request.method(), path: url.pathname });
        return route.fulfill({ status: 405, json: { error: 'Browser checks do not perform mutations.' } });
      }
      if (url.pathname === '/api/session') return route.fulfill({ json: sessionFixture });
      if (url.pathname === '/api/run') {
        runReads++;
        if (offline) { intentionalNetworkFailures++; return route.abort('internetdisconnected'); }
        return route.fulfill({ json: fixture });
      }
      blocked.push(url.href); return route.abort();
    }
    if (request.isNavigationRequest() && url.pathname === '/demo.html') {
      const response = await route.fetch();
      return route.fulfill({ response, headers: { ...response.headers(), 'content-security-policy': csp } });
    }
    if (!allowedAssets.has(url.pathname) || url.search) { blocked.push(url.href); return route.abort(); }
    return route.continue();
  });
  await page.goto(`${baseURL}/demo.html`, { waitUntil: 'networkidle' });
  await page.locator('.reference-chart[data-chart-kind="people"]').waitFor();
  await page.evaluate(() => document.fonts.ready);
  return { context, page, errors, blocked, mutations, reads: () => runReads,
    setOffline: value => { offline = value; }, networkFailures: () => intentionalNetworkFailures };
}

async function dimensions(page) {
  return page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }));
}

try {
  for (const [name, viewport] of Object.entries(layouts)) {
    const { context, page, errors, blocked, mutations, reads, setOffline, networkFailures } = await openPage(viewport);
    const chart = page.locator('.reference-chart');
    const people = chart.locator('.comparison-person');
    check(`${name}: one figure per comparison person plus a separate person`, await people.count() === expected.group + 1,
      { expected: expected.group + 1, actual: await people.count() });
    check(`${name}: highlighted person has a visible label`, await chart.locator('.comparison-you .comparison-person').count() === 1
      && await chart.locator('.comparison-you small').isVisible()
      && await chart.locator('.comparison-you small').innerText() === 'You');
    const grid = chart.locator('.comparison-grid');
    const relations = await grid.locator(':scope > .comparison-cell').evaluateAll(elements => elements.map(element =>
      ['lower', 'you', 'equal', 'higher'].find(relation => element.classList.contains(`comparison-${relation}`))));
    const expectedRelations = [
      ...Array(expected.lower).fill('lower'), 'you', ...Array(expected.equal).fill('equal'), ...Array(expected.higher).fill('higher'),
    ];
    check(`${name}: one compact grid keeps exact group counts and order`, await grid.count() === 1
      && JSON.stringify(relations) === JSON.stringify(expectedRelations), { relations, expectedRelations });
    check(`${name}: grid cells contain one person each`, await grid.locator(':scope > *').evaluateAll(elements =>
      elements.every(element => element.classList.contains('comparison-cell') && element.querySelectorAll('svg.comparison-person').length === 1)));
    const colors = await chart.evaluate(element => ({
      user: getComputedStyle(element.querySelector('.comparison-you .comparison-person')).color,
      userBackground: getComputedStyle(element.querySelector('.comparison-you')).backgroundColor,
      lower: getComputedStyle(element.querySelector('.comparison-lower .comparison-person')).color,
      higher: getComputedStyle(element.querySelector('.comparison-higher .comparison-person')).color,
      lowerFill: getComputedStyle(element.querySelector('.comparison-lower .comparison-person')).fill,
      higherFill: getComputedStyle(element.querySelector('.comparison-higher .comparison-person')).fill,
      userBorder: getComputedStyle(element.querySelector('.comparison-you')).borderTopStyle,
      userBorderWidth: getComputedStyle(element.querySelector('.comparison-you')).borderTopWidth,
      accent: getComputedStyle(document.documentElement).getPropertyValue('--blue').trim(),
      paper: getComputedStyle(document.documentElement).getPropertyValue('--paper').trim(),
    }));
    const cssColor = value => /^#[0-9a-f]{6}$/i.test(value)
      ? `rgb(${value.slice(1).match(/../g).map(channel => parseInt(channel, 16)).join(', ')})` : value;
    check(`${name}: person uses distinct accent color`, colors.user !== colors.lower && colors.user !== colors.higher
      && colors.userBackground === cssColor(colors.accent) && colors.user === cssColor(colors.paper), colors);
    check(`${name}: lower and higher groups are visually distinct`, colors.lower !== colors.higher, colors);
    check(`${name}: figures remain distinct without color`, colors.lowerFill !== 'none' && colors.higherFill === 'none'
      && colors.userBorder !== 'none' && parseFloat(colors.userBorderWidth) > 0, colors);
    const caption = await chart.locator('figcaption').innerText();
    check(`${name}: caption uses recorded lower count`, caption.includes(`Out of ${expected.group} people in your comparison group,`)
      && caption.includes(`${expected.lower} scored lower than you.`), { caption });
    const text = await chart.locator('.comparison-text').innerText();
    check(`${name}: text version includes equal and higher counts`, text.includes(`${expected.equal} scored the same.`)
      && text.includes(`${expected.higher} scored higher.`) && text.includes('Each figure is one person')
      && text.includes('blue figure marks you and is not part of the group'), { text });
    check(`${name}: visual figures hidden from screen readers`, await people.evaluateAll(elements =>
      elements.every(element => Boolean(element.closest('[aria-hidden="true"]')))));
    const accessible = await chart.ariaSnapshot();
    check(`${name}: accessible text states counts without individual SVG announcements`, accessible.includes(`${expected.lower} scored lower than you`)
      && accessible.includes(`${expected.higher} scored higher`) && !accessible.includes('- img'), { accessible });
    const resultText = (await page.locator('.result-sentence').innerText()).replace(/\s+/g, ' ').trim();
    const summaryText = (await page.locator('.report-summary').innerText()).replace(/\s+/g, ' ').trim();
    check(`${name}: recorded summary and placement sentence stay visible unchanged`,
      summaryText === expected.summary && resultText === expected.summarySentence
      && await page.locator('.report-summary').isVisible() && await page.locator('.result-sentence').isVisible(),
      { summaryText, resultText });
    check(`${name}: uncertainty note remains visible`, await chart.locator('.reference-chart-note').isVisible()
      && await chart.locator('.reference-chart-note').innerText().then(text => text.includes('does not cover every source of uncertainty')));
    let size = await dimensions(page);
    check(`${name}: no horizontal overflow`, size.content <= size.viewport, size);
    const disclosure = chart.locator('#comparison-values');
    const summary = disclosure.locator('summary');
    await summary.focus();
    await page.keyboard.press('Enter');
    check(`${name}: details opens with keyboard`, await disclosure.evaluate(element => element.open));
    const beforeReads = reads();
    await page.clock.runFor(2200);
    await page.waitForFunction(() => document.querySelector('#comparison-values')?.open === true);
    check(`${name}: details survives polling`, reads() > beforeReads && await disclosure.evaluate(element => element.open), { beforeReads, afterReads: reads() });
    check(`${name}: exact interpretation range retained inside comparison details`, await disclosure.innerText().then(text => text.includes(expected.interpretationRange)));
    size = await dimensions(page);
    check(`${name}: expanded details has no overflow`, size.content <= size.viewport, size);

    // A connection error changes the page HTML, forcing a real DOM replacement.
    // Only the intercepted GET fails; the recorded report fixture is unchanged.
    await summary.focus();
    const beforeErrorSummary = await summary.elementHandle();
    setOffline(true);
    await page.clock.runFor(2200);
    await page.locator('.connection-banner').waitFor({ state: 'visible' });
    check(`${name}: poll error forces a rerender`, !await beforeErrorSummary.evaluate(element => element.isConnected)
      && networkFailures() === 1, { intentionalNetworkFailures: networkFailures() });
    check(`${name}: open details and summary focus survive poll error`, await disclosure.evaluate(element => element.open)
      && await page.evaluate(() => document.activeElement?.id === 'comparison-details-toggle'));
    const beforeRecoverySummary = await summary.elementHandle();
    setOffline(false);
    await page.clock.runFor(5200);
    await page.locator('.connection-banner').waitFor({ state: 'hidden' });
    check(`${name}: poll recovery forces a rerender`, !await beforeRecoverySummary.evaluate(element => element.isConnected));
    check(`${name}: open details and summary focus survive recovery`, await disclosure.evaluate(element => element.open)
      && await page.evaluate(() => document.activeElement?.id === 'comparison-details-toggle'));
    await beforeErrorSummary.dispose();
    await beforeRecoverySummary.dispose();

    await summary.focus();
    await page.keyboard.press('Space');
    check(`${name}: details closes with keyboard`, await disclosure.evaluate(element => !element.open));
    const violations = await page.evaluate(() => window.__round2Violations);
    check(`${name}: production CSP and asset boundary respected`, !violations.length && !blocked.length && !mutations.length,
      { violations, blocked, mutations });
    const expectedNetworkErrors = errors.filter(error => error.includes('ERR_INTERNET_DISCONNECTED'));
    const unexpectedErrors = errors.filter(error => !error.includes('ERR_INTERNET_DISCONNECTED'));
    check(`${name}: no unexpected browser errors`, unexpectedErrors.length === 0, { errors: unexpectedErrors, expectedNetworkErrors });

    if (name === 'desktop' || name === 'phone' || name === 'narrow-phone') {
      // Also exercise a report without optional summary parts. Its exact recorded
      // decimal bounds must survive the generated plain-language range sentence.
      // These are the existing fixture's interpretation bounds, not new numbers.
      const fallback = structuredClone(fixture.report);
      delete fallback.bundle.report.delivered.summary_parts;
      fallback.bundle.report.delivered.placement.percentile_range = [...bundle.interpret.percentile_range];
      const fallbackRange = fallback.bundle.report.delivered.placement.percentile_range.join(' to ');
      await page.evaluate(markup => {
        document.querySelector('#app').innerHTML = `<main class="workspace-main" id="main">${markup}</main>`;
      }, reportView(fallback));
      const fallbackSentence = await page.locator('.result-sentence').innerText();
      check(`${name}: summary fallback preserves exact recorded bounds in a plain sentence`,
        await page.locator('.result-range').innerText() === fallbackRange
        && fallbackSentence === `You scored higher than about ${fallbackRange} out of every 100 people in your comparison group.`,
        { fallbackSentence, fallbackRange });
      check(`${name}: summary fallback retains the exact full authored summary`,
        await page.locator('.report-summary').innerText() === expected.summary);
      size = await dimensions(page);
      check(`${name}: summary fallback has no overflow`, size.content <= size.viewport, size);
      const fallbackFilename = `synthetic-recorded-range-fallback-${name}.png`;
      await page.screenshot({ path: resolve(syntheticOutput, fallbackFilename), fullPage: true, animations: 'disabled', caret: 'hide' });
      report.summaryFallback ??= { label: 'Synthetic contract fixture without optional summary parts; recorded interpretation bounds are delivered unchanged.', screenshots: [] };
      report.summaryFallback.screenshots.push(`synthetic-checks/${fallbackFilename}`);

      const syntheticScores = Array.from({ length: 50 }, () => rawScores).flat();
      const syntheticReference = { ...reference, n_participants: syntheticScores.length,
        distribution: { ...reference.distribution, n: syntheticScores.length, raw_scores: syntheticScores } };
      const markup = referenceChart({ kind: 'score', score: recordedScore,
        percentileRange: deliveredRange, reference: syntheticReference });
      // Isolated component only. The browser's API fixture is unchanged.
      await page.evaluate(({ markup, label }) => {
        document.querySelector('#app').innerHTML = `<main class="workspace-main" id="main"><h1>Synthetic layout check</h1><p>${label}</p>${markup}</main>`;
      }, { markup, label: report.syntheticLayout.label });
      const synthetic = page.locator('.reference-chart');
      const rowPositions = await synthetic.locator('.comparison-grid > .comparison-cell').evaluateAll(elements =>
        [...new Set(elements.map(element => Math.round(element.getBoundingClientRect().top)))]);
      check(`${name}: synthetic 150-person chart includes every figure`, await synthetic.locator('.comparison-person').count() === 151,
        { expected: 151, actual: await synthetic.locator('.comparison-person').count() });
      check(`${name}: synthetic 150-person figures wrap in one grid`, await synthetic.locator('.comparison-grid').count() === 1
        && await synthetic.locator('.comparison-grid > .comparison-cell').count() === 151
        && rowPositions.length > 1, { rows: rowPositions.length });
      size = await dimensions(page);
      check(`${name}: synthetic 150-person chart has no overflow`, size.content <= size.viewport, size);
      const filename = `synthetic-150-people-${name}.png`;
      await page.screenshot({ path: resolve(syntheticOutput, filename), fullPage: true, animations: 'disabled', caret: 'hide' });
      report.syntheticLayout.screenshots ??= [];
      report.syntheticLayout.screenshots.push(`synthetic-checks/${filename}`);
    }
    await context.close();
  }
} catch (error) {
  check('browser checks complete', false, { error: error.stack || String(error) });
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
  report.summary = { total: report.checks.length, passed: report.checks.filter(entry => entry.passed).length, failed: report.failures.length };
  await writeFile(resolve(output, 'people-checks.json'), JSON.stringify(report, null, 2) + '\n');
  console.log(JSON.stringify(report.summary));
  if (report.failures.length) process.exitCode = 1;
}
