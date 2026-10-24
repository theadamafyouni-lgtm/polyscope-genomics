import { createWebGLPainter } from './helix-webgl.js';

// Decorative molecule, never a visualization of a person's sequence or progress.
// The backbone and rungs are closed 3D tubes. Their visible surface patches are
// perspective-projected, lit by surface normals, then painted back to front.
const TAU = Math.PI * 2;
export const HELIX_PERIOD = 10_000;
export const HELIX_WAITING_PERIOD = 14_000;
// Round up: browser timers truncate fractional delays, which could exceed 30fps.
export const HELIX_FRAME_MS = Math.ceil(1000 / 30);
const LENGTH = 310;
const RADIUS = 32;
const TURNS = 2.25;
const TUBE = 3.25;
const CAMERA = 360;
const TILT = -Math.PI / 12;
const INITIAL_PHASE = .65;
const BACKGROUND = [23, 63, 53];
const COLORS = [[217, 237, 146], [237, 242, 227], [144, 169, 147], [176, 191, 153], [121, 158, 151]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const unit = a => { const length = Math.hypot(...a); return a.map(value => value / length); };
const LIGHT = unit([-.25, -.65, 1]);

function tube(points, radius, color, sides = 12) {
  const rings = points.map(({ center, normal, tangent }) => {
    const binormal = unit(cross(tangent, normal));
    return Array.from({ length: sides }, (_, index) => {
      const angle = index * TAU / sides;
      const n = normal.map((value, axis) => value * Math.cos(angle) + binormal[axis] * Math.sin(angle));
      return { point: center.map((value, axis) => value + n[axis] * radius), normal: n };
    });
  });
  const patches = [];
  for (let index = 0; index < rings.length - 1; index++) {
    for (let side = 0; side < sides; side++) {
      const next = (side + 1) % sides;
      const vertices = [rings[index][side], rings[index + 1][side], rings[index + 1][next], rings[index][next]];
      patches.push({ vertices: vertices.map(vertex => vertex.point), normal: unit(vertices.reduce((n, vertex) => n.map((value, axis) => value + vertex.normal[axis]), [0, 0, 0])), color });
    }
  }
  // Closed ends, not dots along the backbone. End faces stay within the tube.
  for (const index of [0, rings.length - 1]) {
    patches.push({ vertices: rings[index].map(vertex => vertex.point), normal: points[index].tangent.map(value => value * (index ? 1 : -1)), color });
  }
  return patches;
}

function molecule() {
  const patches = [];
  const twist = TAU * TURNS / LENGTH;
  for (let strand = 0; strand < 2; strand++) {
    const points = Array.from({ length: 145 }, (_, index) => {
      const x = (index / 144 - .5) * LENGTH;
      const angle = x * twist + strand * Math.PI;
      return { center: [x, RADIUS * Math.cos(angle), RADIUS * Math.sin(angle)], normal: [0, Math.cos(angle), Math.sin(angle)], tangent: unit([1, -RADIUS * twist * Math.sin(angle), RADIUS * twist * Math.cos(angle)]) };
    });
    patches.push(...tube(points, TUBE, strand));
  }
  for (let index = 0; index < 24; index++) {
    const x = ((index + .5) / 24 - .5) * LENGTH;
    const angle = x * twist;
    const tangent = [0, Math.cos(angle), Math.sin(angle)];
    // Small segments let the near half occlude a far strand correctly. Rungs
    // meet the inside of each backbone and have rounded, softly lit surfaces.
    const points = Array.from({ length: 9 }, (_, segment) => {
      const distance = (segment / 8 * 2 - 1) * RADIUS;
      return { center: [x, distance * tangent[1], distance * tangent[2]], tangent, normal: [1, 0, 0] };
    });
    patches.push(...tube(points, 1.65, 2 + index % 3, 10));
  }
  return patches;
}
let mesh;

// Rotation around the fixed long (x) axis: x never advances with phase.
export function rotateHelixPoint(point, phase) {
  const [x, y, z] = point;
  const c = Math.cos(phase), s = Math.sin(phase);
  return [x, y * c - z * s, y * s + z * c];
}
function orient(point, phase) {
  const [x, y, z] = rotateHelixPoint(point, phase);
  return [x * Math.cos(TILT) - y * Math.sin(TILT), x * Math.sin(TILT) + y * Math.cos(TILT), z];
}
export function projectHelixPoint(point, phase, width, height) {
  const [x, y, z] = orient(point, phase);
  const scale = Math.min(width / 365, height / 170) * CAMERA / (CAMERA - z);
  return { x: width / 2 + x * scale, y: height / 2 + y * scale, z, scale };
}
export function helixFrame(phase, width, height) {
  const faces = [];
  for (const patch of (mesh ||= molecule())) {
    const normal = orient(patch.normal, phase);
    const points = patch.vertices.map(point => projectHelixPoint(point, phase, width, height));
    const z = points.reduce((sum, point) => sum + point.z, 0) / points.length;
    const center = patch.vertices.reduce((sum, point) => sum.map((value, axis) => value + point[axis] / patch.vertices.length), [0, 0, 0]);
    const worldCenter = orient(center, phase);
    if (dot(normal, [-worldCenter[0], -worldCenter[1], CAMERA - z]) <= 0) continue;
    const depth = Math.max(0, Math.min(1, (z + RADIUS + TUBE) / (2 * (RADIUS + TUBE))));
    const diffuse = Math.max(0, dot(normal, LIGHT));
    const specular = Math.pow(Math.max(0, dot(normal, unit([LIGHT[0], LIGHT[1], LIGHT[2] + 1]))), 18) * .18;
    const brightness = .47 + diffuse * .48 + specular;
    const visibility = .49 + depth * .51;
    const rgb = COLORS[patch.color].map((value, axis) => Math.round(BACKGROUND[axis] + (Math.min(255, value * brightness) - BACKGROUND[axis]) * visibility));
    faces.push({ points, z, color: `rgb(${rgb.join(',')})`, kind: patch.color < 2 ? 'backbone' : 'rung' });
  }
  return faces.sort((a, b) => a.z - b.z);
}

export function helixView({ waiting = false } = {}) {
  return `<div class="dna-helix${waiting ? ' dna-helix-waiting' : ''}" aria-hidden="true"><canvas class="dna-molecule" width="660" height="330"></canvas></div>`;
}

function createCanvasPainter(canvas) {
  const context = canvas.getContext('2d', { alpha: true });
  if (!context) return null;
  return {
    backend: 'canvas',
    draw(phase, width, height, ratio) {
      const pixelWidth = Math.round(width * ratio), pixelHeight = Math.round(height * ratio);
      if (canvas.width !== pixelWidth || canvas.height !== pixelHeight) { canvas.width = pixelWidth; canvas.height = pixelHeight; }
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);
      for (const face of helixFrame(phase, width, height)) {
        context.beginPath();
        for (let index = 0; index < face.points.length; index++) {
          const point = face.points[index];
          if (index) context.lineTo(point.x, point.y); else context.moveTo(point.x, point.y);
        }
        context.closePath(); context.fillStyle = face.color; context.fill();
        context.strokeStyle = face.color; context.lineWidth = .45;
        context.lineJoin = 'round'; context.stroke();
      }
    },
    dispose() {},
  };
}

