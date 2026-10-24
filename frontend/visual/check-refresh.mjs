// Product-entry browser checks. Every API reply uses an existing visual fixture.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/check-refresh.mjs
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { startPreviewServer } from './server.mjs';
import { FIXED_NOW, VIEWPORTS, runFixtures, sessionFixture } from './fixtures.mjs';

const toolRoot = process.env.UI_REVIEW_TOOLS || '/tmp/ui-refresh-tools/node_modules';
const { chromium } = await import(pathToFileURL(`${toolRoot}/playwright/index.mjs`).href);
const { default: AxeBuilder } = await import(pathToFileURL(`${toolRoot}/@axe-core/playwright/dist/index.mjs`).href);
const output = process.env.UI_REVIEW_CHECKS || '/root/reviews/ui-shots/after/checks.json';
const states = process.env.UI_REVIEW_STATES?.split(',') || Object.keys(runFixtures);
// Read the exact current production boundary; do not relax it for the preview.
const webSource = await readFile(new URL('../../demo_site/web.py', import.meta.url), 'utf8');
const csp = webSource.match(/self\.send_header\("Content-Security-Policy", "([^"]+)"\)/)?.[1];
if (!csp) throw new Error('Could not read the production Content-Security-Policy.');
const assetNames = [...webSource.matchAll(/^(?:PUBLIC|PRIVATE) = \{([^\n]+)\}/gm)]
  .flatMap(match => [...match[1].matchAll(/"([^"]+)"/g)].map(name => `/${name[1]}`));
const allowedAssets = new Set(assetNames);
const { server, baseURL } = await startPreviewServer();
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const report = {
  syntheticTestFixturesOnly: true, noAnalysisPerformed: true,
  checkedAt: new Date().toISOString(), browser: browser.version(),
  productionContentSecurityPolicy: csp, productionStaticAllowlist: [...allowedAssets],
  standards: ['wcag2a', 'wcag2aa', 'wcag21aa'],
  accessibility: [], interactions: [], motion: [], failures: [],
};

function check(group, name, condition, detail = {}) {
  const result = { name, passed: Boolean(condition), ...detail };
  group.push(result);
  if (!condition) { report.failures.push(result); console.log(`FAIL ${name}: ${JSON.stringify(detail)}`); }
  return condition;
}

async function instrument(page, permissionResult) {
  await page.addInitScript(({ permissionResult }) => {
    window.__visualChecks = {
      permissionRequests: 0, notifications: [], tones: 0, audioResumes: 0,
      hidden: false, animationFrames: 0, securityViolations: [],
    };
    class TestNotification {
      static permission = 'default';
      static async requestPermission() {
        window.__visualChecks.permissionRequests++;
        TestNotification.permission = permissionResult;
        return permissionResult;
      }
      constructor(title, options) { window.__visualChecks.notifications.push({ title, ...options }); }
      close() {}
      addEventListener() {}
    }
    class TestAudioContext {
      state = 'suspended'; currentTime = 0; destination = {};
      async resume() { this.state = 'running'; window.__visualChecks.audioResumes++; }
      async close() { this.state = 'closed'; }
      createOscillator() { return { type: '', frequency: { setValueAtTime() {} }, connect() {}, start() { window.__visualChecks.tones++; }, stop() {} }; }
      createGain() { return { gain: { setValueAtTime() {}, linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {} }, connect() {} }; }
    }
    Object.defineProperty(window, 'Notification', { configurable: true, value: TestNotification });
    Object.defineProperty(window, 'AudioContext', { configurable: true, value: TestAudioContext });
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.__visualChecks.hidden });
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => window.__visualChecks.hidden ? 'hidden' : 'visible' });
    const requestAnimationFrame = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = callback => {
      window.__visualChecks.animationFrames++;
      return requestAnimationFrame(callback);
    };
    document.addEventListener('securitypolicyviolation', event => {
      window.__visualChecks.securityViolations.push({ directive: event.violatedDirective, blockedURI: event.blockedURI });
    });
  }, { permissionResult });
  await page.clock.install({ time: FIXED_NOW });
}

