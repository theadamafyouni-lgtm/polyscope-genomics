// Isolated browser checks for the decorative 3D helix. Every API reply is an
// existing test fixture; no analysis, real account, or remote asset is used.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/check-helix.mjs
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { setTimeout as delay } from 'node:timers/promises';
import { startPreviewServer } from './server.mjs';
import { FIXTURE_NOW, VIEWPORTS, runFixtures, sessionFixture } from './fixtures.mjs';

const output = process.env.UI_HELIX_CHECKS || '/root/reviews/ui-shots/helix/checks.json';
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE || '/tmp/ui-refresh-tools/node_modules/playwright/index.mjs').href);
const webSource = await readFile(new URL('../../demo_site/web.py', import.meta.url), 'utf8');
const csp = webSource.match(/self\.send_header\("Content-Security-Policy", "([^"]+)"\)/)?.[1];
if (!csp) throw new Error('Cannot inspect the existing production CSP.');
const allowedAssets = new Set([...webSource.matchAll(/^(?:PUBLIC|PRIVATE) = \{([^\n]+)\}/gm)]
  .flatMap(match => [...match[1].matchAll(/"([^"]+)"/g)].map(name => `/${name[1]}`)));
const { server, baseURL } = await startPreviewServer();
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const report = { fixtureOnly: true, browser: browser.version(), capturedAt: new Date().toISOString(), csp, devices: [], failures: [] };

function check(entry, name, passed, detail = {}) {
  const result = { name, passed: Boolean(passed), ...detail };
  entry.checks.push(result);
  if (!passed) { report.failures.push({ device: entry.device, ...result }); console.log(`FAIL ${entry.device}: ${name}`); }
}

async function snapshot(page) {
  return page.locator('.dna-helix').evaluate(root => {
    const canvas = root.querySelector('canvas');
    const box = canvas.getBoundingClientRect();
    return {
      state: root.dataset.helixState, period: Number(root.dataset.period), backend: root.dataset.helixBackend,
      frame: Number(canvas.dataset.frame), phase: Number(canvas.dataset.phase),
      width: canvas.width, height: canvas.height,
      bounds: [box.x, box.y, box.width, box.height],
      transform: getComputedStyle(canvas).transform,
      shortTimers: window.__helixChecks.shortTimers,
      pendingShortTimers: window.__helixChecks.pending.size,
      raf: window.__helixChecks.raf,
      time: performance.now(),
    };
  });
}
const withoutImage = value => value;
const phaseDelta = (before, after) => (after.phase - before.phase + Math.PI * 2) % (Math.PI * 2);

async function measure(entry, page, label, expectedPeriod) {
  // Preserve the existing page-arrival motion; measure after it settles so its
  // final subpixel movement cannot be mistaken for helix translation.
  await page.evaluate(() => Promise.all(document.getAnimations().filter(animation => animation.effect?.getTiming().iterations !== Infinity).map(animation => animation.finished.catch(() => {}))));
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Performance.enable');
  const pixelsBefore = await page.locator('.dna-helix canvas').screenshot({ animations: 'allow' });
  const before = await snapshot(page);
  const metricsBefore = Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(metric => [metric.name, metric.value]));
  await delay(2100);
  const metricsAfter = Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(metric => [metric.name, metric.value]));
  await cdp.detach();
  const after = await snapshot(page);
  const pixelsAfter = await page.locator('.dna-helix canvas').screenshot({ animations: 'allow' });
  (entry.performanceSamples ||= []).push({ label, note: 'Short local browser sample excludes bitmap-read snapshots; not a guarantee of CPU usage on every device.', elapsedSeconds: metricsAfter.Timestamp - metricsBefore.Timestamp, taskSeconds: metricsAfter.TaskDuration - metricsBefore.TaskDuration, scriptSeconds: metricsAfter.ScriptDuration - metricsBefore.ScriptDuration, layoutSeconds: metricsAfter.LayoutDuration - metricsBefore.LayoutDuration, styleSeconds: metricsAfter.RecalcStyleDuration - metricsBefore.RecalcStyleDuration });
  const elapsed = after.time - before.time;
  const frames = after.frame - before.frame;
  const radians = phaseDelta(before, after);
  const expectedRadians = elapsed / expectedPeriod * Math.PI * 2;
  if (label === 'running') check(entry, 'timer audit observes the active renderer', after.shortTimers > before.shortTimers, { before: before.shortTimers, after: after.shortTimers });
  check(entry, `${label}: rotating at the stated rate`, after.period === expectedPeriod && Math.abs(radians - expectedRadians) < 0.12, { radians, expectedRadians, elapsed, period: after.period });
  check(entry, `${label}: at most 30 canvas frames each second`, frames > 10 && frames <= Math.ceil(elapsed / 1000 * 30) + 1, { frames, elapsed, averageFramesPerSecond: frames / (elapsed / 1000) });
  check(entry, `${label}: pixels change without sliding the canvas`, !pixelsBefore.equals(pixelsAfter) && JSON.stringify(before.bounds) === JSON.stringify(after.bounds) && before.transform === after.transform, { before: withoutImage(before), after: withoutImage(after) });
  return { before, after };
}

