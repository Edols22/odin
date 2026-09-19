import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { createIcons, Maximize, RotateCcw, Download } from 'lucide';

const meta = JSON.parse(document.getElementById('map-metadata').textContent);
function floats(id) {
  const bytes = Uint8Array.from(atob(document.getElementById(id).textContent.trim()), c => c.charCodeAt(0));
  return new Float32Array(bytes.buffer);
}
const positions = floats('map-points'), trajectory = floats('map-trajectory');
const host = document.getElementById('scene');
const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
renderer.setClearColor(0x171b1c);
host.appendChild(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.OrthographicCamera(-20, 20, 15, -15, .01, 1000);
camera.up.set(0, 0, 1);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = false;
const lo = new THREE.Vector3(...meta.bounds[0]), hi = new THREE.Vector3(...meta.bounds[1]);
const center = lo.clone().add(hi).multiplyScalar(.5), extent = hi.clone().sub(lo);
const geometry = new THREE.BufferGeometry();
geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
const colors = new Float32Array(positions.length);
const color = new THREE.Color();
for (let i = 0; i < positions.length / 3; i++) {
  const h = (positions[i * 3 + 2] - lo.z) / extent.z;
  color.setHSL(.57 - .50 * h, .72, .62);
  colors.set([color.r, color.g, color.b], i * 3);
}
geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
const clip = new THREE.Plane(new THREE.Vector3(0, 0, -1), hi.z + .01);
renderer.localClippingEnabled = true;
const material = new THREE.PointsMaterial({ size: 2, sizeAttenuation: false, vertexColors: true, clippingPlanes: [clip] });
scene.add(new THREE.Points(geometry, material));
const pathGeometry = new THREE.BufferGeometry();
pathGeometry.setAttribute('position', new THREE.BufferAttribute(trajectory, 3));
const path = new THREE.Line(pathGeometry, new THREE.LineBasicMaterial({ color: 0xff5ca8, depthTest: false }));
path.renderOrder = 2;
scene.add(path);
const origin = new THREE.AxesHelper(1.2);
scene.add(origin);
const grid = new THREE.GridHelper(40, 40, 0x525b5b, 0x303738);
grid.rotation.x = Math.PI / 2;
grid.position.set(center.x, center.y, lo.z - .03);
scene.add(grid);
const marker = new THREE.Mesh(new THREE.SphereGeometry(.10, 10, 8), new THREE.MeshBasicMaterial({ color: 0xff5ca8 }));
marker.position.fromArray(trajectory);
scene.add(marker);
function render() { renderer.render(scene, camera); }
controls.addEventListener('change', render);
let preset = 'perspective';
function fit(mode = preset) {
  preset = mode;
  const aspect = host.clientWidth / host.clientHeight;
  const radius = extent.length() * .5;
  const halfHeight = radius * 1.08 / Math.min(1, aspect);
  camera.left = -halfHeight * aspect; camera.right = halfHeight * aspect;
  camera.top = halfHeight; camera.bottom = -halfHeight;
  camera.zoom = 1;
  const direction = mode === 'top' ? new THREE.Vector3(0, 0, 1)
    : mode === 'front' ? new THREE.Vector3(0, -1, .0001) : new THREE.Vector3(1, -1.4, 1.3).normalize();
  camera.position.copy(center).addScaledVector(direction, radius * 3 + 10);
  camera.up.set(0, mode === 'top' ? 1 : 0, mode === 'top' ? 0 : 1);
  controls.target.copy(center); camera.updateProjectionMatrix(); controls.update(); render();
  document.querySelectorAll('[data-view]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.view === mode)));
}
function resize() { renderer.setSize(host.clientWidth, host.clientHeight); fit(); }
new ResizeObserver(resize).observe(host);
document.querySelectorAll('[data-view]').forEach(button => button.onclick = () => fit(button.dataset.view));
document.getElementById('fit').onclick = () => fit();
document.getElementById('reset').onclick = () => { ceiling.value = hi.z.toFixed(2); clip.constant = hi.z + .01; ceilingLabel.value = ceiling.value + ' m'; fit('perspective'); };
document.getElementById('grid').onchange = event => { grid.visible = event.target.checked; render(); };
document.getElementById('trajectory').onchange = event => { path.visible = marker.visible = event.target.checked; render(); };
document.getElementById('point-size').oninput = event => { material.size = Number(event.target.value); render(); };
const ceiling = document.getElementById('ceiling'), ceilingLabel = document.getElementById('ceiling-value');
ceiling.min = lo.z; ceiling.max = hi.z; ceiling.value = hi.z;
ceilingLabel.value = hi.z.toFixed(2) + ' m';
ceiling.oninput = () => { clip.constant = Number(ceiling.value); ceilingLabel.value = Number(ceiling.value).toFixed(2) + ' m'; render(); };
document.getElementById('filename').textContent = meta.source.split('/').pop();
document.getElementById('count').textContent = `${meta.points.toLocaleString()} points / ${meta.keyframes} keyframes`;
document.getElementById('extent').textContent = `${extent.x.toFixed(1)} x ${extent.y.toFixed(1)} x ${extent.z.toFixed(1)} m`;
createIcons({ icons: { Maximize, RotateCcw, Download } });
window.mapView = { count: positions.length / 3, keyframes: meta.keyframes, renderer, camera, scene, controls, render };
fit();
