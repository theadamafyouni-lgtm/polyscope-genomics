// Isolated browser checks for the two review directions; never uses a real API.
// PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/check-directions.mjs
// Install @axe-core/playwright beside Playwright in /tmp/ui-refresh-tools first.
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { startPreviewServer } from './server.mjs';
import { FIXED_NOW, VIEWPORTS } from './fixtures.mjs';

const toolRoot = process.env.UI_REVIEW_TOOLS || '/tmp/ui-refresh-tools/node_modules';
const { chromium } = await import(pathToFileURL(`${toolRoot}/playwright/index.mjs`).href);
const { default: AxeBuilder } = await import(pathToFileURL(`${toolRoot}/@axe-core/playwright/dist/index.mjs`).href);
const output = process.env.UI_REVIEW_CHECKS || '/root/reviews/ui-shots/directions/checks.json';
const { server, baseURL } = await startPreviewServer();
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const report = { syntheticTestFixturesOnly: true, noAnalysisPerformed: true, checkedAt: new Date().toISOString(), browser: browser.version(), standards: ['wcag2a', 'wcag2aa', 'wcag21aa'], accessibility: [], interactions: [], failures: [] };

function check(group, name, condition, detail = {}) {
  const result = { name, passed: Boolean(condition), ...detail };
  group.push(result);
  if (!condition) { report.failures.push(result); console.log(`FAIL ${name}: ${JSON.stringify(detail)}`); }
  return condition;
}

async function instrument(page, permissionResult = 'granted') {
  await page.addInitScript(({ permissionResult }) => {
    window.__visualChecks = { permissionRequests: 0, notifications: [], tones: 0, audioResumes: 0, hidden: false };
    class TestNotification {
      static permission = 'default';
      static async requestPermission() {
        window.__visualChecks.permissionRequests++;
        TestNotification.permission = permissionResult;
        return permissionResult;
      }
      constructor(title, options) { window.__visualChecks.notifications.push({ title, ...options }); }
      close() {}
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
    // Deterministic browser visibility signal; represents the person in another tab.
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => window.__visualChecks.hidden });
  }, { permissionResult });
  await page.clock.install({ time: FIXED_NOW });
}

async function openPage(direction, state, viewport, permission = 'granted') {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1, locale: 'en-US', timezoneId: 'UTC', reducedMotion: 'reduce', colorScheme: 'light' });
  const page = await context.newPage();
  const errors = [];
  const unexpectedRequests = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  await page.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin !== baseURL || url.pathname.startsWith('/api/')) {
      unexpectedRequests.push(url.href); return route.abort();
    }
    return route.continue();
  });
  await instrument(page, permission);
  await page.goto(`${baseURL}/visual/direction.html?direction=${direction}&state=${state}`, { waitUntil: 'networkidle' });
  await page.locator('#main h1').waitFor();
  await page.evaluate(() => document.fonts.ready);
  return { context, page, errors, unexpectedRequests };
}

