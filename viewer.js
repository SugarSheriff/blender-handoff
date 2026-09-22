import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { inspect, GlbError } from './inspect.js';

const MAX_FILE_BYTES = 150 * 1024 * 1024;
const container = document.getElementById('viewer');
const hint = document.getElementById('viewer-hint');
const reportEl = document.getElementById('report');
const fileInput = document.getElementById('file-input');

// ---------- Renderer ----------
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
// Khronos PBR Neutral keeps base colors close to their authored values, which is what
// you want when the question is "does this match what the artist approved?"
renderer.toneMapping = THREE.NeutralToneMapping;
container.prepend(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color('#171d2c');
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

const camera = new THREE.PerspectiveCamera(40, 1, 0.01, 1000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

const grid = new THREE.GridHelper(10, 20, 0x3a4256, 0x252c3d);
scene.add(grid);

let current = null;
const loader = new GLTFLoader();

function resize() {
  const { clientWidth: w, clientHeight: h } = container;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(container);
resize();

renderer.setAnimationLoop(() => {
  controls.update();
  renderer.render(scene, camera);
});

function frame(object) {
  const box = new THREE.Box3().setFromObject(object);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.length() / 2, 1e-4);
  const distance = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.1;
  camera.near = distance / 100;
  camera.far = distance * 100;
  camera.position.copy(center).add(new THREE.Vector3(0.8, 0.55, 1).normalize().multiplyScalar(distance));
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  // Scale the grid to the model so a 2 cm ring and a 200 m building both read sensibly.
  const step = 10 ** Math.floor(Math.log10(radius));
  grid.scale.setScalar(step);
  grid.position.set(center.x, box.min.y, center.z);
}

function dispose(object) {
  object.traverse((o) => {
    o.geometry?.dispose();
    for (const m of [].concat(o.material || [])) {
      for (const v of Object.values(m)) if (v?.isTexture) v.dispose();
      m.dispose();
    }
  });
}

// ---------- Loading ----------
async function load(buffer, name) {
  let report;
  try {
    report = inspect(buffer);
  } catch (e) {
    renderReport(name, null, e instanceof GlbError ? e.message : `unexpected error: ${e.message}`);
    hint.textContent = 'Not a readable GLB. See the report →';
    return;
  }
  renderReport(name, report);
  hint.textContent = 'Loading…';
  loader.parse(buffer, '', (gltf) => {
    if (current) { scene.remove(current); dispose(current); }
    current = gltf.scene;
    scene.add(current);
    frame(current);
    hint.textContent = '';
  }, (err) => {
    // The inspector passed but three.js could not load it, often because of an
    // extension this page has no decoder for. Worth saying so plainly.
    hint.textContent = `three.js could not render this file: ${err.message || err}`;
  });
}

async function loadSample() {
  hint.textContent = 'Loading sample…';
  try {
    const res = await fetch('sample.glb');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await load(await res.arrayBuffer(), 'sample.glb');
  } catch (e) {
    hint.textContent = `Could not fetch sample.glb (${e.message}). Serve this folder over HTTP rather than opening it from disk.`;
  }
}

async function loadFile(file) {
  if (!file) return;
  if (/\.gltf$/i.test(file.name)) {
    renderReport(file.name, null, 'This inspector reads .glb (binary glTF). A .gltf file points to external buffers and textures, which a single dropped file can\'t include. Re-export as GLB.');
    return;
  }
  if (file.size > MAX_FILE_BYTES) {
    renderReport(file.name, null, `File is ${(file.size / 2 ** 20).toFixed(0)} MB; this demo stops at ${MAX_FILE_BYTES / 2 ** 20} MB.`);
    return;
  }
  await load(await file.arrayBuffer(), file.name);
}

// ---------- Report ----------
const fmt = (n) => n.toLocaleString();
const meters = (v) => (v >= 1 ? `${v.toFixed(2)} m` : v >= 0.01 ? `${(v * 100).toFixed(1)} cm` : `${(v * 1000).toFixed(1)} mm`);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, attrs);
  node.append(...children);
  return node;
}

function renderReport(name, r, fatal) {
  reportEl.replaceChildren();
  reportEl.append(el('h3', { textContent: name }));
  if (fatal) {
    reportEl.append(el('div', { className: 'verdict bad', textContent: `Rejected: ${fatal}` }));
    return;
  }
  reportEl.append(el('div', { className: 'file-meta', textContent: `${fmt(r.bytes)} bytes · generator: ${r.generator || 'unknown'}` }));

  const size = r.bounds?.size;
  const rows = [
    ['glTF version', r.version ?? '—'],
    ['Nodes / meshes / primitives', `${r.counts.nodes} / ${r.counts.meshes} / ${r.counts.primitives}`],
    ['Materials / textures', `${r.counts.materials} / ${r.counts.textures}`],
    ['Triangles (as rendered)', fmt(r.triangles)],
    ['World size (x × y × z)', size ? size.map(meters).join(' × ') : '—'],
    ['Images', r.images.length ? [...new Set(r.images)].join(', ') : 'none'],
    ['Animations', String(r.counts.animations)],
    ['extensionsUsed', r.extensionsUsed.join(', ') || 'none'],
    ['extensionsRequired', r.extensionsRequired.join(', ') || 'none'],
  ];
  reportEl.append(el('table', {}, el('tbody', {}, ...rows.map(([k, v]) => el('tr', {}, el('th', { textContent: k }), el('td', { textContent: v }))))));

  if (r.issues.length) {
    reportEl.append(el('div', { className: 'verdict bad' }, 'Ingest would reject this file:',
      el('ul', {}, ...r.issues.map((i) => el('li', { textContent: i })))));
  } else {
    reportEl.append(el('div', { className: 'verdict ok', textContent: 'Passes ingest validation.' }));
  }

  // Same thresholds as preflight's units.suspicious_size rule, applied to the output.
  const largest = size ? Math.max(...size) : 0;
  if (largest > 500) {
    reportEl.append(el('div', { className: 'verdict note', textContent: `Largest dimension is ${fmt(Math.round(largest))} m. That's usually a centimeter asset exported as meters.` }));
  } else if (largest > 0 && largest < 0.005) {
    reportEl.append(el('div', { className: 'verdict note', textContent: `Largest dimension is ${meters(largest)}. Likely a units mix-up.` }));
  }

  if (r.materials.length) {
    const mats = el('div', { className: 'mats' }, el('h4', { textContent: 'Materials' }));
    for (const m of r.materials.slice(0, 12)) {
      const row = el('div', { className: 'mat' }, el('span', { textContent: m.name }));
      for (const map of m.maps) row.append(el('span', { className: 'chip', textContent: map }));
      if (m.alphaMode !== 'OPAQUE') row.append(el('span', { className: 'chip', textContent: m.alphaMode.toLowerCase() }));
      if (!m.maps.length) row.append(el('span', { className: 'chip', textContent: 'factors only' }));
      mats.append(row);
    }
    if (r.materials.length > 12) mats.append(el('div', { className: 'chip', textContent: `+${r.materials.length - 12} more` }));
    reportEl.append(mats);
  }
}

// ---------- Wiring ----------
fileInput.addEventListener('change', () => loadFile(fileInput.files[0]));
document.getElementById('btn-sample').addEventListener('click', loadSample);
container.addEventListener('dragover', (e) => { e.preventDefault(); container.classList.add('dragging'); });
container.addEventListener('dragleave', () => container.classList.remove('dragging'));
container.addEventListener('drop', (e) => {
  e.preventDefault();
  container.classList.remove('dragging');
  loadFile(e.dataTransfer.files[0]);
});

loadSample();