async function openPage(state, viewport, { permission = 'granted', reducedMotion = 'reduce' } = {}) {
  const context = await browser.newContext({
    viewport, deviceScaleFactor: 1, locale: 'en-US', timezoneId: 'UTC', reducedMotion,
    colorScheme: 'light', isMobile: viewport.width < 600, hasTouch: viewport.width < 600,
  });
  const page = await context.newPage();
  const errors = [];
  const unexpectedRequests = [];
  const mutations = [];
  let currentState = state;
  let reads = 0;
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => {
    if (message.type() === 'error' && !(currentState === 'offline' && message.text().includes('ERR_INTERNET_DISCONNECTED'))) errors.push(message.text());
  });
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== baseURL) {
      unexpectedRequests.push(url.href); return route.abort();
    }
    if (url.pathname.startsWith('/api/')) {
      if (request.method() !== 'GET') {
        mutations.push({ method: request.method(), path: url.pathname });
        return route.fulfill({ status: 405, json: { error: 'Visual checks do not perform mutations.' } });
      }
      if (url.pathname === '/api/session') return route.fulfill({ json: { ...sessionFixture, authenticated: currentState !== 'sign-in' } });
      if (url.pathname === '/api/run') {
        if (currentState === 'offline' && reads++ > 0) return route.abort('internetdisconnected');
        return route.fulfill({ json: runFixtures[currentState] });
      }
      unexpectedRequests.push(url.href); return route.abort();
    }
    if (request.isNavigationRequest()) {
      if (!['/login.html', '/demo.html'].includes(url.pathname)) {
        unexpectedRequests.push(url.href); return route.abort();
      }
      const response = await route.fetch();
      return route.fulfill({ response, headers: { ...response.headers(), 'content-security-policy': csp } });
    }
    if (!allowedAssets.has(url.pathname) || url.search) {
      unexpectedRequests.push(url.href); return route.abort();
    }
    return route.continue();
  });
  await instrument(page, permission);
  await page.goto(`${baseURL}/${state === 'sign-in' ? 'login' : 'demo'}.html`, { waitUntil: 'networkidle' });
  await page.locator('h1').first().waitFor();
  if (state === 'sign-in') await page.locator('#password:not([disabled])').waitFor();
  if (state === 'offline') {
    await page.clock.runFor(2100);
    await page.locator('[role="alert"]').first().waitFor();
  }
  await page.evaluate(() => document.fonts.ready);
  const transition = async next => {
    if (!(next in runFixtures)) throw new Error(`Unknown existing fixture: ${next}`);
    currentState = next;
    await page.clock.runFor(2100);
    // Poll fetch is asynchronous; wait for the actual changed content below.
    if (next === 'waiting-answer') await page.locator('#answer-form').waitFor();
    if (next === 'finished-score') await page.locator('.report-score .result-title').waitFor();
  };
  return { context, page, errors, unexpectedRequests, mutations, transition };
}

