/**
 * galaxy-view.js — raw-Three.js "Milky-Way" galaxy render mode (ADR-0135).
 *
 * A THIRD render mode for #canvas-wrap, alongside the 2D/3D 3d-force-graph
 * renderers. Ports the ACTUAL scene from docs/plans/viz-galaxy.mockup.html —
 * additive glow point-sprites, dual billboard core-glow halos, a 900-star
 * starfield, MiniOrbit auto-rotate, FogExp2, faint intra-arm LineSegments, and a
 * live oscilloscope control panel — instead of the #209 approach that only
 * ported node positions into 3d-force-graph (no glow/halo/starfield/theme).
 *
 * Design (repo convention: no browser render harness → the MATH is unit-tested
 * here, the GPU/render/picking layer is the user's smoke-check):
 *   - PURE, exported, testable-without-THREE functions:
 *       heatColorRGB, normalizeHeat, ageOf, buildNodeModel, mulberry32,
 *       layoutPositions, expRadius, sizeOf, GALAXY_DEFAULTS
 *     These are what galaxy-view.test.js covers.
 *   - THREE-dependent scene lives in the GalaxyScene class, which REUSES the
 *     already-loaded window.THREE r0.158 (do NOT load the mockup's r0.160 — a
 *     second THREE global clobbers the shared WebGL context).
 *
 * Data map (/api/graph → scene). The payload has gaps this module fills:
 *   - No per-node cluster_id → derive membership from clusters[].member_node_ids
 *     (a node in a cluster of >=2 members = ARM material; else CORE/single).
 *   - heat is already hard-capped [0,1] system-wide → clamp defensively, feed
 *     RAW to the ramp (mockup parity — no soft-saturation compression).
 *   - WIKI nodes carry no heat → colour by type tint (heat treated as ~0).
 *   - ENTITY nodes carry no age → age fallback 0.5.
 *
 * Car D #4: node-selection halo + search highlight. The halo is a world-space
 * billboard Sprite added to the scene at the picked node's diskPos (tracks the
 * camera orbit for free — the disk has no transform; spin rotates the camera).
 * ndcToScreen/haloScale (galaxy-halo.js) are the unit-tested projection + pulse
 * math; nodeScreenPos() projects a node world→screen ONCE at click so index.html
 * can anchor the floating popup near it.
 */

import { ndcToScreen, haloScale } from './galaxy-halo.js';

// ── constants (lifted from the mockup, R_MAX / R_CORE / R_SCALE / Z_LAYER) ──────
export const R_MAX = 46; // outer disk radius
export const R_CORE = 3.2; // core bulge radius scale
export const R_SCALE = 12.0; // exponential disk scale-length
export const Z_LAYER = 2.4; // per-type z offset when layering on

// Default camera pose (matches the GalaxyScene constructor). resetView() restores
// exactly this; fitView() reuses CAM_TARGET + a computed distance.
export const CAM_DEFAULT_POS = Object.freeze({ x: 0, y: 46, z: 72 });
export const CAM_TARGET = Object.freeze({ x: 0, y: 0, z: 0 });
export const CAM_FOV_DEG = 52; // PerspectiveCamera vertical FOV

/**
 * Camera distance that frames a disk of radius `rMax` inside the vertical FOV.
 * dist = (rMax * pad) / tan(fov/2). Pure — the exact `pad` is smoke-check-tunable.
 * @param {number} rMax   disk radius (world units)
 * @param {number} fovDeg vertical field of view (degrees)
 * @param {number} [pad]  headroom multiplier (>1 leaves margin around the disk)
 * @returns {number} camera distance from the target
 */
export function fitDistanceForDisk(rMax, fovDeg, pad = 1.15) {
  const half = (Math.max(1, fovDeg) * Math.PI) / 360; // (fov/2) in radians
  const t = Math.tan(half);
  const d = (rMax * pad) / (t > 1e-6 ? t : 1e-6);
  return Math.max(14, Math.min(320, d)); // clamp to MiniOrbit's wheel bounds
}

// live-tunable layout params (mirrors the mockup's `P`); GALAXY_DEFAULTS is the
// reset target.
export const GALAXY_DEFAULTS = Object.freeze({
  arms: 4,
  pitch: 0.3,
  radmode: 'heat', // 'heat' | 'age'
  thick: 0.9,
  bulge: 1.0,
  coredens: 1.0,
  single: 'core', // 'core' | 'halo'
  layer: 'off', // 'off' | 'on'  (per-type z-layering)
  edges: 'off', // 'off' | 'on'  (faint intra-arm edges)
  spin: 0.35,
});

// localStorage key for persisted layout params (Bug 1).
export const GALAXY_P_KEY = 'yadgar-galaxy-params';

// Per-control bounds — the CLAMP SOURCE for loadSavedP (NOT the DOM, so the
// clamp stays pure/testable). MUST mirror the GALAXY_CHROME_HTML slider
// min/max/step + segmented allowed values exactly; drift = silent corruption.
export const P_BOUNDS = Object.freeze({
  arms: { min: 2, max: 6, int: true },
  pitch: { min: 0.08, max: 0.75 },
  thick: { min: 0.1, max: 3.0 },
  coredens: { min: 0.3, max: 2.5 },
  bulge: { min: 0.2, max: 2.5 },
  spin: { min: 0, max: 2.0 },
  radmode: { enum: ['heat', 'age'] },
  single: { enum: ['core', 'halo'] },
  layer: { enum: ['off', 'on'] },
  edges: { enum: ['off', 'on'] },
});

/** Clamp/validate one param value against P_BOUNDS; returns null if unusable. */
function _clampParam(key, val) {
  const b = P_BOUNDS[key];
  if (!b) return null; // unknown key → drop
  if (b.enum) return b.enum.includes(val) ? val : null;
  let v = Number(val);
  if (!Number.isFinite(v)) return null;
  if (b.int) v = Math.round(v);
  return Math.min(b.max, Math.max(b.min, v));
}

/**
 * Load persisted layout params, merged over GALAXY_DEFAULTS. Pure + injectable
 * storage (overlays.js pattern) so private-mode throw + malformed JSON are
 * testable. Unknown keys dropped; each value clamped to its control's bounds;
 * any failure → clean GALAXY_DEFAULTS.
 *
 * @param {Storage} [storage] localStorage-compatible; defaults to window.localStorage
 */
export function loadSavedP(storage) {
  const P = { ...GALAXY_DEFAULTS };
  const store = storage ?? (typeof window !== 'undefined' ? window.localStorage : null);
  if (!store) return P;
  let raw;
  try {
    raw = store.getItem(GALAXY_P_KEY);
  } catch (_) {
    return P; // private-mode / access throw
  }
  if (!raw) return P;
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (_) {
    return P; // malformed JSON → defaults
  }
  if (!parsed || typeof parsed !== 'object') return P;
  for (const key of Object.keys(P_BOUNDS)) {
    if (!(key in parsed)) continue;
    const v = _clampParam(key, parsed[key]);
    if (v !== null) P[key] = v;
  }
  return P;
}

/**
 * Persist the current params (only known+clamped keys). Pure + injectable
 * storage; a private-mode setItem throw is swallowed (persistence is best-effort).
 *
 * @param {object} P live params
 * @param {Storage} [storage] localStorage-compatible; defaults to window.localStorage
 */
export function saveP(P, storage) {
  const store = storage ?? (typeof window !== 'undefined' ? window.localStorage : null);
  if (!store || !P) return;
  const out = {};
  for (const key of Object.keys(P_BOUNDS)) {
    const v = _clampParam(key, P[key]);
    if (v !== null) out[key] = v;
  }
  try {
    store.setItem(GALAXY_P_KEY, JSON.stringify(out));
  } catch (_) {
    /* private-mode / quota — best-effort */
  }
}

// Deterministic seed so a given (nodes, P) always yields the same layout — the
// determinism the vitest test pins. The mockup shared one rnd() stream across
// corpus + every relayout (non-deterministic); we reseed at layoutPositions()
// entry instead.
export const GALAXY_SEED = 0xbeef1234 | 0;

// #69: the cosmic-backdrop starfield drifts at ~this fraction of the galaxy's
// apparent auto-rotate speed, so the far shell trails slowly behind the faster-
// spinning disk (parallax / depth). Auto-rotate is a CAMERA orbit, so to make
// the starfield APPEAR to move at 0.25× we co-rotate the star object by
// (1 − 0.25) of the per-frame azimuth delta (cancelling 75% of the apparent
// motion). Tunable; the nebula dome stays fixed (see _frame).
export const BACKDROP_ROTATE_FACTOR = 0.25;

// ── deterministic PRNG (mulberry32, verbatim from the mockup) ──────────────────
export function mulberry32(a) {
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * Deterministic cosmic-backdrop starfield buffers (far shell, r 280..1380).
 *
 * Replaces the old dim 900-pt field that FogExp2 swallowed past r≈380 — this
 * shell is rendered fog-exempt + additive so the stars actually read on black.
 * Positions squash the vertical axis (*0.7) into a mild disk; per-vertex colours
 * are mostly cool-white with a ~12% warm minority. All draws come from a single
 * mulberry32(seed) stream, so the field is fully reproducible for the unit test.
 *
 * @param {number} n     star count
 * @param {number} seed  PRNG seed
 * @returns {{positions: Float32Array, colors: Float32Array}}
 */
export function buildStarfield(n = 3200, seed = GALAXY_SEED) {
  const rnd = mulberry32((seed ^ 0x51ce) | 0);
  const positions = new Float32Array(n * 3);
  const colors = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    const r = 280 + rnd() * 1100; // 280..1380 (< camera far plane 2000)
    const th = rnd() * Math.PI * 2;
    const ph = Math.acos(2 * rnd() - 1);
    positions[i * 3] = r * Math.sin(ph) * Math.cos(th);
    positions[i * 3 + 1] = r * Math.sin(ph) * Math.sin(th) * 0.7; // vertical squash
    positions[i * 3 + 2] = r * Math.cos(ph);
    const b = 0.45 + rnd() * 0.55; // brightness 0.45..1.0
    const warm = rnd() < 0.12;
    colors[i * 3] = b;
    colors[i * 3 + 1] = b * (warm ? 0.88 : 0.98);
    colors[i * 3 + 2] = warm ? b * 0.72 : b;
  }
  return { positions, colors };
}

