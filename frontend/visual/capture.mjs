// Usage: PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/capture.mjs
// Optional: --out /absolute/path --states running,finished-score --path /visual/direction.html
// Requires Playwright installed in /tmp/ui-refresh-tools, or PLAYWRIGHT_MODULE.
import { mkdir, writeFile, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { startPreviewServer } from './server.mjs';
import { FIXTURE_NOW, VIEWPORTS, runFixtures, installFixtureRoutes } from './fixtures.mjs';

const args = process.argv.slice(2);
const option = name => args.includes(name) ? args[args.indexOf(name) + 1] : undefined;
const out = resolve(option('--out') || '/root/reviews/ui-shots/before');
const states = (option('--states') || Object.keys(runFixtures).join(',')).split(',');
const pagePath = option('--path');
const direction = option('--direction');
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE || '/tmp/ui-refresh-tools/node_modules/playwright/index.mjs').href);

// Guard the copied score source against accidental changes to scientific values.
const contractsSource = await readFile(new URL('../contracts.test.js', import.meta.url), 'utf8');
const fixturesSource = await readFile(new URL('./fixtures.mjs', import.meta.url), 'utf8');
for (const name of ['exampleJudgment', 'exampleBundle']) {
  const extract = source => source.match(new RegExp(`function ${name}\\([^]*?\\n\\}`))[0];
  if (extract(contractsSource) !== extract(fixturesSource)) throw new Error(`Visual fixture ${name} diverged from contracts.test.js`);
}
await mkdir(out, { recursive: true });
const { server, baseURL } = await startPreviewServer();
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const results = [];
try {
  for (const name of states) {
    for (const [device, viewport] of Object.entries(VIEWPORTS)) {
      const context = await browser.newContext({ viewport, deviceScaleFactor: 1, isMobile: device === 'phone', hasTouch: device === 'phone', locale: 'en-US', timezoneId: 'UTC', reducedMotion: 'reduce', colorScheme: 'light' });
      const page = await context.newPage();
      const errors = [];
      const failedRequests = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
      page.on('requestfailed', request => failedRequests.push({ url: request.url(), error: request.failure()?.errorText }));
      await page.clock.install({ time: FIXTURE_NOW });
      await installFixtureRoutes(page, name);
      const path = pagePath || (name === 'sign-in' ? '/login.html' : '/demo.html');
      const url = new URL(path, baseURL);
      if (pagePath) url.searchParams.set('state', name);
      if (direction) url.searchParams.set('direction', direction);
      await page.goto(url.href, { waitUntil: 'networkidle' });
      if (name === 'sign-in' && !pagePath) await page.locator('#password:not([disabled])').waitFor();
      else if (!pagePath) await page.locator('h1').waitFor();
      if (name === 'offline' && !pagePath) {
        await page.clock.runFor(2100);
        await page.locator('[role="alert"]').waitFor();
      }
      await page.evaluate(() => document.fonts.ready);
      const dimensions = await page.evaluate(() => ({ width: innerWidth, documentWidth: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight, title: document.title }));
      const expectedNetworkFailure = name === 'offline';
      const unexpectedErrors = errors.filter(error => !(expectedNetworkFailure && error.includes('net::ERR_INTERNET_DISCONNECTED')));
      const filename = `${name}-${device}.png`;
      await page.screenshot({ path: resolve(out, filename), fullPage: true, animations: 'disabled', caret: 'hide' });
      results.push({ state: name, device, screenshot: filename, viewport, ...dimensions, overflow: dimensions.documentWidth > viewport.width, errors: unexpectedErrors, expectedNetworkFailure, failedRequests });
      console.log(`${name} ${device}: ${dimensions.documentWidth}×${dimensions.height}; overflow=${dimensions.documentWidth > viewport.width}; errors=${unexpectedErrors.length}`);
      await context.close();
    }
  }
  await writeFile(resolve(out, 'manifest.json'), JSON.stringify({ syntheticTestFixturesOnly: true, noAnalysisPerformed: true, capturedAt: new Date().toISOString(), fixtureClock: new Date(FIXTURE_NOW).toISOString(), browser: browser.version(), source: 'frontend/visual/fixtures.mjs', pagePath: pagePath || 'original product entry points', direction, results }, null, 2) + '\n');
  if (results.some(result => result.overflow || result.errors.length)) process.exitCode = 1;
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
