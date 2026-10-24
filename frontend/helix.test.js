import test from 'node:test';
import assert from 'node:assert/strict';
import { helixView, rotateHelixPoint, projectHelixPoint, helixFrame, createHelixRenderer, HELIX_PERIOD, HELIX_WAITING_PERIOD, HELIX_FRAME_MS } from './helix.js';
import { createMolecule } from './helix-webgl.js';

test('the canvas is decorative and introduces no run content, plotted points or focus stops', () => {
  const html = helixView();
  assert.match(html, /aria-hidden="true"/);
  assert.match(html, /<canvas /);
  assert.doesNotMatch(html, /<circle|<svg|<script|tabindex|style=|<img/);
  assert.match(helixView({ waiting: true }), /dna-helix-waiting/);
});

test('axial rotation preserves every long-axis coordinate and radial distance for a whole turn', () => {
  for (const phase of [0, .7, Math.PI / 2, Math.PI, Math.PI * 2]) {
    for (const point of [[-155, 32, 0], [0, 13, -29], [155, 0, 32]]) {
      const next = rotateHelixPoint(point, phase);
      assert.equal(next[0], point[0]);
      assert.ok(Math.abs(Math.hypot(next[1], next[2]) - Math.hypot(point[1], point[2])) < 1e-10);
      if (phase === Math.PI * 2) next.forEach((value, axis) => assert.ok(Math.abs(value - point[axis]) < 1e-10));
    }
  }
});

test('perspective makes the near surface larger and fallback surfaces are sorted far to near', () => {
  const near = projectHelixPoint([10, 10, 32], 0, 440, 220);
  const far = projectHelixPoint([10, 10, -32], 0, 440, 220);
  assert.ok(near.scale > far.scale);
  const faces = helixFrame(.65, 440, 220);
  assert.ok(faces.some(face => face.kind === 'backbone'));
  assert.ok(faces.some(face => face.kind === 'rung'));
  assert.ok(faces.every((face, index) => index === 0 || face.z >= faces[index - 1].z));
  assert.ok(faces.every(face => face.points.every(point => Number.isFinite(point.x) && Number.isFinite(point.y))));
});

test('WebGL molecule uses smooth tubes, rounded ends and rounded rungs with depth tests', () => {
  const molecule = createMolecule();
  const count = role => molecule.children.filter(child => child.userData.role === role);
  assert.equal(count('backbone').length, 2);
  assert.equal(count('end-cap').reduce((sum, child) => sum + child.count, 0), 4);
  assert.equal(count('rung').reduce((sum, child) => sum + child.count, 0), 24);
  assert.ok(count('backbone').every(child => child.geometry.type === 'TubeGeometry' && !child.material.flatShading));
  assert.ok(count('rung').every(child => child.geometry.type === 'CapsuleGeometry'));
  assert.ok(molecule.children.every(child => child.material.depthTest && child.material.depthWrite && !child.material.transparent));
  const geometries = new Set(molecule.children.map(child => child.geometry));
  const materials = new Set(molecule.children.map(child => child.material));
  geometries.forEach(geometry => geometry.dispose()); materials.forEach(material => material.dispose());
});

function fixture({ reduced = false, hidden = false } = {}) {
  let time = 0, waiting = false, paintCount = 0, disposedCount = 0, observer;
  let box = { width: 440, height: 220 };
  const timers = new Map(); let nextTimer = 0;
  const media = Object.assign(new EventTarget(), { matches: reduced });
  const doc = Object.assign(new EventTarget(), { hidden });
  const canvas = Object.assign(new EventTarget(), { dataset: {} });
  const root = { dataset: {}, querySelector: () => canvas, classList: { contains: () => waiting }, getBoundingClientRect: () => box };
  const painter = { backend: 'test', draw: () => paintCount++, dispose: () => disposedCount++ };
  const win = { devicePixelRatio: 2, matchMedia: () => media };
  const controller = createHelixRenderer(root, { document: doc, window: win, painter, now: () => time,
    setTimeout: (callback, delay) => { timers.set(++nextTimer, { callback, delay }); return nextTimer; },
    clearTimeout: id => timers.delete(id),
    ResizeObserver: class { constructor(callback) { observer = callback; } observe() {} disconnect() {} },
  });
  return { root, canvas, media, doc, controller, timers,
    get paints() { return paintCount; }, get disposals() { return disposedCount; },
    tick(milliseconds = 34) { time += milliseconds; const [id, item] = timers.entries().next().value; timers.delete(id); item.callback(); },
    wait(milliseconds) { time += milliseconds; },
    waiting(value) { waiting = value; controller.sync(); },
    visibility(value) { doc.hidden = value; doc.dispatchEvent(new Event('visibilitychange')); },
    motion(value) { media.matches = value; media.dispatchEvent(new Event('change')); },
    resize(width, height) { box = { width, height }; observer(); },
  };
}