export function createHelixRenderer(root, environment = {}) {
  const canvas = root.querySelector('canvas');
  const painter = environment.painter || createWebGLPainter(canvas) || createCanvasPainter(canvas);
  if (!painter) { root.dataset.helixState = 'unavailable'; return { sync() {}, dispose() {} }; }
  root.dataset.helixBackend = painter.backend;
  const doc = environment.document || document;
  const win = environment.window || window;
  const now = environment.now || (() => performance.now());
  const schedule = environment.setTimeout || ((callback, delay) => win.setTimeout(callback, delay));
  const cancel = environment.clearTimeout || (id => win.clearTimeout(id));
  const media = win.matchMedia('(prefers-reduced-motion: reduce)');
  let disposed = false, lost = false, timer = null, phase = INITIAL_PHASE, previousTime = null, frame = 0;
  let width = 0, height = 0, ratio = 1, period = HELIX_PERIOD;
  const readState = () => doc.hidden ? 'hidden' : lost ? 'context-lost' : media.matches ? 'reduced' : root.classList.contains('dna-helix-waiting') ? 'waiting' : 'running';
  function stop() { if (timer !== null) cancel(timer); timer = null; previousTime = null; }
  function draw() {
    if (lost || !width || !height) return;
    painter.draw(phase, width, height, ratio);
    canvas.dataset.frame = String(++frame);
    canvas.dataset.phase = phase.toFixed(6);
  }
  function tick() {
    timer = null;
    if (disposed) return;
    // Media/visibility flags can update before their change event is delivered.
    if (lost || doc.hidden || media.matches || !width || !height) { sync(); return; }
    const time = now();
    if (previousTime !== null) phase = (phase + Math.max(0, time - previousTime) * TAU / period) % TAU;
    previousTime = time;
    draw();
    // Schedule after painting, so even a slow frame cannot cause a catch-up burst.
    timer = schedule(tick, HELIX_FRAME_MS);
  }
  function sync() {
    if (disposed) return;
    const state = readState();
    period = root.classList.contains('dna-helix-waiting') ? HELIX_WAITING_PERIOD : HELIX_PERIOD;
    root.dataset.period = String(period);
    root.dataset.helixState = state;
    if (state === 'hidden' || state === 'reduced' || lost || !width || !height) {
      stop();
      if (state === 'reduced' && frame === 0) draw();
    } else if (timer === null) {
      previousTime = now();
      timer = schedule(tick, HELIX_FRAME_MS);
    }
  }
  function resize() {
    if (disposed) return;
    const box = root.getBoundingClientRect();
    const nextWidth = Math.round(box.width), nextHeight = Math.round(box.height);
    const nextRatio = Math.min(win.devicePixelRatio || 1, 1.5);
    if (nextWidth === width && nextHeight === height && nextRatio === ratio) return;
    width = nextWidth; height = nextHeight; ratio = nextRatio;
    if (!doc.hidden) draw();
    sync();
  }
  const Resize = environment.ResizeObserver || win.ResizeObserver;
  const observer = Resize ? new Resize(resize) : null;
  observer?.observe(root);
  if (!observer) win.addEventListener('resize', resize);
  doc.addEventListener('visibilitychange', sync);
  media.addEventListener('change', sync);
  const contextLost = event => { event.preventDefault(); lost = true; sync(); };
  const contextRestored = () => { lost = false; if (!doc.hidden) draw(); sync(); };
  canvas.addEventListener('webglcontextlost', contextLost);
  canvas.addEventListener('webglcontextrestored', contextRestored);
  resize();
  return {
    sync,
    dispose() {
      if (disposed) return;
      disposed = true; stop(); observer?.disconnect(); painter.dispose();
      doc.removeEventListener('visibilitychange', sync);
      media.removeEventListener('change', sync);
      canvas.removeEventListener('webglcontextlost', contextLost);
      canvas.removeEventListener('webglcontextrestored', contextRestored);
      if (!observer) win.removeEventListener('resize', resize);
      root.dataset.helixState = 'disposed';
    },
  };
}