// ── seamless nebula skydome shader (direction-based fbm → no UV seam) ────────────
// Additive on black so unlit directions add nothing (no grey haze); the camera
// only tilts, so the fixed-orientation dome parallaxes against the nearer stars.
const NEBULA_VERT = `
  varying vec3 vDir;
  void main() {
    vDir = normalize(position);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }`;
const NEBULA_FRAG = `
  precision highp float;
  varying vec3 vDir;
  uniform float uIntensity;
  vec3 hash3(vec3 p){ p = vec3(dot(p,vec3(127.1,311.7,74.7)),dot(p,vec3(269.5,183.3,246.1)),dot(p,vec3(113.5,271.9,124.6)));
    return -1.0 + 2.0*fract(sin(p)*43758.5453123); }
  float noise(vec3 p){ vec3 i=floor(p),f=fract(p),u=f*f*(3.0-2.0*f);
    return mix(mix(mix(dot(hash3(i+vec3(0,0,0)),f-vec3(0,0,0)),dot(hash3(i+vec3(1,0,0)),f-vec3(1,0,0)),u.x),
                   mix(dot(hash3(i+vec3(0,1,0)),f-vec3(0,1,0)),dot(hash3(i+vec3(1,1,0)),f-vec3(1,1,0)),u.x),u.y),
               mix(mix(dot(hash3(i+vec3(0,0,1)),f-vec3(0,0,1)),dot(hash3(i+vec3(1,0,1)),f-vec3(1,0,1)),u.x),
                   mix(dot(hash3(i+vec3(0,1,1)),f-vec3(0,1,1)),dot(hash3(i+vec3(1,1,1)),f-vec3(1,1,1)),u.x),u.y),u.z); }
  float fbm(vec3 p){ float v=0.0,a=0.5; for(int k=0;k<5;k++){ v+=a*noise(p); p*=2.02; a*=0.5; } return v; }
  void main(){
    vec3 d = normalize(vDir);
    float m = smoothstep(0.30, 0.85, fbm(d*2.4)*0.5+0.5);            // sparse mask → mostly black
    float t = fbm(d*5.0 + 11.0)*0.5+0.5;                             // colour mix
    vec3 col = mix(vec3(0.06,0.11,0.30), vec3(0.22,0.07,0.26), t);   // blue ↔ purple
    col += vec3(0.02,0.12,0.13) * smoothstep(0.55, 0.95, fbm(d*3.3-7.0)*0.5+0.5); // teal wisp
    gl_FragColor = vec4(col * m * uIntensity, 1.0);
  }`;

