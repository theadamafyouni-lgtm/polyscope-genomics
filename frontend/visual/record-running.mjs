// Usage: PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/record-running.mjs
// Optional: --out /absolute/path --devices desktop,phone --duration 14
// Uses only the local production UI and intercepted test fixtures; never a worker.
import { mkdir, mkdtemp, readdir, rm, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { setTimeout as delay } from 'node:timers/promises';
import { startPreviewServer } from './server.mjs';
import { FIXTURE_NOW, VIEWPORTS, installFixtureRoutes } from './fixtures.mjs';

const execute = promisify(execFile);
const args = process.argv.slice(2);
const option = name => args.includes(name) ? args[args.indexOf(name) + 1] : undefined;
const out = resolve(option('--out') || '/root/reviews/ui-shots/after');
const devices = (option('--devices') || 'desktop,phone').split(',');
const duration = Number(option('--duration') || 14);
if (!Number.isFinite(duration) || duration < 1 || duration > 60) throw new Error('Duration must be between 1 and 60 seconds.');
if (devices.some(device => !Object.hasOwn(VIEWPORTS, device))) throw new Error('Devices must be desktop and/or phone.');

async function findFFmpeg() {
  if (process.env.FFMPEG_PATH) return process.env.FFMPEG_PATH;
  const browsers = process.env.PLAYWRIGHT_BROWSERS_PATH || '/tmp/ui-refresh-browsers';
  const directories = await readdir(browsers).catch(() => []);
  const bundled = directories.filter(name => /^ffmpeg-\d+$/.test(name)).sort().at(-1);
  if (bundled) return join(browsers, bundled, 'ffmpeg-linux');
  await execute('ffmpeg', ['-version']);
  return 'ffmpeg';
}

const ffmpeg = await findFFmpeg();
const { chromium } = await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE || '/tmp/ui-refresh-tools/node_modules/playwright/index.mjs').href);
await mkdir(out, { recursive: true });
const temporary = await mkdtemp(join(tmpdir(), 'ui-refresh-recording-'));
const { server, baseURL } = await startPreviewServer();
const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
const results = [];

try {
  for (const device of devices) {
    const viewport = VIEWPORTS[device];
    const context = await browser.newContext({
      viewport, deviceScaleFactor: 1, isMobile: device === 'phone', hasTouch: device === 'phone',
      locale: 'en-US', timezoneId: 'UTC', reducedMotion: 'no-preference', colorScheme: 'light',
      recordVideo: { dir: temporary, size: viewport },
    });
    const page = await context.newPage();
    const video = page.video();
    const errors = [];
    const externalRequests = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
    page.on('request', request => {
      if (new URL(request.url()).origin !== baseURL) externalRequests.push(request.url());
    });
    // Freeze only Date through a small fixture shim. Playwright's clock also
    // wraps timers, which would distort the native canvas rendering cadence.
    await page.addInitScript(fixedNow => {
      const NativeDate = window.Date;
      window.Date = class extends NativeDate {
        constructor(...values) { super(...(values.length ? values : [fixedNow])); }
        static now() { return fixedNow; }
      };
    }, FIXTURE_NOW);
    await installFixtureRoutes(page, 'running');
    await page.goto(`${baseURL}/demo.html`, { waitUntil: 'networkidle' });
    await page.locator('html[data-fonts="ready"]').waitFor();
    await page.locator('.dna-helix canvas').waitFor({ state: 'visible' });
    await page.waitForFunction(() => Number(document.querySelector('.dna-helix canvas')?.dataset.frame) > 0);
    await page.evaluate(() => document.fonts.ready);
    await page.waitForFunction(() => document.fonts.check('400 16px Manrope') && document.fonts.check('400 48px Newsreader'));
    // The recording is cropped to its final N seconds, after at least one extra
    // second with loaded fonts. No loading screen or fallback-font frame survives.
    const snapshot = () => page.locator('.dna-helix canvas').evaluate(canvas => ({
      frame: Number(canvas.dataset.frame),
      phase: Number(canvas.dataset.phase),
      state: canvas.closest('.dna-helix').dataset.helixState,
      backend: canvas.closest('.dna-helix').dataset.helixBackend,
      period: Number(canvas.closest('.dna-helix').dataset.period),
      time: performance.now(),
    }));
    const before = await snapshot();
    if (!Number.isFinite(before.phase) || before.frame < 1) throw new Error(`${device}: helix has not rendered its first frame.`);
    console.log(`Recording ${device}: ${duration} seconds of the running screen after fonts load…`);
    await delay((duration + 1.5) * 1000);
    const after = await snapshot();
    const renderedFrames = after.frame - before.frame;
    const observedSeconds = (after.time - before.time) / 1000;
    const observedFramesPerSecond = renderedFrames / observedSeconds;
    if (renderedFrames < 2 || before.phase === after.phase) throw new Error(`${device}: helix did not rotate.`);
    // A one-frame tolerance covers a draw at either edge of the observation.
    if (renderedFrames > Math.ceil(observedSeconds * 30) + 1) throw new Error(`${device}: helix exceeded 30 frames per second.`);
    const dimensions = await page.evaluate(() => ({ width: innerWidth, documentWidth: document.documentElement.scrollWidth }));
    if (dimensions.documentWidth > viewport.width) throw new Error(`${device}: horizontal overflow.`);
    if (errors.length || externalRequests.length) throw new Error(`${device}: ${JSON.stringify({ errors, externalRequests })}`);
    await context.close();

    const raw = await video.path();
    const filename = `running-${device}.webm`;
    const poster = `running-${device}-video-poster.png`;
    // Re-encode for accurate frame boundaries. The Playwright-bundled FFmpeg
    // includes the VP9 decoder/encoder and avoids requiring a system install.
    await execute(ffmpeg, [
      '-hide_banner', '-loglevel', 'error', '-y', '-sseof', String(-duration), '-i', raw,
      '-t', String(duration), '-an', '-c:v', 'libvpx-vp9', '-b:v', '0', '-crf', '25',
      '-deadline', 'good', '-cpu-used', '2', '-pix_fmt', 'yuv420p', resolve(out, filename),
    ], { maxBuffer: 1024 * 1024 });
    await execute(ffmpeg, [
      '-hide_banner', '-loglevel', 'error', '-y', '-i', resolve(out, filename),
      '-frames:v', '1', '-update', '1', resolve(out, poster),
    ], { maxBuffer: 1024 * 1024 });
    results.push({
      device, viewport, filename, poster, durationSeconds: duration, errors, externalRequests,
      rotating: true, renderedFrames, observedSeconds, observedFramesPerSecond,
      initialPhase: before.phase, finalPhase: after.phase,
      renderer: before.backend, animationState: before.state,
      rotationPeriodMilliseconds: before.period,
      fontsLoadedBeforeRecordedSegment: true,
    });
    console.log(`Saved ${resolve(out, filename)}`);
  }
  await writeFile(resolve(out, 'recording-manifest.json'), JSON.stringify({
    syntheticTestFixturesOnly: true, noAnalysisPerformed: true,
    capturedAt: new Date().toISOString(), fixtureClock: new Date(FIXTURE_NOW).toISOString(),
    source: 'frontend/visual/fixtures.mjs', entryPoint: '/demo.html',
    browser: browser.version(), reducedMotion: false, results,
  }, null, 2) + '\n');
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
  await rm(temporary, { recursive: true, force: true });
}