try {
  for (const direction of ['a', 'b']) {
    for (const state of ['running', 'waiting-answer', 'finished-score', 'finished-refusal']) {
      for (const [device, viewport] of Object.entries(VIEWPORTS)) {
        const { context, page, errors, unexpectedRequests } = await openPage(direction, state, viewport);
        const key = `${direction}/${state}/${device}`;
        const entry = { direction, state, device, checks: [] };
        report.accessibility.push(entry);
        const result = await new AxeBuilder({ page }).withTags(report.standards).analyze();
        entry.violations = result.violations.map(({ id, impact, description, help, nodes }) => ({ id, impact, description, help, nodes: nodes.map(({ target, failureSummary }) => ({ target, failureSummary })) }));
        entry.incomplete = result.incomplete.map(({ id, nodes }) => ({ id, targets: nodes.map(node => node.target) }));
        check(entry.checks, `${key}: wcag aa`, entry.violations.length === 0, { violationCount: entry.violations.length });
        if (entry.violations.length) console.log(JSON.stringify({ key, violations: entry.violations }));
        const dimensions = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }));
        check(entry.checks, `${key}: no overflow`, dimensions.content <= dimensions.viewport, dimensions);
        check(entry.checks, `${key}: permission unrequested`, await page.evaluate(() => window.__visualChecks.permissionRequests === 0));
        if (state === 'waiting-answer') check(entry.checks, `${key}: question tab title`, (await page.title()).includes('Answer needed'));
        const summary = page.locator('details > summary').first();
        if (await summary.count()) {
          await summary.focus();
          await page.keyboard.press('Enter');
          check(entry.checks, `${key}: details opens with enter`, await summary.evaluate(element => element.parentElement.open));
          await page.keyboard.press('Space');
          check(entry.checks, `${key}: details closes with space`, await summary.evaluate(element => !element.parentElement.open));
        }
        if (device === 'phone') {
          await page.setViewportSize({ width: 320, height: 844 });
          const narrow = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }));
          check(entry.checks, `${key}: 320px no overflow`, narrow.content <= narrow.viewport, narrow);
        }
        check(entry.checks, `${key}: no browser errors`, errors.length === 0, { errors });
        check(entry.checks, `${key}: no external or api requests`, unexpectedRequests.length === 0, { unexpectedRequests });
        console.log(`${key}: ${entry.violations.length} axe violations; ${entry.checks.filter(item => !item.passed).length} failed checks`);
        await context.close();
      }
    }
    for (const permission of ['granted', 'denied']) {
      const { context, page } = await openPage(direction, 'running', VIEWPORTS.desktop, permission);
      const entry = { direction, permission, checks: [] };
      report.interactions.push(entry);
      const key = `${direction}/notifications-${permission}`;
      check(entry.checks, `${key}: no permission on load`, await page.evaluate(() => window.__visualChecks.permissionRequests === 0));
      await page.locator('[data-action="alerts"]').click();
      await page.waitForFunction(() => document.querySelector('[data-action="alerts"]')?.getAttribute('aria-pressed') === 'true');
      check(entry.checks, `${key}: permission requested once on click`, await page.evaluate(() => window.__visualChecks.permissionRequests === 1));
      const tonesBeforeQuestion = await page.evaluate(() => window.__visualChecks.tones);
      await page.evaluate(() => { window.__visualChecks.hidden = true; });
      await page.locator('[data-state="waiting-answer"]').click();
      await page.locator('#answer').waitFor();
      check(entry.checks, `${key}: waiting title after transition`, (await page.title()).includes('Answer needed'));
      const first = await page.evaluate(() => structuredClone(window.__visualChecks));
      check(entry.checks, `${key}: hidden question notification`, first.notifications.length === (permission === 'granted' ? 1 : 0), { notifications: first.notifications });
      check(entry.checks, `${key}: question sound`, first.tones === tonesBeforeQuestion + 1, { tonesBeforeQuestion, tonesAfterQuestion: first.tones });
      await page.locator('[data-state="waiting-answer"]').click();
      const replay = await page.evaluate(() => structuredClone(window.__visualChecks));
      check(entry.checks, `${key}: same question does not repeat alert`, replay.notifications.length === first.notifications.length && replay.tones === first.tones);
      const draft = 'Synthetic keyboard test answer — do not send.';
      await page.locator('#answer').fill(draft);
      await page.locator('[data-action="alerts"]').click();
      check(entry.checks, `${key}: answer survives disabling alerts`, await page.locator('#answer').inputValue() === draft);
      await page.locator('[data-action="alerts"]').click();
      await page.waitForFunction(() => document.querySelector('[data-action="alerts"]')?.getAttribute('aria-pressed') === 'true');
      check(entry.checks, `${key}: answer survives enabling alerts`, await page.locator('#answer').inputValue() === draft);
      check(entry.checks, `${key}: existing permission not requested again`, await page.evaluate(() => window.__visualChecks.permissionRequests === 1));
      await context.close();
    }
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