/** Box-Muller gaussian driven by a supplied rnd() (so it stays deterministic). */
function gaussWith(rnd) {
  let u = 0;
  let v = 0;
  while (u === 0) u = rnd();
  while (v === 0) v = rnd();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

// ── heat → clamped [0,1] ────────────────────────────────────────────────────────
/**
 * Heat is hard-capped [0,1] system-wide (thermodynamics.py / heat_decay.py /
 * DB maxh=1.0). Clamp defensively and pass the RAW value straight to the ramp —
 * mockup parity. The old h/(h+1) soft-saturation compressed [0,1]→[0,0.5],
 * making the upper colour ramp unreachable AND holding spiral arms out of the
 * core (drive=1-heat≥0.5). NaN / negative → 0; values above 1 → 1.
 */
export function normalizeHeat(h) {
  const v = Number(h);
  if (!Number.isFinite(v) || v <= 0) return 0;
  return Math.min(1, v);
}

// ── age derivation ─────────────────────────────────────────────────────────────
/**
 * Normalized age [0,1] for a node (0 = newest, 1 = oldest), from created_at /
 * last_accessed relative to a corpus time-window. Entities (and any node with
 * no usable timestamp) fall back to 0.5 — the ADR-0135 "ENTITY no age" gap.
 *
 * @param {object} node
 * @param {number} nowMs   reference "now" (ms epoch)
 * @param {number} spanMs  window over which age spreads 0→1 (older→1)
 */
export function ageOf(node, nowMs, spanMs) {
  const ts = node && (node.created_at ?? node.last_accessed);
  if (ts == null) return 0.5; // ENTITY / missing timestamp → mid fallback
  const t = typeof ts === 'number' ? ts : Date.parse(ts);
  if (!Number.isFinite(t)) return 0.5;
  const age = (nowMs - t) / spanMs;
  return Math.min(1, Math.max(0, age));
}

// ── heat colour ramp (cold-cyan → phosphor → amber → fault-red), pure RGB ──────
// Lifted from the mockup's heatColor, but returns a plain {r,g,b} in [0,1] so it
// is testable without THREE. The GalaxyScene copies these into THREE.Color.
const _RAMP = {
  COLD: { r: 0x0a / 255, g: 0x4a / 255, b: 0x6b / 255 },
  CYAN: { r: 0x3e / 255, g: 0xc9 / 255, b: 0xff / 255 },
  PHOS: { r: 0x49 / 255, g: 0xff / 255, b: 0xa4 / 255 },
  AMBER: { r: 0xff / 255, g: 0xc3 / 255, b: 0x5c / 255 },
  REDH: { r: 0xff / 255, g: 0x40 / 255, b: 0x59 / 255 },
};
function _lerp(a, b, t) {
  const tt = Math.min(1, Math.max(0, t));
  return { r: a.r + (b.r - a.r) * tt, g: a.g + (b.g - a.g) * tt, b: a.b + (b.b - a.b) * tt };
}
/**
 * heat (already normalized [0,1]) → {r,g,b} in [0,1]. Piecewise 4-segment ramp,
 * boundary-safe (h<=0 → COLD, h>=1 → REDH).
 */
export function heatColorRGB(h) {
  const x = Math.min(1, Math.max(0, Number(h) || 0));
  const R = _RAMP;
  if (x < 0.3) return _lerp(R.COLD, R.CYAN, x / 0.3);
  if (x < 0.6) return _lerp(R.CYAN, R.PHOS, (x - 0.3) / 0.3);
  if (x < 0.82) return _lerp(R.PHOS, R.AMBER, (x - 0.6) / 0.22);
  return _lerp(R.AMBER, R.REDH, (x - 0.82) / 0.18);
}

// subtle per-type hue nudge blended into the heat colour (mockup TYPE_TINT).
export const TYPE_TINT = Object.freeze({
  memory: { r: 0x49 / 255, g: 0xff / 255, b: 0xa4 / 255 },
  wiki: { r: 0x3e / 255, g: 0xc9 / 255, b: 0xff / 255 },
  entity: { r: 0x8f / 255, g: 0xb0 / 255, b: 0xa0 / 255 },
});

/** Normalise a graph node's raw type string to memory|wiki|entity. */
export function galaxyType(node) {
  const t = node && node.type ? String(node.type).toLowerCase() : '';
  if (t === 'wiki') return 'wiki';
  if (t === 'entity') return 'entity';
  return 'memory'; // memory + any heat-bearing / temporal type
}

// ── payload → stable node model ────────────────────────────────────────────────
/**
 * Build the stable backbone the scene, picking, filter-mask and heat-patch all
 * share. Derives cluster membership from clusters[].member_node_ids:
 *   - a node in a cluster of >=2 distinct members → ARM material (cluster>=0)
 *   - otherwise → CORE / single (cluster=-1, single=true)
 * Single-member "clusters" are demoted (not real clusters), matching the mockup.
 *
 * @param {{nodes?:Array, clusters?:Array}} payload  /api/graph response
 * @param {object} [opts] {nowMs, spanMs} for age; defaults to Date.now() / 180d.
 * @returns {{nodes:Array, idToIndex:Object, clusterStat:Array, armClusters:Array,
 *            counts:{total,core,arm,wiki,memory,entity}}}
 *   Each model node: {id,type,heat(normalized),age,cluster,single}
 */
export function buildNodeModel(payload, opts = {}) {
  const raw = (payload && payload.nodes) || [];
  const rawClusters = (payload && payload.clusters) || [];
  const nowMs = opts.nowMs != null ? opts.nowMs : Date.now();
  const spanMs = opts.spanMs != null ? opts.spanMs : 180 * 24 * 3600 * 1000; // ~180d

  // id → cluster index, from clusters[].member_node_ids. A cluster only counts
  // when it has >=2 DISTINCT member ids that actually exist in the node set.
  const idPresent = new Set(raw.map((n) => n && n.id));
  const idToCluster = Object.create(null);
  const clusterStat = [];
  for (let c = 0; c < rawClusters.length; c++) {
    const members = Array.from(new Set((rawClusters[c] && rawClusters[c].member_node_ids) || []))
      .filter((id) => idPresent.has(id));
    if (members.length < 2) continue; // single-member → not a real cluster
    const cid = clusterStat.length;
    for (const id of members) {
      if (!(id in idToCluster)) idToCluster[id] = cid; // first cluster wins (stable)
    }
    clusterStat.push({ id: cid, n: 0, heat: 0, score: 0 });
  }

  const nodes = new Array(raw.length);
  const idToIndex = Object.create(null);
  const counts = { total: raw.length, core: 0, arm: 0, wiki: 0, memory: 0, entity: 0 };

  for (let i = 0; i < raw.length; i++) {
    const rn = raw[i] || {};
    const type = galaxyType(rn);
    // WIKI carries no heat → treat as ~0 (coloured by type tint downstream).
    const heat = type === 'wiki' ? 0 : normalizeHeat(rn.heat);
    const age = ageOf(rn, nowMs, spanMs);
    const cluster = rn.id in idToCluster ? idToCluster[rn.id] : -1;
    const single = cluster < 0;
    nodes[i] = { id: rn.id, type, heat, age, cluster, single };
    idToIndex[rn.id] = i;
    counts[type]++;
    if (single) counts.core++;
    else counts.arm++;
    if (cluster >= 0) {
      const s = clusterStat[cluster];
      s.n++;
      s.heat += heat;
    }
  }
  // per-cluster score: size-weighted with a mean-heat kicker (mockup formula).
  for (const s of clusterStat) {
    s.score = s.n * 0.6 + (s.heat / (s.n || 1)) * s.n * 0.4;
  }
  const armClusters = clusterStat.slice().sort((a, b) => b.score - a.score);
  return { nodes, idToIndex, clusterStat, armClusters, counts };
}

/**
 * Balance the spiral arms by GREEDY LIGHTEST-ARM bin-packing (first-fit
 * decreasing). Round-robin by rank (`i % arms`) dumped the biggest clusters into
 * arms 0/1, so 2 arms held most of the nodes. Instead we walk the spine clusters
 * largest-first (they arrive score-sorted, which tracks node count) and drop each
 * onto the arm with the smallest running node-count load — ties go to the lowest
 * arm index for determinism (the layout determinism vitest depends on it).
 *
 * @param {Array<{id:number,n:number}>} spine  spine clusters, largest-first
 * @param {number} arms  arm count (P.arms)
 * @returns {Map<number, number>} clusterId → arm index
 */
export function assignArmsBalanced(spine, arms) {
  const out = new Map();
  const k = Math.max(1, arms | 0);
  const armLoad = new Array(k).fill(0);
  for (const c of spine || []) {
    if (!c) continue;
    // lightest arm; first (lowest index) wins ties for determinism.
    let best = 0;
    for (let a = 1; a < k; a++) {
      if (armLoad[a] < armLoad[best]) best = a;
    }
    out.set(c.id, best);
    armLoad[best] += (typeof c.n === 'number' ? c.n : 0);
  }
  return out;
}

// ── exponential-disk radius sampler (verbatim maths from the mockup) ────────────
/**
 * Inverse-CDF of r*e^{-r/L} truncated at rMax; `tight` adds an inward pull.
 * Deterministic given the supplied rnd().
 */
export function expRadius(rnd, L, rMax, tight) {
  const k = 1 - Math.exp(-rMax / L);
  let r = -L * Math.log(1 - rnd() * k);
  if (tight > 0) r *= Math.pow(rnd(), tight * 0.6);
  return Math.min(rMax, r);
}

// ── layout: node model + P → position buffer ──────────────────────────────────
/**
 * Compute {pos:Float32Array(N*3), coreCount, armCount, haloCount, armOfCluster}
 * for the given node model + live params P. Deterministic: reseeds a fresh
 * mulberry32(seed) at entry so identical (model, P) → identical positions.
 *
 * Port of the mockup's layout(): log-spiral arms for clustered nodes, a dense
 * spheroidal core bulge (or optional outer halo) for loose/single nodes,
 * exponential radial density, radmode heat|age biasing arm placement.
 *
 * @param {ReturnType<typeof buildNodeModel>} model
 * @param {object} P  live params (see GALAXY_DEFAULTS)
 * @param {number} [seed] PRNG seed (default GALAXY_SEED)
 */
export function layoutPositions(model, P, seed = GALAXY_SEED) {
  const rnd = mulberry32(seed | 0);
  const nodes = model.nodes;
  const N = nodes.length;
  const pos = new Float32Array(N * 3);

  // rank real clusters, assign spine clusters to arms by GREEDY lightest-arm
  // bin-packing (balanced node counts, not rank round-robin — which starved half
  // the arms); the rest scatter inter-arm (arm = -2 marker).
  const real = model.armClusters; // already score-sorted (largest-first), all n>=2
  const nCluster = model.clusterStat.length;
  const armOfCluster = new Array(nCluster).fill(-1);
  const nSpine = Math.min(real.length, P.arms * 3);
  const armMap = assignArmsBalanced(real.slice(0, nSpine), P.arms);
  for (let i = 0; i < nSpine; i++) armOfCluster[real[i].id] = armMap.get(real[i].id);
  for (let i = nSpine; i < real.length; i++) armOfCluster[real[i].id] = -2;

  const armBase = (i) => (i / P.arms) * Math.PI * 2;
  let coreCount = 0;
  let armCount = 0;
  let haloCount = 0;

  for (let i = 0; i < N; i++) {
    const nd = nodes[i];
    const loose = nd.single;
    const arm = loose ? -1 : armOfCluster[nd.cluster];
    const toHalo = loose && P.single === 'halo';
    const toCore = loose && !toHalo;
    let x;
    let y;
    let z;

    if (toCore) {
      const bulgeL = (R_SCALE * 0.42) / (0.6 + P.coredens * 0.9);
      const rr = expRadius(rnd, bulgeL, R_MAX * 0.55, 1.4 * P.bulge);
      const th = rnd() * Math.PI * 2;
      const ph = Math.acos(2 * rnd() - 1);
      x = rr * Math.sin(ph) * Math.cos(th);
      z = rr * Math.sin(ph) * Math.sin(th);
      y = rr * Math.cos(ph) * 0.62;
      coreCount++;
    } else if (toHalo) {
      const rr = R_CORE * 1.6 + Math.pow(rnd(), 0.55) * (R_MAX * 1.15);
      const th = rnd() * Math.PI * 2;
      const ph = Math.acos(2 * rnd() - 1);
      x = rr * Math.sin(ph) * Math.cos(th);
      z = rr * Math.sin(ph) * Math.sin(th);
      y = rr * Math.cos(ph) * 0.55;
      haloCount++;
    } else {
      const drive = P.radmode === 'heat' ? 1 - nd.heat : nd.age; // hot/young → inner
      let radius = expRadius(rnd, R_SCALE, R_MAX, 0);
      radius = radius * 0.55 + Math.pow(drive, 0.8) * R_MAX * 0.45;
      radius = Math.max(R_CORE, radius);
      let angle;
      if (arm >= 0) {
        angle = armBase(arm) + Math.log(radius / R_CORE + 1.0) / P.pitch;
        angle += gaussWith(rnd) * 0.16;
      } else {
        angle = rnd() * Math.PI * 2 + gaussWith(rnd) * 0.5;
      }
      x = Math.cos(angle) * radius;
      z = Math.sin(angle) * radius;
      const thick = P.thick * (0.35 + 0.65 * (radius / R_MAX));
      y = gaussWith(rnd) * thick;
      if (P.layer === 'on') {
        y += nd.type === 'memory' ? Z_LAYER : nd.type === 'wiki' ? 0 : -Z_LAYER;
      }
      armCount++;
    }
    pos[i * 3] = x;
    pos[i * 3 + 1] = y;
    pos[i * 3 + 2] = z;
  }
  return { pos, coreCount, armCount, haloCount, armOfCluster };
}

/**
 * Per-vertex size from heat (hot larger; loose core stars smaller/fainter;
 * clustered entities smaller). Pure — mirrors the mockup's size formula so the
 * scene and the size buffer stay in sync. Returns a size scalar (>0).
 */
export function sizeOf(nd) {
  let sz = 0.45 + nd.heat * nd.heat * 2.6;
  if (nd.single) sz *= 0.66; // core star (halo further reduced by the scene layer)
  if (nd.type === 'entity' && !nd.single) sz *= 0.55;
  if (nd.heat > 0.9) sz *= 1.35;
  return sz;
}

/**
 * Build the per-vertex visibility mask from a {id: visible} map. Missing id =
 * visible (1); explicit `false` = hidden (0). Pure — the scene's setVisible()
 * delegates here so the mask logic is vitest-covered (the render/discard is the
 * smoke-check). Order matches `nodes` (== the point-buffer vertex order).
 *
 * @param {Array<{id:*}>} nodes  model nodes (buildNodeModel().nodes)
 * @param {Object<string,boolean>} visById  id → visible; missing = visible
 * @returns {Uint8Array} 1=visible, 0=hidden, length == nodes.length
 */
export function visibilityMask(nodes, visById) {
  const arr = nodes || [];
  const map = visById || {};
  const mask = new Uint8Array(arr.length);
  for (let i = 0; i < arr.length; i++) {
    mask[i] = map[arr[i].id] === false ? 0 : 1;
  }
  return mask;
}

// ── edge rendering (#69): 2-class faint backdrop, bright-per-type on focus ──────
// The galaxy previously drew ONLY decorative intra-arm lines (one hardcoded
// colour, no node identity). #69 renders the REAL typed edges from the /api/graph
// payload as a single additive LineSegments. Colour is where the meaning lives:
//   - global backdrop → 2 subtle classes by edge ROLE
//       retrieval     (affects recall ranking) → warm, brighter
//       informational (stored structure)       → cool, dimmer
//     Neither fights node-heat brightness (both stay well below full white).
//   - on FOCUS (a node clicked/hovered) → that node's incident edges light up in
//     their full PER-TYPE colour; every other edge fades to the quiet backdrop.
// Under AdditiveBlending an RGB of (0,0,0) adds nothing → invisible, so a
// toggled-off type or a hidden-endpoint edge is painted black (not removed) and
// live-repaint is a pure colour-buffer rewrite — no geometry rebuild.

/** 2-class backdrop colours keyed by edge role. Dim so nodes stay dominant. */
export const EDGE_ROLE_COLOR = Object.freeze({
  // warm amber — retrieval edges are the load-bearing ones.
  retrieval: { r: 0.62, g: 0.42, b: 0.16 },
  // cool teal — informational structure, quieter.
  informational: { r: 0.17, g: 0.34, b: 0.40 },
});

/** Focus-mode per-type bright colours (0-1 rgb). Fallback = brightened role. */
export const EDGE_TYPE_COLOR = Object.freeze({
  transition: { r: 0.25, g: 0.72, b: 0.31 },
  co_occurrence: { r: 0.91, g: 0.72, b: 0.43 },
  resolved_by: { r: 0.97, g: 0.32, b: 0.29 },
  caused_by: { r: 1.0, g: 0.48, b: 0.45 },
  derived_from: { r: 0.22, g: 0.77, b: 0.81 },
  temporal: { r: 0.43, g: 0.25, b: 0.81 },
  wiki_crossref: { r: 0.82, g: 0.66, b: 1.0 },
  memory_wiki: { r: 1.0, g: 0.65, b: 0.34 },
  causal: { r: 0.75, g: 0.58, b: 0.79 },
  memory_similarity_link: { r: 0.35, g: 0.65, b: 1.0 },
});

/**
 * Resolve an edge endpoint (id string OR {id} node object) to a bare id.
 * Mirrors index.html's _npEndId — kept local so this module is self-contained.
 */
export function edgeEndId(end) {
  return end && end.id != null ? end.id : end;
}

/**
 * Classify a raw edge to 'retrieval' | 'informational'. Prefers an explicit
 * `role` on the wire (backend stamps it, graph_edges.py); falls back to a
 * type→role map for robustness (older payloads / lazy fetches).
 * @param {{role?:string,type?:string}} edge
 * @param {Object<string,string>} [typeRole]  type → role override map
 */
export function edgeRole(edge, typeRole) {
  if (edge && (edge.role === 'retrieval' || edge.role === 'informational')) return edge.role;
  const t = edge && edge.type;
  const m = typeRole && t != null ? typeRole[t] : undefined;
  return m === 'retrieval' ? 'retrieval' : 'informational';
}

/**
 * Build the GL segment buffers (positions + per-vertex colours) for the edge
 * LineSegments from the CURRENT render state. Pure — no THREE, no DOM — so the
 * colour policy is vitest-covered while the draw call stays a smoke-check.
 *
 * An edge contributes a 2-vertex segment iff BOTH endpoints resolve to a node
 * index. Its colour is decided by (in order):
 *   1. type toggled OFF  → black (invisible under additive)
 *   2. either endpoint hidden (visMask 0) → black
 *   3. focusId set AND edge incident to it → full per-type bright colour
 *   4. focusId set, edge NOT incident → heavily dimmed backdrop (recede)
 *   5. no focus → 2-class backdrop colour by role
 *
 * @param {object} args
 * @param {Array} args.edges          raw edges [{source,target,type,role}]
 * @param {Object<string,number>} args.idToIndex   node id → vertex index
 * @param {Float32Array} args.diskPos  node world positions (index*3)
 * @param {Uint8Array} [args.visMask]  per-index 1=visible/0=hidden (null=all vis)
 * @param {Object<string,boolean>} [args.toggleState]  edge type → shown (missing=shown)
 * @param {*} [args.focusId]           focused node id, or null
 * @param {Object<string,string>} [args.typeRole]  type→role fallback map
 * @returns {{positions:Float32Array, colors:Float32Array, count:number}}
 */
export function edgeSegments(args) {
  const {
    edges = [], idToIndex = {}, diskPos = new Float32Array(0),
    visMask = null, toggleState = {}, focusId = null, typeRole = {},
  } = args || {};
  const pos = [];
  const col = [];
  const BLACK = { r: 0, g: 0, b: 0 };
  // Non-incident edges recede further when a node is focused (backdrop * this).
  const UNFOCUS_DIM = 0.35;
  for (const e of edges) {
    const sId = edgeEndId(e.source);
    const tId = edgeEndId(e.target);
    const a = idToIndex[sId];
    const b = idToIndex[tId];
    if (a == null || b == null) continue; // orphan endpoint → skip
    pos.push(diskPos[a * 3], diskPos[a * 3 + 1], diskPos[a * 3 + 2]);
    pos.push(diskPos[b * 3], diskPos[b * 3 + 1], diskPos[b * 3 + 2]);

    let c;
    const type = e.type;
    const shown = type == null || toggleState[type] !== false;
    const endpointVisible = !visMask || (visMask[a] && visMask[b]);
    if (!shown || !endpointVisible) {
      c = BLACK;
    } else {
      const role = edgeRole(e, typeRole);
      const backdrop = EDGE_ROLE_COLOR[role] || EDGE_ROLE_COLOR.informational;
      if (focusId != null) {
        const incident = sId === focusId || tId === focusId;
        if (incident) {
          c = EDGE_TYPE_COLOR[type] || { r: backdrop.r * 1.8, g: backdrop.g * 1.8, b: backdrop.b * 1.8 };
        } else {
          c = { r: backdrop.r * UNFOCUS_DIM, g: backdrop.g * UNFOCUS_DIM, b: backdrop.b * UNFOCUS_DIM };
        }
      } else {
        c = backdrop;
      }
    }
    col.push(c.r, c.g, c.b, c.r, c.g, c.b);
  }
  return {
    positions: new Float32Array(pos),
    colors: new Float32Array(col),
    count: pos.length / 6,
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// THREE-DEPENDENT SCENE. Not exercised by vitest (no WebGL); the user smoke-check
// covers render / picking / teardown. Guarded so importing this module in jsdom
// (for the pure-fn tests) never touches window.THREE.
// ─────────────────────────────────────────────────────────────────────────────

class GalaxyScene {
  /**
   * @param {HTMLElement} container  #canvas-wrap
   * @param {object} deps  { onPick(node), payload }
   */
  constructor(container, deps) {
    const THREE = window.THREE;
    if (!THREE) throw new Error('galaxy-view: window.THREE (r0.158) not loaded');
    this.THREE = THREE;
    this.container = container;
    this.deps = deps || {};
    this.P = loadSavedP(); // Bug 1: restore persisted params (falls back to defaults)
    this._raf = null;
    this._visible = null; // Uint8Array mask (1=visible) or null=all visible
    this._focusId = null; // #69: focused node id → brighten its incident edges
    this._disposed = false;
    this._last = typeof performance !== 'undefined' ? performance.now() : Date.now();
    this._fpsAcc = 0;
    this._fpsN = 0;

    // named bound handlers (removable on teardown — the mockup used anonymous arrows)
    this._onResize = this._handleResize.bind(this);
    this._onPointerDown = this._handlePointerDown.bind(this);
    this._onPointerUp = this._handlePointerUp.bind(this);
    this._onPointerMove = this._handlePointerMove.bind(this);
    this._onWheel = this._handleWheel.bind(this);
    this._onClick = this._handleClick.bind(this);
    this._frame = this._frame.bind(this);

    this.model = buildNodeModel(deps.payload || { nodes: [], clusters: [] });
    this._buildDom();
    this._buildScene();
    this._buildPoints();
    this.relayout();
    this._raf = requestAnimationFrame(this._frame);
  }

  // ── DOM: canvas + panel + legend + hud + #atmos (galaxy-scoped chrome) ────────
  _buildDom() {
    const c = this.container;
    // load the companion stylesheet once (traces-tab.js pattern).
    if (!document.querySelector('link[href*="galaxy-view.css"]')) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = './galaxy-view.css';
      document.head.appendChild(link);
    }
    c.innerHTML = '';
    c.classList.add('galaxy-mode');

    this.canvas = document.createElement('canvas');
    this.canvas.className = 'galaxy-gl';
    c.appendChild(this.canvas);

    this.atmos = document.createElement('div');
    this.atmos.id = 'galaxy-atmos';
    c.appendChild(this.atmos);

    // chrome (control panel + legend + hud). innerHTML is static markup — the IDs
    // are galaxy-prefixed so they never collide with the FG overlays.
    this.chrome = document.createElement('div');
    this.chrome.className = 'galaxy-chrome';
    this.chrome.innerHTML = GALAXY_CHROME_HTML;
    c.appendChild(this.chrome);

    this._wireControls();
    this._syncCounts();
  }

  _buildScene() {
    const THREE = this.THREE;
    const w = this.container.clientWidth || window.innerWidth;
    const h = this.container.clientHeight || window.innerHeight;
    this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.renderer.setSize(w, h);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x000000); // pitch black — nebula/stars add over it
    this.scene.fog = new THREE.FogExp2(0x03070b, 0.01);

    this.camera = new THREE.PerspectiveCamera(52, w / h, 0.1, 2000);
    this.camera.position.set(0, 46, 72);
    this.camera.lookAt(0, 0, 0);
    this.controls = new MiniOrbit(THREE, this.camera, this.canvas, {
      down: this._onPointerDown,
      up: this._onPointerUp,
      move: this._onPointerMove,
      wheel: this._onWheel,
    });

    // ── cosmic backdrop: seamless nebula skydome + fog-exempt starfield ─────────
    // The old dim 900-pt field sat inside FogExp2's reach (r≥380 → fog factor ≈ 1)
    // and read as flat black. Both layers below are fog:false + additive so they
    // paint on the pitch-black scene background; the dome is a direction-based fbm
    // shader (no UV seam) and, since the camera only tilts, its fixed orientation
    // parallaxes against the nearer star shell.
    this.nebulaGeo = new THREE.SphereGeometry(1600, 48, 32);
    this.nebulaMat = new THREE.ShaderMaterial({
      side: THREE.BackSide,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      fog: false,
      uniforms: { uIntensity: { value: 0.55 } },
      vertexShader: NEBULA_VERT,
      fragmentShader: NEBULA_FRAG,
    });
    this.nebula = new THREE.Mesh(this.nebulaGeo, this.nebulaMat);
    this.scene.add(this.nebula);

    const sf = buildStarfield(3200, GALAXY_SEED);
    this.starGeo = new THREE.BufferGeometry();
    this.starGeo.setAttribute('position', new THREE.BufferAttribute(sf.positions, 3));
    this.starGeo.setAttribute('color', new THREE.BufferAttribute(sf.colors, 3));
    this.starMat = new THREE.PointsMaterial({
      size: 1.5,
      sizeAttenuation: false,
      vertexColors: true,
      transparent: true,
      opacity: 0.95,
      fog: false,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this.starfield = new THREE.Points(this.starGeo, this.starMat);
    this.scene.add(this.starfield);

    // dual core-glow billboards (additive).
    this.sprite = this._makeSpriteTexture();
    this.coreGlowMat = new THREE.SpriteMaterial({
      map: this._makeSpriteTexture(),
      color: 0x9effd0,
      transparent: true,
      opacity: 0.55,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this.coreGlow = new THREE.Sprite(this.coreGlowMat);
    this.coreGlow.scale.set(34, 34, 1);
    this.scene.add(this.coreGlow);
    this.coreGlow2Mat = new THREE.SpriteMaterial({
      map: this._makeSpriteTexture(),
      color: 0xffd98a,
      transparent: true,
      opacity: 0.32,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    this.coreGlow2 = new THREE.Sprite(this.coreGlow2Mat);
    this.coreGlow2.scale.set(15, 15, 1);
    this.scene.add(this.coreGlow2);

    this.edgeLines = null; // #69: built from payload edges on relayout (_buildEdges)
    this._edgeColAttr = null;
    this._edgePosAttr = null;
    window.addEventListener('resize', this._onResize);
    this.canvas.addEventListener('click', this._onClick);
  }

  _makeSpriteTexture() {
    const THREE = this.THREE;
    const s = 64;
    const cv = document.createElement('canvas');
    cv.width = s;
    cv.height = s;
    const g = cv.getContext('2d');
    const grad = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
    grad.addColorStop(0, 'rgba(255,255,255,1)');
    grad.addColorStop(0.35, 'rgba(255,255,255,0.85)');
    grad.addColorStop(0.7, 'rgba(255,255,255,0.18)');
    grad.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = grad;
    g.fillRect(0, 0, s, s);
    const tex = new THREE.CanvasTexture(cv);
    tex.needsUpdate = true;
    return tex;
  }

  _buildPoints() {
    const THREE = this.THREE;
    const N = this.model.nodes.length;
    this.diskGeo = new THREE.BufferGeometry();
    this.diskPos = new Float32Array(N * 3);
    this.diskCol = new Float32Array(N * 3);
    this.diskSz = new Float32Array(N);
    this.diskGeo.setAttribute('position', new THREE.BufferAttribute(this.diskPos, 3));
    this.diskGeo.setAttribute('color', new THREE.BufferAttribute(this.diskCol, 3));
    this.diskGeo.setAttribute('size', new THREE.BufferAttribute(this.diskSz, 1));
    this.pointMat = new THREE.ShaderMaterial({
      uniforms: { uTex: { value: this.sprite }, uPix: { value: this.renderer.getPixelRatio() } },
      vertexShader:
        'attribute float size; varying vec3 vCol; varying float vSize; uniform float uPix;' +
        'void main(){ vCol=color; vSize=size; vec4 mv=modelViewMatrix*vec4(position,1.0);' +
        'gl_PointSize = size * uPix * (300.0 / -mv.z); gl_Position = projectionMatrix*mv; }',
      // Hidden verts carry size=0. WebGL clamps gl_PointSize to >=1 (spec
      // ALIASED_POINT_SIZE_RANGE), so a size=0 point still rasterises a 1px
      // additive dot — the discard below drops it (Bug 3).
      fragmentShader:
        'uniform sampler2D uTex; varying vec3 vCol; varying float vSize;' +
        'void main(){ if(vSize<=0.0) discard;' +
        'vec4 t=texture2D(uTex, gl_PointCoord); if(t.a<0.02) discard;' +
        'gl_FragColor=vec4(vCol, t.a); }',
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      vertexColors: true,
    });
    this.diskPoints = new THREE.Points(this.diskGeo, this.pointMat);
    this.scene.add(this.diskPoints);
    this._raycaster = new THREE.Raycaster();
    this._raycaster.params.Points = { threshold: 1.2 };
  }

  // ── relayout: recompute positions + colours + sizes from model + P ────────────
  relayout() {
    if (this._disposed) return;
    const THREE = this.THREE;
    const model = this.model;
    const N = model.nodes.length;
    const { pos, coreCount, armOfCluster } = layoutPositions(model, this.P);
    const armHue = (i) => (i / this.P.arms - 0.5) * 0.1;
    const _c = new THREE.Color();
    const _white = new THREE.Color(0xffffff);
    for (let i = 0; i < N; i++) {
      const nd = model.nodes[i];
      this.diskPos[i * 3] = pos[i * 3];
      this.diskPos[i * 3 + 1] = pos[i * 3 + 1];
      this.diskPos[i * 3 + 2] = pos[i * 3 + 2];
      const arm = nd.single ? -1 : armOfCluster[nd.cluster];
      const toHalo = nd.single && this.P.single === 'halo';
      const toCore = nd.single && !toHalo;
      const rgb = heatColorRGB(nd.heat);
      _c.setRGB(rgb.r, rgb.g, rgb.b);
      if (arm >= 0 && !toHalo && !toCore) _c.offsetHSL(armHue(arm), 0, 0);
      const tint = TYPE_TINT[nd.type];
      _c.lerp(new THREE.Color(tint.r, tint.g, tint.b), toHalo ? 0.35 : toCore ? 0.2 : 0.14);
      if (nd.heat > 0.82) _c.lerp(_white, ((nd.heat - 0.82) / 0.18) * 0.4);
      this.diskCol[i * 3] = _c.r;
      this.diskCol[i * 3 + 1] = _c.g;
      this.diskCol[i * 3 + 2] = _c.b;
      let sz = sizeOf(nd);
      if (toHalo) sz = (0.45 + nd.heat * nd.heat * 2.6) * 0.42; // halo further faded
      this.diskSz[i] = sz;
    }
    this._baseSz = Float32Array.from(this.diskSz); // remember so setVisible can restore
    this._applyVisibilityMask(); // re-apply filter masks wiped by the size rewrite
    this.diskGeo.attributes.position.needsUpdate = true;
    this.diskGeo.attributes.color.needsUpdate = true;
    this.diskGeo.attributes.size.needsUpdate = true;

    // core glow tracks bulge + core-density.
    const coreI = 0.3 + 0.22 * Math.min(2, this.P.bulge) + 0.18 * Math.min(2, this.P.coredens);
    this.coreGlowMat.opacity = this.P.single === 'halo' ? coreI * 0.5 : coreI;
    this.coreGlow.scale.setScalar(22 + 10 * this.P.bulge + 6 * this.P.coredens);
    this.coreGlow2Mat.opacity = this.P.single === 'halo' ? 0.16 : 0.34;

    this._buildEdges();
    this._coreCount = coreCount;
    this._syncCounts();
  }

  // ── real typed edges (#69) ────────────────────────────────────────────────────
  // #69 replaced the decorative intra-arm lines with the REAL /api/graph edges,
  // rendered as ONE additive LineSegments with per-vertex colours. Geometry is
  // built here (rebuilt only when node positions change on relayout); colour is a
  // separate _repaintEdges() pass so node-vis / type-toggle / focus changes are a
  // cheap colour-buffer rewrite with NO geometry rebuild (edgeSegments is pure +
  // vitest-covered). One draw call; positions are static (physics-frozen galaxy).
  _buildEdges() {
    const THREE = this.THREE;
    if (this.edgeLines) {
      this.scene.remove(this.edgeLines);
      this.edgeLines.geometry.dispose();
      this.edgeLines.material.dispose();
      this.edgeLines = null;
    }
    const edges = (this.deps.payload && this.deps.payload.edges) || [];
    if (!edges.length) return;
    const { positions, colors } = edgeSegments({
      edges,
      idToIndex: this.model.idToIndex,
      diskPos: this.diskPos,
      visMask: this._visible,
      toggleState: this.deps.edgeToggleState || {},
      focusId: this._focusId != null ? this._focusId : null,
      typeRole: this.deps.edgeTypeRole || {},
    });
    if (!positions.length) return;
    const g = new THREE.BufferGeometry();
    this._edgePosAttr = new THREE.BufferAttribute(positions, 3);
    this._edgeColAttr = new THREE.BufferAttribute(colors, 3);
    g.setAttribute('position', this._edgePosAttr);
    g.setAttribute('color', this._edgeColAttr);
    const m = new THREE.LineBasicMaterial({
      transparent: true,
      opacity: 0.9, // colour magnitude carries the "faintness"; additive on black
      blending: THREE.AdditiveBlending,
      vertexColors: true,
      depthWrite: false,
    });
    this.edgeLines = new THREE.LineSegments(g, m);
    this.scene.add(this.edgeLines);
  }

  // Colour-only repaint (no geometry rebuild). Cheap: rewrites the colour buffer
  // from current visibility + type-toggle + focus state via the pure edgeSegments
  // helper (positions are identical, so we reuse them). Called from setVisible,
  // setEdgeToggleState, and setFocus.
  _repaintEdges() {
    if (this._disposed || !this.edgeLines || !this._edgeColAttr) return;
    const edges = (this.deps.payload && this.deps.payload.edges) || [];
    if (!edges.length) return;
    const { colors } = edgeSegments({
      edges,
      idToIndex: this.model.idToIndex,
      diskPos: this.diskPos,
      visMask: this._visible,
      toggleState: this.deps.edgeToggleState || {},
      focusId: this._focusId != null ? this._focusId : null,
      typeRole: this.deps.edgeTypeRole || {},
    });
    if (colors.length !== this._edgeColAttr.array.length) return; // stale — skip
    this._edgeColAttr.array.set(colors);
    this._edgeColAttr.needsUpdate = true;
  }

  /** Update the edge type→shown map and repaint (no geometry rebuild). */
  setEdgeToggleState(toggleState) {
    this.deps.edgeToggleState = toggleState || {};
    this._repaintEdges();
  }

  /** Set (or clear, with null) the focused node → brighten its incident edges. */
  setFocus(nodeId) {
    this._focusId = nodeId != null ? nodeId : null;
    this._repaintEdges();
  }

  // ── filter visibility: per-vertex size=0 to hide (shares the model backbone) ──
  /** @param {Object<string,boolean>} visById  id → visible; missing id = visible. */
  setVisible(visById) {
    if (this._disposed) return;
    const mask = visibilityMask(this.model.nodes, visById);
    this._visible = mask;
    this._applyVisibilityMask();
    this.diskGeo.attributes.size.needsUpdate = true;
    this._repaintEdges(); // #69: hidden-endpoint edges recede (painted black)
    let n = 0;
    for (let i = 0; i < mask.length; i++) n += mask[i];
    this._setHud('galaxy-h-nodes', n);
  }

  _applyVisibilityMask() {
    if (!this._visible || !this._baseSz) return;
    for (let i = 0; i < this.diskSz.length; i++) {
      this.diskSz[i] = this._visible[i] ? this._baseSz[i] : 0;
    }
  }

  // ── live heat patch (deferred v1 in the plan, but the hook exists) ────────────
  patchHeat(updates) {
    if (this._disposed || !updates) return 0;
    const map = Object.create(null);
    for (const u of updates) if (u && u.id != null) map[u.id] = u.heat;
    let patched = 0;
    for (const nd of this.model.nodes) {
      if (nd.id in map && nd.type !== 'wiki') {
        nd.heat = normalizeHeat(map[nd.id]);
        patched++;
      }
    }
    if (patched > 0) this.relayout();
    return patched;
  }

  // ── Car D #4: node-selection halo (world-space billboard Sprite) ──────────────
  // The halo is placed at the node's diskPos world coords. Because the disk has
  // NO transform (added at origin) and spin rotates the CAMERA, the halo tracks
  // the node as the camera orbits. Pulses via haloScale() in _frame.
  _ensureHaloSprite() {
    if (this.haloSprite || this._disposed) return;
    const THREE = this.THREE;
    // radial-gradient ring texture on a canvas → additive sprite.
    const cv = document.createElement('canvas');
    cv.width = cv.height = 64;
    const ctx = cv.getContext('2d');
    const g = ctx.createRadialGradient(32, 32, 10, 32, 32, 30);
    g.addColorStop(0, 'rgba(63,208,201,0)');
    g.addColorStop(0.72, 'rgba(63,208,201,0)');
    g.addColorStop(0.82, 'rgba(63,208,201,0.85)');
    g.addColorStop(1, 'rgba(63,208,201,0)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 64, 64);
    this.haloTex = new THREE.CanvasTexture(cv);
    this.haloMat = new THREE.SpriteMaterial({
      map: this.haloTex,
      transparent: true,
      depthWrite: false,
      depthTest: false,
      blending: THREE.AdditiveBlending,
    });
    this.haloSprite = new THREE.Sprite(this.haloMat);
    this.haloSprite.visible = false;
    this.haloSprite.renderOrder = 999;
    this.scene.add(this.haloSprite);
  }

  /** Show the pulsing selection halo at node `id`; hides if not found. */
  showHalo(id) {
    if (this._disposed) return;
    this._ensureHaloSprite();
    const idx = this.model.idToIndex[id];
    if (idx == null) {
      this.hideHalo();
      return;
    }
    this._haloBase = Math.max(3, (this._baseSz ? this._baseSz[idx] : 1) * 3.2);
    this.haloSprite.position.set(
      this.diskPos[idx * 3],
      this.diskPos[idx * 3 + 1],
      this.diskPos[idx * 3 + 2],
    );
    this.haloSprite.visible = true;
    this._haloId = id;
    this.resume(); // ensure the RAF is running so the halo pulses
  }

  hideHalo() {
    this._haloId = null;
    if (this.haloSprite) this.haloSprite.visible = false;
  }

  /**
   * Project node `id`'s world position to screen pixel coords (once, at click).
   * @returns {{x,y,onscreen}|null}
   */
  nodeScreenPos(id) {
    if (this._disposed) return null;
    const idx = this.model.idToIndex[id];
    if (idx == null) return null;
    const THREE = this.THREE;
    const v = new THREE.Vector3(
      this.diskPos[idx * 3],
      this.diskPos[idx * 3 + 1],
      this.diskPos[idx * 3 + 2],
    );
    v.project(this.camera);
    const rect = this.canvas.getBoundingClientRect();
    const scr = ndcToScreen(v.x, v.y, rect.width, rect.height, v.z);
    return { x: rect.left + scr.x, y: rect.top + scr.y, onscreen: scr.onscreen };
  }

  /**
   * Search highlight: brighten matched nodes, dim the rest by scaling per-vertex
   * colour. idSet empty/null → restore full colour (relayout recomputes diskCol).
   * @param {Set<string>|null} idSet
   */
  highlight(idSet) {
    if (this._disposed) return;
    if (!idSet || idSet.size === 0) {
      this.relayout(); // restore pristine colours
      return;
    }
    // relayout first to get pristine base colours, then scale the dimmed ones.
    this.relayout();
    const nodes = this.model.nodes;
    for (let i = 0; i < nodes.length; i++) {
      if (idSet.has(nodes[i].id)) continue;
      this.diskCol[i * 3] *= 0.18;
      this.diskCol[i * 3 + 1] *= 0.18;
      this.diskCol[i * 3 + 2] *= 0.18;
    }
    this.diskGeo.attributes.color.needsUpdate = true;
  }

  // ── picking: Raycaster(Points) → idToIndex → onPick(node) ─────────────────────
  _handleClick(ev) {
    if (this._disposed || !this.deps.onPick) return;
    const THREE = this.THREE;
    const rect = this.canvas.getBoundingClientRect();
    const ndc = new THREE.Vector2(
      ((ev.clientX - rect.left) / rect.width) * 2 - 1,
      -((ev.clientY - rect.top) / rect.height) * 2 + 1,
    );
    this._raycaster.setFromCamera(ndc, this.camera);
    const hits = this._raycaster.intersectObject(this.diskPoints, false);
    // pick the nearest hit whose vertex is visible (size>0).
    for (const hit of hits) {
      const idx = hit.index;
      if (idx == null) continue;
      if (this._visible && !this._visible[idx]) continue;
      const model = this.model.nodes[idx];
      if (!model) continue;
      const raw = (this.deps.payload && this.deps.payload.nodes) || [];
      const rawNode =
        raw[idx] && raw[idx].id === model.id
          ? raw[idx]
          : raw.find((r) => r && r.id === model.id) || { id: model.id, type: model.type };
      this.deps.onPick(rawNode);
      return;
    }
  }

  // ── control-panel wiring (LIVE → mutate P + relayout; debounced sliders) ──────
  _wireControls() {
    const root = this.chrome;
    const q = (sel) => root.querySelector(sel);
    // Bug 1 ordering trap: push restored P INTO the DOM before binding, so the
    // bind-time apply() below reads back the restored values — not the static
    // HTML defaults (which would clobber the restored P).
    this._syncControlsToP();
    let debTimer = null;
    const debouncedRelayout = () => {
      if (debTimer) clearTimeout(debTimer);
      debTimer = setTimeout(() => this.relayout(), 60);
    };

    const bindSlider = (id, key, fmt) => {
      const el = q('#g-' + id);
      const out = q('#gv-' + id);
      if (!el) return;
      const apply = () => {
        this.P[key] = key === 'arms' ? parseInt(el.value, 10) : parseFloat(el.value);
        if (out) out.textContent = fmt ? fmt(this.P[key]) : this.P[key];
      };
      el.addEventListener('input', () => {
        apply();
        saveP(this.P); // Bug 1: persist — BEFORE the spin early-return so spin persists too
        if (key === 'spin') return; // spin only affects the RAF, not the layout
        debouncedRelayout();
      });
      apply();
    };
    bindSlider('arms', 'arms', (v) => v);
    bindSlider('pitch', 'pitch', (v) => v.toFixed(2));
    bindSlider('thick', 'thick', (v) => v.toFixed(1));
    bindSlider('coredens', 'coredens', (v) => v.toFixed(1));
    bindSlider('bulge', 'bulge', (v) => v.toFixed(1));
    bindSlider('spin', 'spin', (v) => v.toFixed(2));

    const bindSeg = (id, key) => {
      const box = q('#g-' + id);
      if (!box) return;
      box.addEventListener('click', (e) => {
        const b = e.target.closest('button');
        if (!b) return;
        box.querySelectorAll('button').forEach((x) => x.classList.remove('on'));
        b.classList.add('on');
        this.P[key] = b.dataset.v;
        saveP(this.P); // Bug 1: persist segmented choice
        this.relayout();
      });
    };
    bindSeg('radmode', 'radmode');
    bindSeg('single', 'single');
    bindSeg('layer', 'layer');
    // #69: 'edges' seg-control removed (real edges render unconditionally). The
    // P.edges param is kept for back-compat persistence but no longer wired.

    const resetBtn = q('#g-reset');
    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        this.P = { ...GALAXY_DEFAULTS };
        saveP(this.P); // Bug 1: persist the reset
        this._syncControlsToP();
        this.relayout();
      });
    }
  }

  _syncControlsToP() {
    const root = this.chrome;
    const q = (sel) => root.querySelector(sel);
    const setS = (id, v, fmt) => {
      const el = q('#g-' + id);
      const out = q('#gv-' + id);
      if (el) el.value = v;
      if (out) out.textContent = fmt ? fmt(v) : v;
    };
    setS('arms', this.P.arms, (v) => v);
    setS('pitch', this.P.pitch, (v) => v.toFixed(2));
    setS('thick', this.P.thick, (v) => v.toFixed(1));
    setS('coredens', this.P.coredens, (v) => v.toFixed(1));
    setS('bulge', this.P.bulge, (v) => v.toFixed(1));
    setS('spin', this.P.spin, (v) => v.toFixed(2));
    const setSeg = (id, v) => {
      const box = q('#g-' + id);
      if (!box) return;
      box.querySelectorAll('button').forEach((x) => x.classList.toggle('on', x.dataset.v === v));
    };
    setSeg('radmode', this.P.radmode);
    setSeg('single', this.P.single);
    setSeg('layer', this.P.layer);
    // #69: 'edges' seg-control removed from the panel (see _wireControls).
  }

  _syncCounts() {
    this._setHud('galaxy-h-arms', this.P.arms);
    if (this._coreCount != null) this._setHud('galaxy-h-core', this._coreCount);
    const c = this.model.counts;
    // #69: the STRUCTURE/NODES legend moved OUT of the galaxy chrome into the
    // unified left panel (index.html). Node-type counts are handed out via
    // deps.onCounts so index.html owns that DOM (Architecture B — galaxy renders,
    // index.html controls). The .galaxy-hud bottom status bar STAYS in chrome and
    // keeps using _setHud (this.chrome-scoped) below.
    if (typeof this.deps.onCounts === 'function') {
      try { this.deps.onCounts({ ...c, clusters: this.model.clusterStat.length }); } catch (_) { /* panel not wired */ }
    }
    this._setHud('galaxy-h-clusters', this.model.clusterStat.length);
    if (this._visible == null) this._setHud('galaxy-h-nodes', c.total);
  }

  _setHud(id, val) {
    const el = this.chrome && this.chrome.querySelector('#' + id);
    if (el) el.textContent = String(val);
  }

  // ── RAF (auto-rotate + render); pausable for idle/tab-away ────────────────────
  _frame(now) {
    if (this._disposed) return;
    const dt = Math.min(0.05, (now - this._last) / 1000);
    this._last = now;
    const azDelta = -this.P.spin * dt * 0.4; // Bug 12: auto-rotate direction
    this.controls.addAzimuth(azDelta);
    this.controls.update();
    // #69 backdrop parallax: co-rotate the starfield so it APPEARS to drift at
    // BACKDROP_ROTATE_FACTOR of the disk's auto-rotate speed. Camera orbits by
    // azDelta; rotating the star object the same way by (1 − factor) cancels
    // that fraction of the apparent motion. Nebula dome left fixed (no seam).
    if (this.starfield) {
      this.starfield.rotation.y += azDelta * (1 - BACKDROP_ROTATE_FACTOR);
    }
    // Car D #4: pulse the selection halo (world-space; tracks camera orbit).
    if (this.haloSprite && this.haloSprite.visible && this._haloBase) {
      const s = haloScale(this._haloBase, now);
      this.haloSprite.scale.set(s, s, s);
    }
    this.renderer.render(this.scene, this.camera);
    this._fpsAcc += dt;
    this._fpsN++;
    if (this._fpsAcc > 0.5) {
      this._setHud('galaxy-h-fps', Math.round(this._fpsN / this._fpsAcc));
      this._fpsAcc = 0;
      this._fpsN = 0;
    }
    this._raf = requestAnimationFrame(this._frame);
  }

  pause() {
    if (this._raf != null) {
      cancelAnimationFrame(this._raf);
      this._raf = null;
    }
  }

  resume() {
    if (this._raf == null && !this._disposed) {
      this._last = typeof performance !== 'undefined' ? performance.now() : Date.now();
      this._raf = requestAnimationFrame(this._frame);
    }
  }

  _handleResize() {
    if (this._disposed) return;
    const w = this.container.clientWidth || window.innerWidth;
    const h = this.container.clientHeight || window.innerHeight;
    // Bail on a 0-sized container (e.g. a resize fired while #canvas-wrap's tab is
    // display:none) — aspect 0/0 = NaN would break the projection until reload.
    if (!w || !h) return;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  }

  // Public: nudge a resize (used when returning to the home tab, since no window
  // resize fires on tab switch and the scene may have mounted / resized at 0×0).
  resize() {
    this._handleResize();
  }

  // ── Fit / Reset camera (toolbar ⊞ Fit / ⟳ Reset — wired in index.html) ─────────
  // Both write MiniOrbit's {theta,phi,radius,target}, NOT camera.position: the RAF
  // loop calls controls.update() every frame, which recomputes camera.position
  // from that state — setting camera.position directly would be clobbered next tick.

  // Frame the whole galaxy disk (R_MAX) centred at the origin. Keeps the current
  // azimuth (theta) so Fit doesn't feel like a jarring re-orient; only re-centres,
  // re-tilts to a pleasant top-down-ish phi, and pulls the distance to fit.
  fitView() {
    if (!this.controls) return;
    this.controls.target.set(CAM_TARGET.x, CAM_TARGET.y, CAM_TARGET.z);
    this.controls.radius = fitDistanceForDisk(R_MAX, CAM_FOV_DEG);
    // phi derived from the default pose's vertical angle so the tilt reads natural.
    const defR = Math.hypot(CAM_DEFAULT_POS.x, CAM_DEFAULT_POS.y, CAM_DEFAULT_POS.z);
    this.controls.phi = Math.acos(Math.min(1, Math.max(-1, CAM_DEFAULT_POS.y / (defR || 1))));
    this.controls.update();
  }

  // Restore the exact default pose (recenter target + reset azimuth/phi/distance so
  // any drifted auto-rotation offset is cleared). Auto-rotate resumes from here.
  resetView() {
    if (!this.controls) return;
    this.controls.target.set(CAM_TARGET.x, CAM_TARGET.y, CAM_TARGET.z);
    const dx = CAM_DEFAULT_POS.x - CAM_TARGET.x;
    const dy = CAM_DEFAULT_POS.y - CAM_TARGET.y;
    const dz = CAM_DEFAULT_POS.z - CAM_TARGET.z;
    this.controls.radius = Math.sqrt(dx * dx + dy * dy + dz * dz);
    this.controls.theta = Math.atan2(dx, dz);
    this.controls.phi = Math.acos(Math.min(1, Math.max(-1, dy / (this.controls.radius || 1))));
    this.controls.update();
  }

  // MiniOrbit forwards pointer/wheel here purely so teardown can removeEventListener
  // named handlers; the actual orbit maths live in MiniOrbit.
  _handlePointerDown(e) {
    this.controls._down(e);
  }

  _handlePointerUp(e) {
    this.controls._up(e);
  }

  _handlePointerMove(e) {
    this.controls._move(e);
  }

  _handleWheel(e) {
    this.controls._wheel(e);
  }

  // ── teardown: dispose ALL GPU resources + remove listeners (WebGL ~16 ceiling) ─
  destroy() {
    if (this._disposed) return;
    this._disposed = true;
    this.pause();
    // remove listeners (named handlers → removable, unlike the mockup's arrows).
    window.removeEventListener('resize', this._onResize);
    if (this.canvas) this.canvas.removeEventListener('click', this._onClick);
    if (this.controls) this.controls.dispose();

    const disp = (o) => {
      try {
        if (o && o.dispose) o.dispose();
      } catch (_) {
        /* ignore */
      }
    };
    // geometries
    disp(this.diskGeo);
    disp(this.starGeo);
    disp(this.nebulaGeo);
    if (this.edgeLines) {
      disp(this.edgeLines.geometry);
      disp(this.edgeLines.material);
    }
    // materials
    disp(this.pointMat);
    disp(this.starMat);
    disp(this.nebulaMat); // ShaderMaterial — no texture map to dispose
    disp(this.coreGlowMat);
    disp(this.coreGlow2Mat);
    // textures (3 CanvasTextures: standalone sprite + 2 core-glow maps)
    disp(this.sprite);
    disp(this.coreGlowMat && this.coreGlowMat.map);
    disp(this.coreGlow2Mat && this.coreGlow2Mat.map);
    // Car D #4: selection-halo GPU resources
    disp(this.haloTex);
    disp(this.haloMat);
    // renderer — release the WebGL context so the ~16-context ceiling doesn't fill
    if (this.renderer) {
      disp(this.renderer);
      try {
        this.renderer.forceContextLoss();
      } catch (_) {
        /* ignore */
      }
    }
    // DOM — remove all galaxy chrome + canvas + #atmos (innerHTML='' clears every
    // child of canvas-wrap, chrome/atmos/canvas included).
    if (this.container) {
      this.container.classList.remove('galaxy-mode');
      this.container.innerHTML = '';
    }
    this.scene = null;
    this.diskPoints = null;
    this.starfield = null;
    this.nebula = null;
    this.coreGlow = null;
    this.coreGlow2 = null;
    this.edgeLines = null;
    this._edgeColAttr = null;
    this._edgePosAttr = null;
    this.haloSprite = null;
    this.haloMat = null;
    this.haloTex = null;
  }
}

// ── inline OrbitControls (MiniOrbit) — verbatim maths from the mockup, but with
//    externally-supplied named handlers so teardown can removeEventListener. ─────
class MiniOrbit {
  constructor(THREE, cam, dom, handlers) {
    this.cam = cam;
    this.dom = dom;
    this.handlers = handlers;
    this.target = new THREE.Vector3(0, 0, 0);
    this.theta = Math.atan2(cam.position.x - this.target.x, cam.position.z - this.target.z);
    const dx = cam.position.x - this.target.x;
    const dy = cam.position.y - this.target.y;
    const dz = cam.position.z - this.target.z;
    this.radius = Math.sqrt(dx * dx + dy * dy + dz * dz);
    this.phi = Math.acos(Math.min(1, Math.max(-1, dy / this.radius)));
    this.dragging = false;
    this.px = 0;
    this.py = 0;
    dom.addEventListener('pointerdown', handlers.down);
    dom.addEventListener('pointerup', handlers.up);
    dom.addEventListener('pointermove', handlers.move);
    dom.addEventListener('wheel', handlers.wheel, { passive: false });
  }

  _down(e) {
    this.dragging = true;
    this.px = e.clientX;
    this.py = e.clientY;
    try {
      this.dom.setPointerCapture(e.pointerId);
    } catch (_) {
      /* ignore */
    }
  }

  _up() {
    this.dragging = false;
  }

  _move(e) {
    if (!this.dragging) return;
    const dxp = e.clientX - this.px;
    const dyp = e.clientY - this.py;
    this.px = e.clientX;
    this.py = e.clientY;
    this.theta -= dxp * 0.005;
    this.phi = Math.min(Math.PI - 0.05, Math.max(0.05, this.phi - dyp * 0.005));
  }

  _wheel(e) {
    e.preventDefault();
    this.radius = Math.min(320, Math.max(14, this.radius * (1 + Math.sign(e.deltaY) * 0.08)));
  }

  addAzimuth(d) {
    this.theta += d;
  }

  update() {
    const s = Math.sin(this.phi) * this.radius;
    this.cam.position.set(
      this.target.x + s * Math.sin(this.theta),
      this.target.y + Math.cos(this.phi) * this.radius,
      this.target.z + s * Math.cos(this.theta),
    );
    this.cam.lookAt(this.target);
  }

  dispose() {
    this.dom.removeEventListener('pointerdown', this.handlers.down);
    this.dom.removeEventListener('pointerup', this.handlers.up);
    this.dom.removeEventListener('pointermove', this.handlers.move);
    this.dom.removeEventListener('wheel', this.handlers.wheel);
  }
}

// ── control-panel markup (galaxy-prefixed IDs; styles live in index.html CSS) ──
const GALAXY_CHROME_HTML = `
<div class="galaxy-panel">
  <div class="galaxy-panel-hd"><span class="galaxy-led"></span><h1>LAYOUT CONTROLS</h1></div>
  <div class="galaxy-panel-body">
    <div class="galaxy-ctl">
      <div class="galaxy-ctl-lbl">Spiral arms <span class="galaxy-val" id="gv-arms">4</span></div>
      <input type="range" id="g-arms" min="2" max="6" step="1" value="4">
    </div>
    <div class="galaxy-ctl">
      <div class="galaxy-ctl-lbl">Spiral tightness (pitch) <span class="galaxy-val" id="gv-pitch">0.30</span></div>
      <input type="range" id="g-pitch" min="0.08" max="0.75" step="0.01" value="0.30">
    </div>
    <div class="galaxy-ctl">
      <div class="galaxy-ctl-lbl">Arm radius mapping</div>
      <div class="galaxy-seg" id="g-radmode">
        <button data-v="heat" class="on">Heat</button><button data-v="age">Age</button>
      </div>
    </div>
    <div class="galaxy-ctl">
      <div class="galaxy-ctl-lbl">Disk thickness <span class="galaxy-val" id="gv-thick">0.9</span></div>
      <input type="range" id="g-thick" min="0.1" max="3.0" step="0.05" value="0.9">
    </div>
    <div class="galaxy-divider"></div>
    <div class="galaxy-ctl">
      <div class="galaxy-ctl-lbl">Loose / single nodes</div>
      <div class="galaxy-seg cy" id="g-single">
        <button data-v="core" class="on">Dense core</button><button data-v="halo">Outer halo</button>
      </div>
    </div>
    <div class="galaxy-ctl">
      <div class="galaxy-ctl-lbl">Core density <span class="galaxy-val" id="gv-coredens">1.0</span></div>
      <input type="range" id="g-coredens" min="0.3" max="2.5" step="0.05" value="1.0">
    </div>
    <div class="galaxy-ctl">
      <div class="galaxy-ctl-lbl">Core-bulge size / glow <span class="galaxy-val" id="gv-bulge">1.0</span></div>
      <input type="range" id="g-bulge" min="0.2" max="2.5" step="0.05" value="1.0">
    </div>
    <div class="galaxy-divider"></div>
    <div class="galaxy-ctl">
      <div class="galaxy-ctl-lbl">Type z-layering</div>
      <div class="galaxy-seg cy" id="g-layer">
        <button data-v="off" class="on">Off</button><button data-v="on">On</button>
      </div>
    </div>
    <!-- #69: the "Faint intra-arm edges" Off/On control was removed. Edges now
         render the REAL typed graph (2-class colour, faint backdrop, bright on
         focus) unconditionally, and edge visibility is owned by the EDGES section
         of the left panel — a second on/off here would just confuse. -->
    <div class="galaxy-divider"></div>
    <div class="galaxy-ctl">
      <div class="galaxy-ctl-lbl">Auto-rotate speed <span class="galaxy-val" id="gv-spin">0.35</span></div>
      <input type="range" id="g-spin" min="0" max="2.0" step="0.05" value="0.35">
    </div>
    <div class="galaxy-note">
      Positions recompute live from the corpus — no reload.
      <a class="galaxy-reset" id="g-reset">reset defaults</a>
    </div>
  </div>
</div>
<!-- #69: the STRUCTURE / NODE-TYPES legend moved OUT of the galaxy chrome into
     the always-on unified left panel (#galaxy-side-panel in index.html), which
     folds STRUCTURE + NODES + HEAT FILTER + EDGES into one collapsible-section
     panel. Node-type counts flow to it via deps.onCounts. The .galaxy-hud bottom
     status bar below STAYS here (untouched). -->
<div class="galaxy-hud">
  <div class="galaxy-st"><span class="galaxy-k">nodes</span><b id="galaxy-h-nodes">—</b></div>
  <div class="galaxy-st"><span class="galaxy-k">core</span><b id="galaxy-h-core">—</b></div>
  <div class="galaxy-st"><span class="galaxy-k">arms</span><b id="galaxy-h-arms">4</b></div>
  <div class="galaxy-st"><span class="galaxy-k">clusters</span><b id="galaxy-h-clusters">—</b></div>
  <div class="galaxy-st"><span class="galaxy-k">fps</span><b id="galaxy-h-fps">—</b></div>
</div>`;

// ── module-level singleton + public API (window._galaxyView) ────────────────────
let _scene = null;

/**
 * Mount the galaxy scene into `container`, replacing whatever was there.
 * @param {HTMLElement} container  #canvas-wrap
 * @param {object} deps  { onPick(node), payload:{nodes,clusters} }
 */
export function mount(container, deps) {
  if (_scene) destroy();
  if (!container) return null;
  _scene = new GalaxyScene(container, deps || {});
  return _scene;
}

export function destroy() {
  if (_scene) {
    _scene.destroy();
    _scene = null;
  }
}

export function setVisible(visById) {
  if (_scene) _scene.setVisible(visById || {});
}

export function patchHeat(updates) {
  return _scene ? _scene.patchHeat(updates) : 0;
}

export function relayout(payload) {
  if (!_scene) return;
  if (payload) {
    // node set changed → rebuild model + point buffers, then relayout.
    _scene.deps.payload = payload;
    _scene.model = buildNodeModel(payload);
    _scene._visible = null;
    if (_scene.diskPoints) _scene.scene.remove(_scene.diskPoints);
    if (_scene.diskGeo) _scene.diskGeo.dispose();
    if (_scene.pointMat) _scene.pointMat.dispose();
    _scene._buildPoints();
  }
  _scene.relayout();
}

export function pause() {
  if (_scene) _scene.pause();
}

export function resume() {
  if (_scene) _scene.resume();
}

export function resize() {
  if (_scene) _scene.resize();
}

export function isMounted() {
  return _scene != null;
}

// ── Car D #4: selection halo + search highlight + screen-projection ───────────
export function showHalo(id) {
  if (_scene) _scene.showHalo(id);
}

export function hideHalo() {
  if (_scene) _scene.hideHalo();
}

export function nodeScreenPos(id) {
  return _scene ? _scene.nodeScreenPos(id) : null;
}

export function highlight(idSet) {
  if (_scene) _scene.highlight(idSet);
}

// ── #69: edge type-toggle + focus (colour-only repaint, no geometry rebuild) ───
export function setEdgeToggleState(toggleState) {
  if (_scene) _scene.setEdgeToggleState(toggleState);
}

export function setFocus(nodeId) {
  if (_scene) _scene.setFocus(nodeId);
}

// ── toolbar camera controls (⊞ Fit / ⟳ Reset) ────────────────────────────────
export function fitView() {
  if (_scene) _scene.fitView();
}

export function resetView() {
  if (_scene) _scene.resetView();
}

// Expose on window so index.html's plain <script> (non-module) can drive it.
if (typeof window !== 'undefined') {
  window._galaxyView = {
    mount, destroy, setVisible, patchHeat, relayout, pause, resume, resize, isMounted,
    showHalo, hideHalo, nodeScreenPos, highlight, fitView, resetView,
    setEdgeToggleState, setFocus,
  };
}