async function still(entry, page, label, expectedState) {
  // Permit the state-change frame, then require the renderer to be completely idle.
  await delay(100);
  const before = await snapshot(page);
  await delay(500);
  const after = await snapshot(page);
  check(entry, `${label}: a still frame with no recurring animation work`, after.state === expectedState && before.frame === after.frame && before.phase === after.phase && before.shortTimers === after.shortTimers && before.raf === after.raf && after.pendingShortTimers === 0, { before: withoutImage(before), after: withoutImage(after) });
  return after;
}

async function fixDateOnly(page) {
  // Playwright's clock also shims timers. Fix only Date so the renderer cadence,
  // browser animation scheduling and CPU sample use genuinely native wall time.
  await page.addInitScript(value => {
    const NativeDate = window.Date;
    window.__helixFixtureTime = value;
    class FixtureDate extends NativeDate {
      constructor(...arguments_) { super(...(arguments_.length ? arguments_ : [window.__helixFixtureTime])); }
      static now() { return window.__helixFixtureTime; }
    }
    window.Date = FixtureDate;
  }, FIXTURE_NOW);
}

try {
  for (const [device, viewport] of Object.entries(VIEWPORTS)) {
    const entry = { device, checks: [] };
    report.devices.push(entry);
    const context = await browser.newContext({ viewport, deviceScaleFactor: device === 'phone' ? 2 : 1, reducedMotion: 'no-preference', colorScheme: 'light', locale: 'en-US', timezoneId: 'UTC', isMobile: device === 'phone', hasTouch: device === 'phone' });
    const page = await context.newPage();
    const errors = [], requests = [], violations = [];
    let currentState = 'running';
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    await page.addInitScript(() => {
      const audit = window.__helixChecks = { hidden: false, shortTimers: 0, pending: new Set(), raf: 0, violations: [] };
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => audit.hidden });
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => audit.hidden ? 'hidden' : 'visible' });
      const nativeSet = window.setTimeout.bind(window), nativeClear = window.clearTimeout.bind(window);
      window.setTimeout = (callback, duration, ...args) => {
        const short = Number(duration) > 0 && Number(duration) <= 100 && new Error().stack?.includes('/demo.js:');
        let id;
        id = nativeSet(typeof callback === 'function' ? (...values) => { audit.pending.delete(id); callback(...values); } : callback, duration, ...args);
        if (short) { audit.shortTimers++; audit.pending.add(id); }
        return id;
      };
      window.clearTimeout = id => { audit.pending.delete(id); return nativeClear(id); };
      const nativeRaf = window.requestAnimationFrame.bind(window);
      window.requestAnimationFrame = callback => { audit.raf++; return nativeRaf(callback); };
      document.addEventListener('securitypolicyviolation', event => audit.violations.push({ directive: event.violatedDirective, blockedURI: event.blockedURI }));
    });
    // Only Date is fixed: native rendering and animation timers use wall time.
    await fixDateOnly(page);
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== baseURL) { requests.push(url.href); return route.abort(); }
      if (url.pathname === '/api/session' && request.method() === 'GET') return route.fulfill({ json: sessionFixture });
      if (url.pathname === '/api/run' && request.method() === 'GET') return route.fulfill({ json: runFixtures[currentState] });
      if (request.isNavigationRequest() && url.pathname === '/demo.html') {
        const response = await route.fetch();
        return route.fulfill({ response, headers: { ...response.headers(), 'content-security-policy': csp } });
      }
      if (!allowedAssets.has(url.pathname) || url.search) { requests.push(`${request.method()} ${url.href}`); return route.abort(); }
      return route.continue();
    });
    await page.goto(`${baseURL}/demo.html`, { waitUntil: 'networkidle' });
    await page.locator('html[data-fonts="ready"]').waitFor();
    await page.locator('.dna-helix canvas').waitFor();
    await page.waitForFunction(() => document.querySelector('.dna-helix')?.dataset.helixState === 'running');
    await page.evaluate(() => {
      window.__helixOriginalCanvas = document.querySelector('.dna-helix canvas');
      window.__helixOriginalRoot = document.querySelector('.dna-helix');
    });
    entry.renderer = await page.locator('.dna-helix canvas').evaluate(canvas => {
      const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
      if (!gl) return { type: 'canvas2d' };
      const info = gl.getExtension('WEBGL_debug_renderer_info');
      return { type: 'webgl', renderer: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER), vendor: info ? gl.getParameter(info.UNMASKED_VENDOR_WEBGL) : gl.getParameter(gl.VENDOR) };
    });
    check(entry, 'decorative helix adds no screen-reader content or tab stop', await page.locator('.dna-helix').evaluate(root => root.getAttribute('aria-hidden') === 'true' && !root.querySelector('[tabindex],a,button,input,svg circle')));
    await measure(entry, page, 'running', 10000);
    await page.evaluate(value => { window.__helixFixtureTime = value; }, FIXTURE_NOW + 60_000);
    await page.waitForFunction(() => document.querySelector('progress')?.value === 39);
    check(entry, 'minute and polling updates preserve the canvas object', await page.evaluate(() => window.__helixOriginalCanvas === document.querySelector('.dna-helix canvas') && window.__helixOriginalRoot === document.querySelector('.dna-helix')));
    await page.evaluate(() => { window.__helixChecks.hidden = true; document.dispatchEvent(new Event('visibilitychange')); });
    const paused = await still(entry, page, 'hidden tab', 'hidden');
    const resumeTime = await page.evaluate(() => { window.__helixChecks.hidden = false; document.dispatchEvent(new Event('visibilitychange')); return performance.now(); });
    await delay(150);
    const resumed = await snapshot(page);
    check(entry, 'visible tab resumes without jumping through hidden time', resumed.frame > paused.frame && Math.abs(phaseDelta(paused, resumed) - (resumed.time - resumeTime) / 10000 * Math.PI * 2) < 0.12, { paused: withoutImage(paused), resumed: withoutImage(resumed), resumeTime });
    currentState = 'waiting-answer';
    await page.locator('#answer').waitFor();
    await page.waitForFunction(() => document.querySelector('.dna-helix')?.dataset.helixState === 'waiting');
    check(entry, 'question arrival retains the same canvas', await page.evaluate(() => window.__helixOriginalCanvas === document.querySelector('.dna-helix canvas')));
    await measure(entry, page, 'waiting for an answer', 14000);
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.waitForFunction(() => document.querySelector('.dna-helix')?.dataset.helixState === 'reduced');
    await still(entry, page, 'reduced motion selected live', 'reduced');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await page.waitForFunction(() => document.querySelector('.dna-helix')?.dataset.helixState === 'waiting');
    currentState = 'finished-score';
    await page.getByText('Example personalized limit.', { exact: true }).waitFor();
    await delay(100);
    const finishedBefore = await page.evaluate(() => ({ frame: Number(window.__helixOriginalCanvas.dataset.frame), shortTimers: window.__helixChecks.shortTimers, pending: window.__helixChecks.pending.size }));
    await delay(500);
    const finishedAfter = await page.evaluate(() => ({ frame: Number(window.__helixOriginalCanvas.dataset.frame), shortTimers: window.__helixChecks.shortTimers, pending: window.__helixChecks.pending.size }));
    check(entry, 'finished run removes the helix and stops its timers', await page.locator('.dna-helix').count() === 0 && finishedBefore.frame === finishedAfter.frame && finishedBefore.shortTimers === finishedAfter.shortTimers && finishedAfter.pending === 0, { before: finishedBefore, after: finishedAfter });
    violations.push(...await page.evaluate(() => window.__helixChecks.violations));
    check(entry, 'existing production CSP and static routes remain sufficient', violations.length === 0 && requests.length === 0, { violations, requests });
    check(entry, 'no browser errors', errors.length === 0, { errors });
    await context.close();

    const reducedContext = await browser.newContext({ viewport, reducedMotion: 'reduce' });
    const reducedPage = await reducedContext.newPage();
    await reducedPage.addInitScript(() => {
      window.__helixChecks = { shortTimers: 0, pending: new Set(), raf: 0 };
      const nativeSet = window.setTimeout.bind(window), nativeRaf = window.requestAnimationFrame.bind(window);
      window.setTimeout = (callback, duration, ...args) => {
        if (Number(duration) > 0 && Number(duration) <= 100 && new Error().stack?.includes('/demo.js:')) window.__helixChecks.shortTimers++;
        return nativeSet(callback, duration, ...args);
      };
      window.requestAnimationFrame = callback => { window.__helixChecks.raf++; return nativeRaf(callback); };
    });
    await fixDateOnly(reducedPage);
    await reducedPage.route('**/api/**', route => route.fulfill({ json: new URL(route.request().url()).pathname === '/api/session' ? sessionFixture : runFixtures.running }));
    await reducedPage.goto(`${baseURL}/demo.html`, { waitUntil: 'networkidle' });
    await reducedPage.locator('html[data-fonts="ready"]').waitFor();
    await reducedPage.waitForFunction(() => document.querySelector('.dna-helix')?.dataset.helixState === 'reduced');
    const initialStill = await still(entry, reducedPage, 'reduced motion present before load', 'reduced');
    check(entry, 'initial reduced-motion load draws one still frame', initialStill.frame === 1, { frame: initialStill.frame });
    await reducedContext.close();
    console.log(`${device}: ${entry.checks.filter(check => check.passed).length}/${entry.checks.length} checks pass.`);
  }
} catch (error) {
  report.failures.push({ name: 'script exception', message: error.stack });
  console.error(error);
} finally {
  report.passed = report.failures.length === 0;
  await mkdir(resolve(output, '..'), { recursive: true });
  await writeFile(output, JSON.stringify(report, null, 2) + '\n');
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
console.log(`${report.passed ? 'PASS' : 'FAIL'}: ${report.failures.length} helix check failures.`);
if (!report.passed) process.exitCode = 1;
