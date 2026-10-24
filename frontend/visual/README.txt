LOCAL VISUAL REVIEW FIXTURES — SYNTHETIC, NOT SCIENTIFIC DATA

Round 3: report layers and question choices
  PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/check-round3.mjs
  Output defaults to /root/reviews/ui-shots/round3; UI_ROUND3_OUTPUT overrides it.
  UI_ROUND3_STATES and UI_ROUND3_DEVICES can select comma-separated subsets.

  round3-fixtures.mjs copies the example summary, limits, question and z-score
  fields from /root/reviews/ui-fields.md. The documentation does not supply a
  comparison score vector or quantiles, so that example is a labeled component
  preview with no invented figures. It is not an API response or validated run.
  contract-fixture/ uses the complete checked test bundle through the actual app.
  Its grid and underlying scores remain unchanged. Neither fixture analyzes DNA.

  Checks cover closed/open reports, exact summary order and delivered z-score,
  native disclosure keyboard behavior, choices and Other text, exact answer
  payloads, polling and focus preservation, accessibility, self-hosted fonts,
  production CSP, and overflow at desktop, phone and narrow-phone widths.
  The existing check-refresh.mjs also verifies notification and helix behavior;
  check-round2.mjs retains the comparison-grid and exact-range regressions.

Purpose
  Reproduce all nine current demo-site states without connecting to the worker,
  launching an agent, reading genomes, uploading a file, or contacting a live site.
  The browser renders the actual demo.html/demo.js and login.html/login.js.
  Playwright intercepts every /api/ request. The localhost server serves files only.
  Nothing under visual/ is imported by the product entry points.

Implemented refresh
  The user selected A, with B's serif headlines and “Genetics, with context.”
  Production source: demo-app.js, workspace.js, question-alerts.js, result-chart.js,
  helix.js, helix-webgl.js, fonts.js and the source CSS files. demo.js, login.js and demo.css are
  generated and committed because the existing server serves those assets.

  npm ci --prefix frontend
  npm run build --prefix frontend
  npm test
  node --test frontend/visual/presentation.test.mjs
  python3 -m pytest -q tests/test_demo_*.py demo_site/test_worker_progress.py

  Final browser review (production CSP and static asset allowlist):
    npm install --prefix /tmp/ui-refresh-tools --cache /tmp/ui-refresh-npm-cache playwright @axe-core/playwright
    PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/check-refresh.mjs
  Output: /root/reviews/ui-shots/after/checks.json

  Final screenshots and recordings:
    PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/capture.mjs --out /root/reviews/ui-shots/after
    PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/record-running.mjs
    python3 frontend/visual/contact-sheet.py /root/reviews/ui-shots/after --label After
  Recordings retain 14 seconds after fonts load. They use native timers and CSS
  motion while freezing only Date; screenshots use reduced motion for stability.
  The recording script uses Playwright's bundled FFmpeg or FFMPEG_PATH.
  For the helix-only follow-up, pass --out /root/reviews/ui-shots/helix to the
  capture and recording scripts. HELIX-REVIEW.txt records its two critique rounds;
  check-helix.mjs verifies real-time rotation, frame cap, visibility and lifecycle.
  The helix check and recorder freeze only Date, leaving native browser timers and
  performance.now untouched. Their motion checks are separate from fake-clock
  polling tests in check-refresh.mjs.

  CRITIQUES.txt records two screenshot-led refinement rounds. Round-0 and round-1
  captures are retained under after/. Final nine-state captures, both recordings,
  manifests and contact sheets are directly under after/.

