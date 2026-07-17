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

  // rank real clusters, assign to arms round-robin (spine budget arms*3); the
  // rest scatter inter-arm (arm = -2 marker).
  const real = model.armClusters; // already score-sorted, all n>=2
  const nCluster = model.clusterStat.length;
  const armOfCluster = new Array(nCluster).fill(-1);
  const nSpine = Math.min(real.length, P.arms * 3);
  for (let i = 0; i < nSpine; i++) armOfCluster[real[i].id] = i % P.arms;
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

    // starfield backdrop (900 far points; deterministic).
    const rnd = mulberry32(GALAXY_SEED ^ 0x51ce);
    const n = 900;
    const p = new Float32Array(n * 3);
    for (let i = 0; i < n; i++) {
      const r = 380 + rnd() * 520;
      const th = rnd() * Math.PI * 2;
      const ph = Math.acos(2 * rnd() - 1);
      p[i * 3] = r * Math.sin(ph) * Math.cos(th);
      p[i * 3 + 1] = r * Math.sin(ph) * Math.sin(th) * 0.6;
      p[i * 3 + 2] = r * Math.cos(ph);
    }
    this.starGeo = new THREE.BufferGeometry();
    this.starGeo.setAttribute('position', new THREE.BufferAttribute(p, 3));
    this.starMat = new THREE.PointsMaterial({
      color: 0x2a4a5c,
      size: 1.1,
      sizeAttenuation: false,
      transparent: true,
      opacity: 0.55,
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

    this.edgeLines = null; // built by relayout when P.edges==='on'
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

    this._buildEdges(armOfCluster);
    this._coreCount = coreCount;
    this._syncCounts();
  }

  _buildEdges(armOfCluster) {
    const THREE = this.THREE;
    if (this.edgeLines) {
      this.scene.remove(this.edgeLines);
      this.edgeLines.geometry.dispose();
      this.edgeLines.material.dispose();
      this.edgeLines = null;
    }
    if (this.P.edges !== 'on') return;
    const rnd = mulberry32(GALAXY_SEED ^ 0xed6e);
    const perArm = Array.from({ length: this.P.arms }, () => []);
    const nodes = this.model.nodes;
    for (let i = 0; i < nodes.length; i++) {
      const nd = nodes[i];
      if (nd.single) continue;
      const arm = armOfCluster[nd.cluster];
      if (arm < 0) continue;
      if (nd.heat < 0.35) continue;
      const x = this.diskPos[i * 3];
      const z = this.diskPos[i * 3 + 2];
      perArm[arm].push({ i, r: Math.hypot(x, z) });
    }
    const segs = [];
    for (const arr of perArm) {
      arr.sort((a, b) => a.r - b.r);
      for (let k = 0; k < arr.length - 1; k++) {
        if (rnd() > 0.5) continue;
        const a = arr[k].i;
        const b = arr[k + 1].i;
        if (arr[k + 1].r - arr[k].r > 6) continue;
        segs.push(
          this.diskPos[a * 3], this.diskPos[a * 3 + 1], this.diskPos[a * 3 + 2],
          this.diskPos[b * 3], this.diskPos[b * 3 + 1], this.diskPos[b * 3 + 2],
        );
      }
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(segs, 3));
    const m = new THREE.LineBasicMaterial({
      color: 0x1d6b48,
      transparent: true,
      opacity: 0.22,
      blending: THREE.AdditiveBlending,
    });
    this.edgeLines = new THREE.LineSegments(g, m);
    this.scene.add(this.edgeLines);
  }

  // ── filter visibility: per-vertex size=0 to hide (shares the model backbone) ──
  /** @param {Object<string,boolean>} visById  id → visible; missing id = visible. */
  setVisible(visById) {
    if (this._disposed) return;
    const mask = visibilityMask(this.model.nodes, visById);
    this._visible = mask;
    this._applyVisibilityMask();
    this.diskGeo.attributes.size.needsUpdate = true;
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
    bindSeg('edges', 'edges');

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
    setSeg('edges', this.P.edges);
  }

  _syncCounts() {
    this._setHud('galaxy-h-arms', this.P.arms);
    if (this._coreCount != null) this._setHud('galaxy-h-core', this._coreCount);
    const c = this.model.counts;
    this._setHud('galaxy-lg-memory', c.memory);
    this._setHud('galaxy-lg-wiki', c.wiki);
    this._setHud('galaxy-lg-entity', c.entity);
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
    this.controls.addAzimuth(-this.P.spin * dt * 0.4); // Bug 12: auto-rotate direction
    this.controls.update();
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
    if (this.edgeLines) {
      disp(this.edgeLines.geometry);
      disp(this.edgeLines.material);
    }
    // materials
    disp(this.pointMat);
    disp(this.starMat);
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
    this.coreGlow = null;
    this.coreGlow2 = null;
    this.edgeLines = null;
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
    <div class="galaxy-ctl">
      <div class="galaxy-ctl-lbl">Faint intra-arm edges</div>
      <div class="galaxy-seg cy" id="g-edges">
        <button data-v="off" class="on">Off</button><button data-v="on">On</button>
      </div>
    </div>
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
<div class="galaxy-legend">
  <h2>Structure</h2>
  <div class="galaxy-lg-row" style="color:#ffc35c"><span class="galaxy-lg-dot"></span><span style="color:var(--viz-tx-1,#9fc0ae)">core = loose / single nodes</span></div>
  <div class="galaxy-lg-row" style="color:#49ffa4"><span class="galaxy-lg-dot"></span><span style="color:var(--viz-tx-1,#9fc0ae)">arms = real clusters</span></div>
  <h2 style="margin-top:13px">Node types</h2>
  <div class="galaxy-lg-row" style="color:#49ffa4"><span class="galaxy-lg-dot"></span><span style="color:var(--viz-tx-1,#9fc0ae)">memory</span><span class="galaxy-ct" id="galaxy-lg-memory">—</span></div>
  <div class="galaxy-lg-row" style="color:#3ec9ff"><span class="galaxy-lg-dot"></span><span style="color:var(--viz-tx-1,#9fc0ae)">wiki</span><span class="galaxy-ct" id="galaxy-lg-wiki">—</span></div>
  <div class="galaxy-lg-row" style="color:#8fb0a0"><span class="galaxy-lg-dot"></span><span style="color:var(--viz-tx-1,#9fc0ae)">entity</span><span class="galaxy-ct" id="galaxy-lg-entity">—</span></div>
  <div class="galaxy-heatbar"></div>
  <div class="galaxy-heat-lbls"><span>cold</span><span>heat → brightness</span><span>hot</span></div>
</div>
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

// Expose on window so index.html's plain <script> (non-module) can drive it.
if (typeof window !== 'undefined') {
  window._galaxyView = {
    mount, destroy, setVisible, patchHeat, relayout, pause, resume, resize, isMounted,
    showHalo, hideHalo, nodeScreenPos, highlight,
  };
}