test('animation schedules at most 30fps and uses elapsed time for a ten-second turn', () => {
  const f = fixture();
  assert.equal(f.paints, 1);
  assert.equal(f.root.dataset.period, String(HELIX_PERIOD));
  assert.ok(HELIX_FRAME_MS >= 1000 / 30);
  assert.ok([...f.timers.values()].every(timer => timer.delay >= 1000 / 30));
  const phase = Number(f.canvas.dataset.phase);
  f.tick(1000);
  assert.ok(Math.abs(Number(f.canvas.dataset.phase) - phase - Math.PI / 5) < 1e-6);
  f.tick(9000);
  assert.ok(Math.abs(Number(f.canvas.dataset.phase) - phase) < 1e-6);
  f.controller.dispose();
});

test('waiting slows rotation without replacing the canvas or restarting phase', () => {
  const f = fixture(); f.tick(1000);
  const phase = Number(f.canvas.dataset.phase);
  f.waiting(true);
  assert.equal(f.root.dataset.period, String(HELIX_WAITING_PERIOD));
  assert.equal(Number(f.canvas.dataset.phase), phase);
  f.tick(1000);
  assert.ok(Math.abs(Number(f.canvas.dataset.phase) - phase - Math.PI * 2 / 14) < 1e-6);
  f.controller.dispose();
});

test('hidden tabs cancel work and resume from the same phase without catching up', () => {
  const f = fixture(); f.tick(1000);
  const phase = Number(f.canvas.dataset.phase), paints = f.paints;
  f.visibility(true); f.wait(3_600_000);
  assert.equal(f.timers.size, 0); assert.equal(f.paints, paints);
  f.visibility(false); f.tick(34);
  assert.ok(Math.abs(Number(f.canvas.dataset.phase) - phase - 34 * Math.PI * 2 / HELIX_PERIOD) < 1e-6);
  f.controller.dispose();
});

test('reduced motion paints one still frame with no loop and remains still on sync', () => {
  const f = fixture({ reduced: true });
  assert.equal(f.paints, 1); assert.equal(f.timers.size, 0);
  f.controller.sync(); f.waiting(true); f.resize(440, 220);
  assert.equal(f.paints, 1); assert.equal(f.timers.size, 0);
  f.resize(300, 130); assert.equal(f.paints, 2); assert.equal(f.timers.size, 0);
  f.motion(false); assert.equal(f.timers.size, 1);
  f.tick(); f.motion(true);
  const paints = f.paints;
  f.controller.sync(); assert.equal(f.paints, paints); assert.equal(f.timers.size, 0);
  f.controller.dispose();
});

test('context loss pauses drawing, restoration resumes, and disposal releases resources once', () => {
  const f = fixture(); f.tick();
  f.canvas.dispatchEvent(new Event('webglcontextlost', { cancelable: true }));
  assert.equal(f.root.dataset.helixState, 'context-lost');
  assert.equal(f.timers.size, 0);
  f.canvas.dispatchEvent(new Event('webglcontextrestored'));
  assert.equal(f.root.dataset.helixState, 'running'); assert.equal(f.timers.size, 1);
  f.controller.dispose(); f.controller.dispose();
  assert.equal(f.disposals, 1); assert.equal(f.timers.size, 0);
  const paints = f.paints;
  f.motion(true); f.visibility(true); f.resize(320, 120); f.controller.sync();
  assert.equal(f.paints, paints);
});

test('a changed motion flag is respected even before its browser event arrives', () => {
  const f = fixture();
  f.media.matches = true;
  f.tick();
  assert.equal(f.root.dataset.helixState, 'reduced');
  assert.equal(f.paints, 1); assert.equal(f.timers.size, 0);
  f.media.matches = false;
  // The existing status poll also synchronizes the renderer's environment.
  f.controller.sync();
  assert.equal(f.root.dataset.helixState, 'running'); assert.equal(f.timers.size, 1);
  f.controller.dispose();
});