Archived direction review (before the user selected A with B typography)
  PORT=8091 node frontend/visual/server.mjs
  http://127.0.0.1:8091/visual/direction.html?direction=a&state=running
  http://127.0.0.1:8091/visual/direction.html?direction=b&state=finished-score
  Direction A: Clear workbench. Sans-serif hierarchy; focused forest work card.
  Direction B: Field journal. Editorial serif; forest sidebar; paper canvas.
  Footer review links switch between running, question, score, and refusal.
  Question alerts are opt-in; the title always flags a waiting question. The
  prototype sound/notification behavior can be exercised with the Question link.
  Answer/end/sign-out controls are preview-only: no answer, data, or mutation is
  sent. Typed answers exist only in the current page. No storage is used.
  These archived prototypes are isolated from the implemented product UI.

  Each direction is captured with:
    PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/capture.mjs \
      --out /root/reviews/ui-shots/directions/a \
      --states running,finished-score,waiting-answer,finished-refusal \
      --path /visual/direction.html --direction a
  Substitute b for the second direction.

  Data-contract checks for the review presentation:
    node --test frontend/visual/presentation.test.mjs
  Existing frontend regression suite:
    npm test
  Browser accessibility and interaction checks:
    PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/check-directions.mjs

  The current product reads only the worker's run.estimate; missing measurements
  show no numeric estimate. Review fixtures use the measured chip bootstrap range
  (44–61 minutes, two runs) from review-fixes 8459945. Redraw checks preserve it.
  Archived direction prototypes retain the original illustrative timing. Elapsed time requires retained run_started.ts. Missing
  start timestamps suppress the countdown; pending stages say Not yet reported
  because earlier events may have fallen outside the rolling window.
  Results pass the existing getDisplayResult validation unchanged. Numeric values
  retain all digits; measured weight coverage is shown as the recorded fraction.
  No report decision/reasons are substituted for the delivered explanation.

Capture
  npm install --prefix /tmp/ui-refresh-tools --cache /tmp/ui-refresh-npm-cache playwright
  PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers /tmp/ui-refresh-tools/node_modules/.bin/playwright install chromium
  PLAYWRIGHT_BROWSERS_PATH=/tmp/ui-refresh-browsers node frontend/visual/capture.mjs

  Optional capture arguments:
    --out /root/reviews/ui-shots/after
    --states running,finished-score
    --path /visual/direction.html
    --direction a
  A custom --path receives ?state=STATE and optional &direction=DIRECTION.
  Set PLAYWRIGHT_MODULE to an absolute index.mjs path for another installation.
  Capture starts and closes its own server bound to 127.0.0.1 on a random port.

  Manual static review: PORT=8091 node frontend/visual/server.mjs
  This does not provide an API. Use a fixture-aware preview page for manual review.

Fixture exports
  FIXED_NOW / FIXTURE_NOW: fixed milliseconds since epoch.
  session / sessionFixture: synthetic authenticated session response.
  fixtures / runFixtures: state names mapped to API run responses.
  fixtureState(name): a fresh renderDemo state for the selected fixture.
  installFixtureRoutes(page, name): Playwright route installation.
  VIEWPORTS: desktop 1440x1000; phone 390x844; device scale factor 1.

Fixture provenance
  finished-score:
    exampleJudgment() and exampleBundle() are copied verbatim from
    frontend/contracts.test.js. capture.mjs verifies their source text matches.
    No number, model, warning, result, or report sentence is changed.
    These are existing synthetic contract-test numbers, never real analysis data.
  finished-refusal:
    exampleRefusal imported directly from frontend/examples.js, unmodified.
  waiting-answer:
    exampleQuestion imported directly from frontend/examples.js; question_id and
    question are mapped to the worker response's id and text keys. Copy unchanged.
  session:
    Upload/expanded byte limits from demo_site/input-limits.json, unmodified.
    Valid sample names and byte counts from frontend/demo.test.js, unmodified.
    CSRF is a non-secret literal test token. Expiry is a synthetic clock fixture.
  busy:
    Two slots and a retry in 42 minutes copied from the existing busy case in
    frontend/demo.test.js, including its fixed 2026-10-14 18:00 UTC clock.
  running:
    Synthetic stage scaffold uses the event kinds/outcomes in session.test.js.
    All event prose is copied from demo_site/test_worker_progress.py.
    Current step: score. A synthetic run_started timestamp 38 minutes before the
    fixed clock exercises elapsed-time UI. It is not measured run performance.
  offline:
    Initial running response succeeds; the next poll fails with Playwright's
    internetdisconnected abort. The UI retains the last confirmed run status.
    This expected network console error is distinguished in manifest.json.
  error:
    Failed process status and 'Example process failure.' from session.test.js.
  sign-in / upload:
    Ready signed-out session / idle run. No login or upload mutation is submitted.

Clock and capture conditions
  Fixed time: 2026-10-14T18:00:00.000Z. UTC; en-US; reduced motion; light scheme.
  Screenshots are full-page and retain the original DOM without overlays.
  manifest.json accompanies each screenshot set and records synthetic status,
  browser version, viewport, document dimensions, JS errors, failed requests,
  and horizontal overflow. Every state is captured on desktop and phone.
