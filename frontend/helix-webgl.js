// three.js is pinned in package-lock.json and bundled into the existing local
// demo.js asset. No CDN, texture, environment map, network load or runtime import.
import {
  CapsuleGeometry, Curve, DirectionalLight, Fog, Group, HemisphereLight, InstancedMesh, Matrix4, Mesh,
  MeshPhongMaterial, PerspectiveCamera, Scene, SphereGeometry, TubeGeometry,
  Vector3, WebGLRenderer,
} from 'three';

const LENGTH = 310;
const RADIUS = 32;
const TURNS = 2.25;
const TUBE = 3.5;
const CAMERA = 360;
const TILT = Math.PI / 12;
class Backbone extends Curve {
  constructor(offset) { super(); this.offset = offset; }
  getPoint(t, target = new Vector3()) {
    const x = (t - .5) * LENGTH;
    const angle = (t - .5) * Math.PI * 2 * TURNS + this.offset;
    return target.set(x, RADIUS * Math.cos(angle), RADIUS * Math.sin(angle));
  }
}

export function createMolecule() {
  const molecule = new Group();
  const materials = [0xd9ed92, 0xedf2e3, 0x90a993, 0xb0bf99, 0x799e97].map(color =>
    new MeshPhongMaterial({ color, specular: 0x52655b, shininess: 24 }));
  const capGeometry = new SphereGeometry(TUBE, 16, 12);
  for (let strand = 0; strand < 2; strand++) {
    const path = new Backbone(strand * Math.PI);
    const geometry = new TubeGeometry(path, 216, TUBE, 16, false);
    const tube = new Mesh(geometry, materials[strand]);
    tube.userData.role = 'backbone';
    molecule.add(tube);
    const caps = new InstancedMesh(capGeometry, materials[strand], 2);
    for (const end of [0, 1]) caps.setMatrixAt(end, new Matrix4().makeTranslation(...path.getPoint(end).toArray()));
    caps.userData.role = 'end-cap';
    molecule.add(caps);
  }
  const rungRadius = 1.95;
  const rungGeometry = new CapsuleGeometry(rungRadius, RADIUS * 2 - TUBE * 1.5 - rungRadius * 2, 4, 12);
  const rungs = materials.slice(2).map(material => new InstancedMesh(rungGeometry, material, 8));
  for (let index = 0; index < 24; index++) {
    const t = (index + .5) / 24;
    const angle = (t - .5) * Math.PI * 2 * TURNS;
    rungs[index % 3].setMatrixAt(Math.floor(index / 3), new Matrix4().makeRotationX(angle).setPosition((t - .5) * LENGTH, 0, 0));
  }
  rungs.forEach(rung => { rung.userData.role = 'rung'; molecule.add(rung); });
  return molecule;
}

export function createWebGLPainter(canvas) {
  const context = canvas.getContext('webgl2', { antialias: true, alpha: true, powerPreference: 'low-power' });
  if (!context) return null;
  const renderer = new WebGLRenderer({ canvas, context, antialias: true, alpha: true });
  renderer.setClearColor(0x173f35, 0);
  const scene = new Scene();
  scene.fog = new Fog(0x173f35, 326, 438);
  const camera = new PerspectiveCamera(30, 1, 1, 700);
  camera.position.z = CAMERA;
  const axis = new Group();
  axis.rotation.z = TILT;
  const molecule = createMolecule();
  axis.add(molecule); scene.add(axis);
  scene.add(new HemisphereLight(0xf7f7f0, 0x173f35, 1.25));
  const key = new DirectionalLight(0xf7f7f0, 1.55);
  key.position.set(-80, 120, 220); scene.add(key);
  const fill = new DirectionalLight(0xd9ed92, .3);
  fill.position.set(60, -40, -80); scene.add(fill);
  let width = 0, height = 0, ratio = 0;
  return {
    backend: 'webgl',
    draw(phase, nextWidth, nextHeight, nextRatio) {
      if (nextWidth !== width || nextHeight !== height || nextRatio !== ratio) {
        width = nextWidth; height = nextHeight; ratio = nextRatio;
        renderer.setPixelRatio(ratio);
        renderer.setSize(width, height, false);
        const scale = Math.min(width / 365, height / 170);
        camera.aspect = width / height;
        camera.fov = 2 * Math.atan(height / (2 * scale * CAMERA)) * 180 / Math.PI;
        camera.updateProjectionMatrix();
      }
      // Only the molecule's local long axis rotates. Position, tilt and camera
      // remain fixed, with a depth buffer resolving every crossing per pixel.
      molecule.rotation.x = -phase;
      renderer.render(scene, camera);
    },
    dispose() {
      const geometries = new Set(), materials = new Set();
      molecule.traverse(object => { if (object.geometry) geometries.add(object.geometry); if (object.material) materials.add(object.material); if (object.isInstancedMesh) object.dispose(); });
      geometries.forEach(geometry => geometry.dispose());
      materials.forEach(material => material.dispose());
      renderer.dispose();
      renderer.forceContextLoss();
    },
  };
}