async function actualFonts(page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('DOM.enable');
  await cdp.send('CSS.enable');
  const { root } = await cdp.send('DOM.getDocument');
  const { nodeIds } = await cdp.send('DOM.querySelectorAll', { nodeId: root.nodeId, selector: 'body *' });
  const textElements = await page.evaluate(() => [...document.querySelectorAll('body *')].flatMap((element, index) => {
    if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(element.tagName) || !element.checkVisibility({ checkVisibilityCSS: true, checkOpacity: true })) return [];
    const ownText = [...element.childNodes].filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join('').trim();
    const pseudoText = ['::before', '::after'].some(pseudo => !['none', 'normal', '""'].includes(getComputedStyle(element, pseudo).content));
    const controlText = element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement;
    return ownText || pseudoText || controlText ? [{ index, element: `${element.tagName.toLowerCase()}${element.id ? `#${element.id}` : ''}${typeof element.className === 'string' && element.className ? `.${element.className.trim().replace(/\s+/g, '.')}` : ''}`, text: ownText.slice(0, 100) }] : [];
  }));
  const usage = [];
  for (let offset = 0; offset < textElements.length; offset += 16) {
    const batch = await Promise.all(textElements.slice(offset, offset + 16).map(async item => {
      const { fonts } = await cdp.send('CSS.getPlatformFontsForNode', { nodeId: nodeIds[item.index] });
      return { ...item, fonts: fonts.filter(font => font.glyphCount > 0) };
    }));
    usage.push(...batch);
  }
  const faces = await page.evaluate(() => [...document.fonts].map(face => ({ family: face.family, weight: face.weight, style: face.style, status: face.status })));
  const families = [...new Set(usage.flatMap(item => item.fonts.map(font => font.familyName)))];
  const fallbacks = usage.filter(item => item.fonts.some(font => !font.isCustomFont));
  await cdp.detach();
  return { faces, families, fallbacks, elementsChecked: usage.length, renderedGlyphs: usage.reduce((total, item) => total + item.fonts.reduce((sum, font) => sum + font.glyphCount, 0), 0) };
}

async function animationSnapshot(page) {
  return page.evaluate(() => document.getAnimations().map(animation => ({
    name: animation.animationName || '', playState: animation.playState,
    duration: animation.effect.getTiming().duration,
    target: animation.effect.target?.className?.baseVal ?? animation.effect.target?.className ?? '',
    helix: Boolean(animation.effect.target?.closest('[data-helix], .dna-helix')),
  })));
}

async function helixSnapshot(page) {
  return page.locator('.dna-helix').evaluate(root => {
    const canvas = root.querySelector('canvas');
    const rectangle = canvas?.getBoundingClientRect();
    return {
      state: root.dataset.helixState, period: Number(root.dataset.period),
      frame: Number(canvas?.dataset.frame), phase: Number(canvas?.dataset.phase),
      transform: canvas ? getComputedStyle(canvas).transform : null,
      bounds: rectangle ? { x: rectangle.x, y: rectangle.y, width: rectangle.width, height: rectangle.height } : null,
    };
  });
}

try {
  for (const state of states) {
    for (const [device, viewport] of Object.entries(VIEWPORTS)) {
      const { context, page, errors, unexpectedRequests, mutations } = await openPage(state, viewport);
      const key = `${state}/${device}`;
      const entry = { state, device, checks: [] };
      report.accessibility.push(entry);
      const result = await new AxeBuilder({ page }).withTags(report.standards).analyze();
      entry.violations = result.violations.map(({ id, impact, help, nodes }) => ({ id, impact, help, nodes: nodes.map(({ target, failureSummary }) => ({ target, failureSummary })) }));
      entry.incomplete = result.incomplete.map(({ id, nodes }) => ({ id, targets: nodes.map(node => node.target) }));
      check(entry.checks, `${key}: wcag aa`, entry.violations.length === 0, { violationCount: entry.violations.length });
      if (entry.violations.length) console.log(JSON.stringify({ key, violations: entry.violations }));
      const dimensions = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }));
      check(entry.checks, `${key}: no overflow`, dimensions.content <= dimensions.viewport, dimensions);
      check(entry.checks, `${key}: permission unrequested`, await page.evaluate(() => window.__visualChecks.permissionRequests === 0));
      if (state === 'waiting-answer') check(entry.checks, `${key}: question tab title`, (await page.title()).includes('Answer needed'));
      entry.fonts = await actualFonts(page);
      check(entry.checks, `${key}: all rendered glyphs use self-hosted fonts`, entry.fonts.renderedGlyphs > 0 && entry.fonts.fallbacks.length === 0, { families: entry.fonts.families, fallbacks: entry.fonts.fallbacks });
      check(entry.checks, `${key}: sans and serif both loaded`, entry.fonts.faces.length >= 2 && entry.fonts.faces.every(face => face.status === 'loaded'), { faces: entry.fonts.faces });
      const animations = await animationSnapshot(page);
      const reducedHelix = await page.locator('.dna-helix:visible').count() ? await helixSnapshot(page) : null;
      check(entry.checks, `${key}: reduced motion is still`, !animations.some(animation => animation.playState === 'running' && animation.duration > 1) && (!reducedHelix || reducedHelix.state === 'reduced'), { animations, helix: reducedHelix });
      const summary = page.locator('details > summary').first();
      if (await summary.count()) {
        await summary.focus();
        await page.keyboard.press('Enter');
        check(entry.checks, `${key}: details opens with enter`, await summary.evaluate(element => element.parentElement.open));
        await page.keyboard.press('Space');
        check(entry.checks, `${key}: details closes with space`, await summary.evaluate(element => !element.parentElement.open));
      }
      const skip = page.locator('.skip-link');
      if (await skip.count()) {
        await skip.focus();
        await page.keyboard.press('Enter');
        check(entry.checks, `${key}: skip link moves keyboard focus to main`, await page.evaluate(() => document.activeElement?.id === 'main'));
      }
      if (device === 'phone') {
        await page.setViewportSize({ width: 320, height: 844 });
        const narrow = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }));
        check(entry.checks, `${key}: 320px no overflow`, narrow.content <= narrow.viewport, narrow);
      }
      const securityViolations = await page.evaluate(() => window.__visualChecks.securityViolations);
      check(entry.checks, `${key}: production csp respected`, securityViolations.length === 0, { securityViolations });
      check(entry.checks, `${key}: no browser errors`, errors.length === 0, { errors });
      check(entry.checks, `${key}: only existing static endpoints and intercepted api`, unexpectedRequests.length === 0 && mutations.length === 0, { unexpectedRequests, mutations });
      console.log(`${key}: ${entry.violations.length} axe violations; ${entry.fonts.fallbacks.length} fallback-font elements; ${entry.checks.filter(item => !item.passed).length} failed checks`);
      await context.close();
    }
  }
  for (const permission of ['granted', 'denied']) {
    const { context, page, transition, errors, unexpectedRequests, mutations } = await openPage('running', VIEWPORTS.desktop, { permission });
    const entry = { permission, checks: [] };
    report.interactions.push(entry);
    const key = `notifications-${permission}`;
    const alertControl = page.locator('[data-action="alerts"]');
    check(entry.checks, `${key}: no permission on load`, await page.evaluate(() => window.__visualChecks.permissionRequests === 0));
    await alertControl.focus();
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.querySelector('[data-action="alerts"]')?.getAttribute('aria-pressed') === 'true');
    check(entry.checks, `${key}: keyboard opt-in requests once`, await page.evaluate(() => window.__visualChecks.permissionRequests === 1));
    const tonesBeforeQuestion = await page.evaluate(() => window.__visualChecks.tones);
    await page.evaluate(() => { window.__visualChecks.hidden = true; document.dispatchEvent(new Event('visibilitychange')); });
    await transition('waiting-answer');
    check(entry.checks, `${key}: waiting title after transition`, (await page.title()).includes('Answer needed'));
    const first = await page.evaluate(() => structuredClone(window.__visualChecks));
    check(entry.checks, `${key}: hidden question notification`, first.notifications.length === (permission === 'granted' ? 1 : 0), { notifications: first.notifications });
    check(entry.checks, `${key}: question sound`, first.tones === tonesBeforeQuestion + 1, { tonesBeforeQuestion, tonesAfterQuestion: first.tones });
    await transition('waiting-answer');
    const replay = await page.evaluate(() => structuredClone(window.__visualChecks));
    check(entry.checks, `${key}: same question does not repeat alert`, replay.notifications.length === first.notifications.length && replay.tones === first.tones);
    if (await page.locator('#answer').count()) {
      const draft = 'Synthetic keyboard test answer — do not send.';
      await page.locator('#answer').fill(draft);
      await alertControl.click();
      check(entry.checks, `${key}: answer survives disabling alerts`, await page.locator('#answer').inputValue() === draft);
      await alertControl.click();
      await page.waitForFunction(() => document.querySelector('[data-action="alerts"]')?.getAttribute('aria-pressed') === 'true');
      check(entry.checks, `${key}: answer survives enabling alerts`, await page.locator('#answer').inputValue() === draft);
      await page.locator('#answer').focus();
      await page.locator('#answer').evaluate(element => element.setSelectionRange(5, 11));
      await transition('waiting-answer');
      const retained = await page.locator('#answer').evaluate(element => ({ value: element.value, focused: document.activeElement === element, start: element.selectionStart, end: element.selectionEnd }));
      check(entry.checks, `${key}: polling preserves draft focus and selection`, retained.value === draft && retained.focused && retained.start === 5 && retained.end === 11, retained);
    } else {
      const choice = page.locator('input[name="answer-choice"]').first();
      await choice.focus();
      await page.keyboard.press('Space');
      check(entry.checks, `${key}: keyboard selects an offered answer`, await choice.isChecked());
      await alertControl.click();
      check(entry.checks, `${key}: choice survives disabling alerts`, await choice.isChecked());
      await alertControl.click();
      await page.waitForFunction(() => document.querySelector('[data-action="alerts"]')?.getAttribute('aria-pressed') === 'true');
      check(entry.checks, `${key}: choice survives enabling alerts`, await choice.isChecked());
      await choice.focus();
      await transition('waiting-answer');
      const retained = await choice.evaluate(element => ({ checked: element.checked, focused: document.activeElement === element }));
      check(entry.checks, `${key}: polling preserves choice and focus`, retained.checked && retained.focused, retained);
    }
    check(entry.checks, `${key}: existing permission not requested again`, await page.evaluate(() => window.__visualChecks.permissionRequests === 1));
    await transition('finished-score');
    check(entry.checks, `${key}: result clears question title`, !(await page.title()).includes('Answer needed'));
    check(entry.checks, `${key}: no external writes or errors`, errors.length === 0 && unexpectedRequests.length === 0 && mutations.length === 0, { errors, unexpectedRequests, mutations });
    await context.close();
  }
  {
    const { context, page, transition } = await openPage('running', VIEWPORTS.desktop, { reducedMotion: 'no-preference' });
    const entry = { checks: [] };
    report.motion.push(entry);
    await page.waitForTimeout(700);
    const running = await helixSnapshot(page);
    check(entry.checks, 'helix: ordinary motion rotates projected canvas', running.state === 'running' && running.frame > 0 && running.period === 10000, { helix: running });
    const performance = await context.newCDPSession(page);
    await performance.send('Performance.enable');
    const metricsBefore = Object.fromEntries((await performance.send('Performance.getMetrics')).metrics.map(metric => [metric.name, metric.value]));
    const sampleBefore = await helixSnapshot(page);
    await page.waitForTimeout(2000);
    const sampleAfter = await helixSnapshot(page);
    const metricsAfter = Object.fromEntries((await performance.send('Performance.getMetrics')).metrics.map(metric => [metric.name, metric.value]));
    entry.foregroundPerformanceSample = {
      note: 'A short local sample, not a guarantee of CPU usage on every device. Canvas draws are capped at 30 frames per second.',
      elapsedSeconds: metricsAfter.Timestamp - metricsBefore.Timestamp,
      taskSeconds: metricsAfter.TaskDuration - metricsBefore.TaskDuration,
      scriptSeconds: metricsAfter.ScriptDuration - metricsBefore.ScriptDuration,
      layoutSeconds: metricsAfter.LayoutDuration - metricsBefore.LayoutDuration,
      styleSeconds: metricsAfter.RecalcStyleDuration - metricsBefore.RecalcStyleDuration,
      nodesBefore: metricsBefore.Nodes, nodesAfter: metricsAfter.Nodes,
      canvasFrames: sampleAfter.frame - sampleBefore.frame,
    };
    await performance.detach();
    check(entry.checks, 'helix: capped at 30 drawn frames per second', sampleAfter.frame > sampleBefore.frame && sampleAfter.frame - sampleBefore.frame <= Math.ceil(entry.foregroundPerformanceSample.elapsedSeconds * 30) + 1, { before: sampleBefore, after: sampleAfter, seconds: entry.foregroundPerformanceSample.elapsedSeconds });
    check(entry.checks, 'helix: rotates without translating its canvas', sampleAfter.phase !== sampleBefore.phase && JSON.stringify(sampleBefore.bounds) === JSON.stringify(sampleAfter.bounds) && sampleAfter.transform === sampleBefore.transform, { before: sampleBefore, after: sampleAfter });
    await page.evaluate(() => { window.__visualChecks.hidden = true; document.dispatchEvent(new Event('visibilitychange')); });
    const hiddenBefore = await helixSnapshot(page);
    const hiddenRafBefore = await page.evaluate(() => window.__visualChecks.animationFrames);
    await page.waitForTimeout(400);
    const hiddenAfter = await helixSnapshot(page);
    const hiddenRafAfter = await page.evaluate(() => window.__visualChecks.animationFrames);
    check(entry.checks, 'helix: hidden tab pauses all animation work', hiddenAfter.state === 'hidden' && hiddenBefore.frame === hiddenAfter.frame && hiddenRafBefore === hiddenRafAfter, { before: hiddenBefore, after: hiddenAfter, hiddenRafBefore, hiddenRafAfter });
    await page.evaluate(() => { window.__visualChecks.hidden = false; document.dispatchEvent(new Event('visibilitychange')); });
    await transition('waiting-answer');
    const waiting = await helixSnapshot(page);
    check(entry.checks, 'helix: waiting is calmer', waiting.state === 'waiting' && waiting.period > running.period, { helix: waiting });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.waitForTimeout(100);
    const reducedBefore = await helixSnapshot(page);
    const reducedRafBefore = await page.evaluate(() => window.__visualChecks.animationFrames);
    await page.waitForTimeout(400);
    const reducedAfter = await helixSnapshot(page);
    const reducedRafAfter = await page.evaluate(() => window.__visualChecks.animationFrames);
    const reduced = await animationSnapshot(page);
    check(entry.checks, 'helix: live reduced-motion switch is still', reducedAfter.state === 'reduced' && reducedBefore.frame === reducedAfter.frame && reducedRafBefore === reducedRafAfter && !reduced.some(animation => animation.playState === 'running' && animation.duration > 1), { before: reducedBefore, after: reducedAfter, reducedRafBefore, reducedRafAfter, animations: reduced });
    await context.close();
  }
} catch (error) {
  report.failures.push({ name: 'check script exception', passed: false, message: error.stack });
  console.error(error);
} finally {
  report.passed = report.failures.length === 0;
  await mkdir(resolve(output, '..'), { recursive: true });
  await writeFile(output, JSON.stringify(report, null, 2) + '\n');
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
console.log(`${report.passed ? 'PASS' : 'FAIL'}: ${report.accessibility.length} accessibility scenarios; ${report.interactions.length} notification scenarios; ${report.failures.length} failures.`);
if (!report.passed) process.exitCode = 1;
