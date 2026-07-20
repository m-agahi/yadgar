/**
 * galaxy-view.test.js — pure-function unit tests for the galaxy render mode.
 *
 * Covers the MATH only (repo convention: no browser render harness). The
 * THREE-dependent GalaxyScene / render / picking / teardown are the user's
 * smoke-check. Importing galaxy-view.js in jsdom is safe: the module only
 * touches window.THREE inside GalaxyScene, never at import time.
 *
 * Pinned here (per the plan's Surface-1 test list):
 *   - heatColorRGB boundaries + heat normalization
 *   - payload → node-model incl single/loose derivation
 *   - cluster → arm (member_count>=2 = arm; <2 demoted to core)
 *   - layoutPositions() ranges + determinism
 *   - idToIndex backbone
 */

import { describe, it, expect } from 'vitest';
import {
  heatColorRGB,
  normalizeHeat,
  ageOf,
  galaxyType,
  buildNodeModel,
  layoutPositions,
  buildDiskPositions,
  applyServedRelayout,
  expRadius,
  sizeOf,
  visibilityMask,
  loadSavedP,
  saveP,
  mulberry32,
  GALAXY_DEFAULTS,
  GALAXY_P_KEY,
  P_BOUNDS,
  GALAXY_SEED,
  R_MAX,
  assignArmsBalanced,
  fitDistanceForDisk,
  CAM_FOV_DEG,
  buildStarfield,
  edgeEndId,
  edgeRole,
  edgeSegments,
  edgeMaterialState,
  EDGE_ROLE_COLOR,
  EDGE_TYPE_COLOR,
} from './galaxy-view.js';

// Minimal in-memory localStorage-compatible stub (overlays.js test pattern).
function makeStore(initial = {}) {
  const m = new Map(Object.entries(initial));
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    _map: m,
  };
}

// ── normalizeHeat: heat is hard-capped [0,1] → clamp only, NO compression ───────
// Bug 2+6: the old h/(h+1) soft-saturation mapped [0,1]→[0,0.5], starving the
// upper colour ramp (Bug 2) AND holding arms out of the core via drive=1-heat
// (Bug 6). Fixed to a defensive clamp that feeds raw bounded heat to the ramp.
describe('normalizeHeat', () => {
  it('passes through bounded heat unchanged (0.2/0.6/1.0)', () => {
    expect(normalizeHeat(0.2)).toBeCloseTo(0.2, 10);
    expect(normalizeHeat(0.6)).toBeCloseTo(0.6, 10);
    expect(normalizeHeat(1.0)).toBeCloseTo(1.0, 10);
  });
  it('maps 0 → 0', () => {
    expect(normalizeHeat(0)).toBe(0);
  });
  it('clamps out-of-range >1 heat down to 1 (defensive)', () => {
    expect(normalizeHeat(2)).toBe(1);
    expect(normalizeHeat(1000)).toBe(1);
  });
  it('clamps NaN / negative / undefined / null to 0', () => {
    expect(normalizeHeat(NaN)).toBe(0);
    expect(normalizeHeat(-5)).toBe(0);
    expect(normalizeHeat(undefined)).toBe(0);
    expect(normalizeHeat(null)).toBe(0);
  });

  // DISCRIMINATING test (plan Surface-1). The Bug-2+6 defect was that heat=1.0
  // (the system-wide max) mapped to 0.5 under h/(h+1) — the TOP HALF of the
  // colour ramp was unreachable and drive=1-heat never fell below 0.5. The
  // guard: the hottest bounded heat must reach the TOP of the ramp, and the
  // {0.2,0.6,1.0} corpus must land its max ABOVE the old 0.5 ceiling. A bare
  // span check is NOT discriminating — the old raw span was already 0.667.
  it('hottest bounded heat (1.0) reaches the top of the [0,1] ramp', () => {
    // OLD h/(h+1): 1.0 → 0.5 (fails). NEW clamp: 1.0 → 1.0.
    expect(normalizeHeat(1.0)).toBeGreaterThan(0.95);
  });
  it('a {0.2,0.6,1.0} corpus reaches above the old 0.5 compression ceiling', () => {
    const vals = [0.2, 0.6, 1.0].map((h) => normalizeHeat(h));
    // every value must be its RAW self, not compressed below it
    expect(vals[0]).toBeGreaterThan(0.15);
    expect(vals[1]).toBeGreaterThan(0.5); // 0.6 raw > 0.5; OLD gave 0.375 (fails)
    expect(vals[2]).toBeGreaterThan(0.95); // 1.0 raw; OLD gave 0.5 (fails)
  });
});

// ── heatColorRGB: 4-segment ramp, boundary-safe ─────────────────────────────────
describe('heatColorRGB', () => {
  const inUnit = (c) => c.r >= 0 && c.r <= 1 && c.g >= 0 && c.g <= 1 && c.b >= 0 && c.b <= 1;

  it('h<=0 returns COLD (0a4a6b)', () => {
    const c = heatColorRGB(0);
    expect(c.r).toBeCloseTo(0x0a / 255, 6);
    expect(c.g).toBeCloseTo(0x4a / 255, 6);
    expect(c.b).toBeCloseTo(0x6b / 255, 6);
  });
  it('h>=1 returns fault-red (ff4059)', () => {
    const c = heatColorRGB(1);
    expect(c.r).toBeCloseTo(0xff / 255, 6);
    expect(c.g).toBeCloseTo(0x40 / 255, 6);
    expect(c.b).toBeCloseTo(0x59 / 255, 6);
  });
  it('clamps out-of-range input (negative → COLD, >1 → RED)', () => {
    expect(heatColorRGB(-3).b).toBeCloseTo(0x6b / 255, 6);
    expect(heatColorRGB(5).r).toBeCloseTo(0xff / 255, 6);
  });
  it('segment boundaries at 0.30 / 0.60 / 0.82 stay in [0,1] and continuous', () => {
    for (const h of [0, 0.15, 0.3, 0.45, 0.6, 0.71, 0.82, 0.9, 1]) {
      expect(inUnit(heatColorRGB(h))).toBe(true);
    }
    // continuity: just-below vs just-above a boundary differ only slightly
    const near = (h) => heatColorRGB(h);
    const d = (a, b) => Math.abs(a.r - b.r) + Math.abs(a.g - b.g) + Math.abs(a.b - b.b);
    expect(d(near(0.2999), near(0.3001))).toBeLessThan(0.02);
    expect(d(near(0.5999), near(0.6001))).toBeLessThan(0.02);
    expect(d(near(0.8199), near(0.8201))).toBeLessThan(0.02);
  });
});

// ── ageOf: entity/missing → 0.5 fallback; timestamps → clamped [0,1] ────────────
describe('ageOf', () => {
  const now = Date.parse('2026-07-17T00:00:00Z');
  const span = 180 * 24 * 3600 * 1000;
  it('missing timestamp (entity gap) → 0.5', () => {
    expect(ageOf({ type: 'entity' }, now, span)).toBe(0.5);
    expect(ageOf({}, now, span)).toBe(0.5);
  });
  it('unparseable timestamp → 0.5', () => {
    expect(ageOf({ created_at: 'not-a-date' }, now, span)).toBe(0.5);
  });
  it('now → age ~0, one span ago → age ~1, clamped', () => {
    expect(ageOf({ created_at: now }, now, span)).toBeCloseTo(0, 6);
    expect(ageOf({ created_at: now - span }, now, span)).toBeCloseTo(1, 6);
    expect(ageOf({ created_at: now - 10 * span }, now, span)).toBe(1); // clamped
    expect(ageOf({ created_at: now + span }, now, span)).toBe(0); // future clamped
  });
  it('falls back to last_accessed when created_at absent', () => {
    expect(ageOf({ last_accessed: now - span }, now, span)).toBeCloseTo(1, 6);
  });
});

// ── galaxyType normalization ────────────────────────────────────────────────────
describe('galaxyType', () => {
  it('maps wiki/entity explicitly, everything else → memory', () => {
    expect(galaxyType({ type: 'wiki' })).toBe('wiki');
    expect(galaxyType({ type: 'entity' })).toBe('entity');
    expect(galaxyType({ type: 'memory' })).toBe('memory');
    expect(galaxyType({ type: 'temporal' })).toBe('memory');
    expect(galaxyType({})).toBe('memory');
    expect(galaxyType({ type: 'WIKI' })).toBe('wiki'); // case-insensitive
  });
});

// ── buildNodeModel: cluster derivation + single/loose + idToIndex + heat/age gaps ─
describe('buildNodeModel', () => {
  const payload = {
    nodes: [
      { id: 'mem:1', type: 'memory', heat: 1.0, created_at: 1 },
      { id: 'mem:2', type: 'memory', heat: 3.0, created_at: 1 },
      { id: 'wiki:1', type: 'wiki' }, // no heat
      { id: 'ent:1', type: 'entity', heat: 0.2 }, // no age
      { id: 'ent:2', type: 'entity', heat: 0.0 }, // loose singleton
      { id: 'mem:3', type: 'memory', heat: 0.5, created_at: 1 },
    ],
    clusters: [
      { member_node_ids: ['mem:1', 'mem:2', 'wiki:1'] }, // real cluster (>=2)
      { member_node_ids: ['ent:1'] }, // single-member → demoted to core
      { member_node_ids: ['mem:3', 'missing:99'] }, // one real member → demoted
    ],
  };
  const m = buildNodeModel(payload, { nowMs: 1000, spanMs: 1000 });

  it('derives an ARM cluster only for >=2 present members', () => {
    // only the first cluster (mem:1, mem:2, wiki:1) survives → clusterStat length 1
    expect(m.clusterStat.length).toBe(1);
    const byId = Object.fromEntries(m.nodes.map((n) => [n.id, n]));
    expect(byId['mem:1'].cluster).toBe(0);
    expect(byId['mem:1'].single).toBe(false);
    expect(byId['mem:2'].single).toBe(false);
    expect(byId['wiki:1'].single).toBe(false);
  });
  it('demotes single-member and one-present-member clusters to loose/core', () => {
    const byId = Object.fromEntries(m.nodes.map((n) => [n.id, n]));
    expect(byId['ent:1'].single).toBe(true); // single-member cluster
    expect(byId['ent:1'].cluster).toBe(-1);
    expect(byId['mem:3'].single).toBe(true); // only 1 present member
    expect(byId['ent:2'].single).toBe(true); // never in any cluster
  });
  it('passes bounded heat through and zeroes wiki heat', () => {
    const byId = Object.fromEntries(m.nodes.map((n) => [n.id, n]));
    expect(byId['mem:1'].heat).toBeCloseTo(1.0, 6); // heat 1.0 → 1.0 (raw)
    expect(byId['wiki:1'].heat).toBe(0); // wiki has no heat
    expect(byId['mem:2'].heat).toBe(1); // out-of-range 3.0 → clamped to 1.0
  });
  it('entity with no timestamp gets age 0.5', () => {
    const byId = Object.fromEntries(m.nodes.map((n) => [n.id, n]));
    expect(byId['ent:1'].age).toBe(0.5);
  });
  it('builds an idToIndex backbone matching node order', () => {
    expect(m.idToIndex['mem:1']).toBe(0);
    expect(m.idToIndex['ent:2']).toBe(4);
    expect(m.nodes[m.idToIndex['wiki:1']].id).toBe('wiki:1');
  });
  it('counts total/core/arm/type correctly', () => {
    expect(m.counts.total).toBe(6);
    expect(m.counts.arm).toBe(3); // mem:1, mem:2, wiki:1
    expect(m.counts.core).toBe(3); // ent:1, ent:2, mem:3
    expect(m.counts.memory).toBe(3);
    expect(m.counts.wiki).toBe(1);
    expect(m.counts.entity).toBe(2);
  });
  it('handles an empty payload without throwing', () => {
    const e = buildNodeModel({});
    expect(e.nodes).toEqual([]);
    expect(e.clusterStat).toEqual([]);
    expect(e.counts.total).toBe(0);
  });
  it('ignores duplicate member ids when counting cluster size', () => {
    const dupe = buildNodeModel({
      nodes: [{ id: 'a', type: 'memory', heat: 1 }],
      clusters: [{ member_node_ids: ['a', 'a', 'a'] }], // distinct count = 1 → demoted
    });
    expect(dupe.clusterStat.length).toBe(0);
    expect(dupe.nodes[0].single).toBe(true);
  });
});

// ── expRadius: bounded sampler ──────────────────────────────────────────────────
describe('expRadius', () => {
  it('never exceeds rMax and is >= 0', () => {
    const rnd = mulberry32(42);
    for (let i = 0; i < 500; i++) {
      const r = expRadius(rnd, 12, R_MAX, 0);
      expect(r).toBeGreaterThanOrEqual(0);
      expect(r).toBeLessThanOrEqual(R_MAX);
    }
  });
});

// ── sizeOf: heat-driven size, always > 0 ────────────────────────────────────────
describe('sizeOf', () => {
  it('is strictly positive and grows with heat', () => {
    const cold = sizeOf({ heat: 0, type: 'memory', single: false });
    const hot = sizeOf({ heat: 1, type: 'memory', single: false });
    expect(cold).toBeGreaterThan(0);
    expect(hot).toBeGreaterThan(cold);
  });
  it('shrinks loose/core stars and clustered entities', () => {
    const armMem = sizeOf({ heat: 0.5, type: 'memory', single: false });
    const coreMem = sizeOf({ heat: 0.5, type: 'memory', single: true });
    const armEnt = sizeOf({ heat: 0.5, type: 'entity', single: false });
    expect(coreMem).toBeLessThan(armMem);
    expect(armEnt).toBeLessThan(armMem);
  });
});

// ── layoutPositions: ranges + determinism ───────────────────────────────────────
describe('layoutPositions', () => {
  const payload = {
    nodes: [],
    clusters: [{ member_node_ids: [] }],
  };
  // build a mixed corpus: 2 clusters of 4 + a loose core mass
  const members0 = [];
  const members1 = [];
  for (let i = 0; i < 4; i++) {
    payload.nodes.push({ id: `c0:${i}`, type: 'memory', heat: 0.6 });
    members0.push(`c0:${i}`);
    payload.nodes.push({ id: `c1:${i}`, type: 'memory', heat: 0.3 });
    members1.push(`c1:${i}`);
  }
  for (let i = 0; i < 40; i++) {
    payload.nodes.push({ id: `loose:${i}`, type: 'entity', heat: 0.1 });
  }
  payload.clusters = [
    { member_node_ids: members0 },
    { member_node_ids: members1 },
  ];
  const model = buildNodeModel(payload);
  const P = { ...GALAXY_DEFAULTS };

  it('emits a Float32Array of length 3N with finite values', () => {
    const { pos } = layoutPositions(model, P);
    expect(pos).toBeInstanceOf(Float32Array);
    expect(pos.length).toBe(model.nodes.length * 3);
    for (let i = 0; i < pos.length; i++) expect(Number.isFinite(pos[i])).toBe(true);
  });

  it('keeps every node within a bounded radius (arms/core disk, no runaways)', () => {
    const { pos } = layoutPositions(model, P);
    for (let i = 0; i < model.nodes.length; i++) {
      const x = pos[i * 3];
      const y = pos[i * 3 + 1];
      const z = pos[i * 3 + 2];
      const rXZ = Math.hypot(x, z);
      // arm radius <= R_MAX; core is a spheroid within R_MAX*0.55; halo would be
      // wider but default P.single='core'. Allow a small margin for arm/core spread.
      expect(rXZ).toBeLessThanOrEqual(R_MAX + 2);
      expect(Math.abs(y)).toBeLessThanOrEqual(R_MAX);
    }
  });

  it('is DETERMINISTIC: same (model, P) → identical positions', () => {
    const a = layoutPositions(model, P);
    const b = layoutPositions(model, P);
    expect(Array.from(a.pos)).toEqual(Array.from(b.pos));
    expect(a.coreCount).toBe(b.coreCount);
    expect(a.armCount).toBe(b.armCount);
  });

  it('is deterministic across an explicit seed too', () => {
    const a = layoutPositions(model, P, GALAXY_SEED);
    const b = layoutPositions(model, P, GALAXY_SEED);
    expect(Array.from(a.pos)).toEqual(Array.from(b.pos));
  });

  it('changing arms count changes the layout (controls actually drive shape)', () => {
    const a = layoutPositions(model, { ...P, arms: 2 });
    const b = layoutPositions(model, { ...P, arms: 6 });
    expect(Array.from(a.pos)).not.toEqual(Array.from(b.pos));
  });

  it('assigns loose nodes to core and clustered nodes to arms', () => {
    const { coreCount, armCount } = layoutPositions(model, P);
    expect(coreCount).toBe(40); // the loose entities
    expect(armCount).toBe(8); // the two 4-member clusters
  });

  it('halo mode moves loose nodes out of the core count', () => {
    const { coreCount, haloCount } = layoutPositions(model, { ...P, single: 'halo' });
    expect(coreCount).toBe(0);
    expect(haloCount).toBe(40);
  });
});

// ── visibilityMask: filter-mask backbone (Bug 3 — pure part) ────────────────────
describe('visibilityMask', () => {
  const nodes = [{ id: 'a' }, { id: 'b' }, { id: 'c' }];
  it('missing id = visible (1), explicit false = hidden (0)', () => {
    const mask = visibilityMask(nodes, { b: false });
    expect(Array.from(mask)).toEqual([1, 0, 1]);
  });
  it('explicit true also visible; only strict false hides', () => {
    const mask = visibilityMask(nodes, { a: true, b: false, c: 0 });
    // c:0 is falsy but NOT === false → still visible (matches setVisible contract)
    expect(Array.from(mask)).toEqual([1, 0, 1]);
  });
  it('empty / missing visById → all visible', () => {
    expect(Array.from(visibilityMask(nodes, {}))).toEqual([1, 1, 1]);
    expect(Array.from(visibilityMask(nodes))).toEqual([1, 1, 1]);
  });
  it('returns a Uint8Array of length == nodes.length', () => {
    const mask = visibilityMask(nodes, {});
    expect(mask).toBeInstanceOf(Uint8Array);
    expect(mask.length).toBe(3);
  });
  it('empty node set → empty mask, no throw', () => {
    expect(Array.from(visibilityMask([], { x: false }))).toEqual([]);
    expect(Array.from(visibilityMask(undefined))).toEqual([]);
  });
});

// ── P_BOUNDS mirrors the control HTML (drift guard) ─────────────────────────────
describe('P_BOUNDS', () => {
  it('slider bounds match the GALAXY_CHROME_HTML min/max exactly', () => {
    // If a slider range in GALAXY_CHROME_HTML changes, update P_BOUNDS to match
    // (loadSavedP clamps against P_BOUNDS, NOT the DOM — drift = silent corruption).
    expect(P_BOUNDS.arms).toMatchObject({ min: 2, max: 6, int: true });
    expect(P_BOUNDS.pitch).toMatchObject({ min: 0.08, max: 0.75 });
    expect(P_BOUNDS.thick).toMatchObject({ min: 0.1, max: 3.0 });
    expect(P_BOUNDS.coredens).toMatchObject({ min: 0.3, max: 2.5 });
    expect(P_BOUNDS.bulge).toMatchObject({ min: 0.2, max: 2.5 });
    expect(P_BOUNDS.spin).toMatchObject({ min: 0, max: 2.0 });
  });
  it('every GALAXY_DEFAULTS key has a bound entry', () => {
    for (const key of Object.keys(GALAXY_DEFAULTS)) {
      expect(P_BOUNDS[key]).toBeDefined();
    }
  });
});

// ── loadSavedP / saveP: persistence round-trip + clamp + malformed fallback ─────
// Bug 1: controls didn't persist. Pure + injectable storage so private-mode
// throw and malformed JSON are testable without a real localStorage.
describe('loadSavedP / saveP', () => {
  it('round-trips a saved P (only known keys) through a store', () => {
    const store = makeStore();
    const P = { ...GALAXY_DEFAULTS, arms: 5, pitch: 0.5, single: 'halo', spin: 1.2 };
    saveP(P, store);
    const loaded = loadSavedP(store);
    expect(loaded.arms).toBe(5);
    expect(loaded.pitch).toBeCloseTo(0.5, 10);
    expect(loaded.single).toBe('halo');
    expect(loaded.spin).toBeCloseTo(1.2, 10);
  });
  it('no stored value → clean GALAXY_DEFAULTS', () => {
    expect(loadSavedP(makeStore())).toEqual({ ...GALAXY_DEFAULTS });
  });
  it('clamps out-of-range numbers to control bounds', () => {
    const store = makeStore({
      [GALAXY_P_KEY]: JSON.stringify({ arms: 99, pitch: -1, thick: 100, spin: 5 }),
    });
    const P = loadSavedP(store);
    expect(P.arms).toBe(6); // max 6
    expect(P.pitch).toBeCloseTo(0.08, 10); // min 0.08
    expect(P.thick).toBe(3.0); // max 3.0
    expect(P.spin).toBe(2.0); // max 2.0
  });
  it('rounds arms to an integer', () => {
    const store = makeStore({ [GALAXY_P_KEY]: JSON.stringify({ arms: 3.7 }) });
    expect(loadSavedP(store).arms).toBe(4);
  });
  it('drops unknown keys and invalid enum values → keeps default', () => {
    const store = makeStore({
      [GALAXY_P_KEY]: JSON.stringify({ bogus: 42, radmode: 'nonsense', single: 'halo' }),
    });
    const P = loadSavedP(store);
    expect('bogus' in P).toBe(false);
    expect(P.radmode).toBe(GALAXY_DEFAULTS.radmode); // invalid enum → default
    expect(P.single).toBe('halo'); // valid enum → applied
  });
  it('malformed JSON → clean defaults (no throw)', () => {
    const store = makeStore({ [GALAXY_P_KEY]: '{not valid json' });
    expect(loadSavedP(store)).toEqual({ ...GALAXY_DEFAULTS });
  });
  it('non-object JSON (array / scalar) → defaults', () => {
    expect(loadSavedP(makeStore({ [GALAXY_P_KEY]: '[1,2,3]' }))).toEqual({ ...GALAXY_DEFAULTS });
    expect(loadSavedP(makeStore({ [GALAXY_P_KEY]: '7' }))).toEqual({ ...GALAXY_DEFAULTS });
  });
  it('getItem throw (private mode) → defaults, no throw', () => {
    const store = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {},
    };
    expect(loadSavedP(store)).toEqual({ ...GALAXY_DEFAULTS });
  });
  it('setItem throw (private mode / quota) is swallowed', () => {
    const store = {
      getItem: () => null,
      setItem: () => {
        throw new Error('QuotaExceeded');
      },
    };
    expect(() => saveP({ ...GALAXY_DEFAULTS }, store)).not.toThrow();
  });
  it('saveP persists only clamped known keys (drops garbage)', () => {
    const store = makeStore();
    saveP({ ...GALAXY_DEFAULTS, arms: 100, junk: 'x' }, store);
    const raw = JSON.parse(store.getItem(GALAXY_P_KEY));
    expect(raw.arms).toBe(6); // clamped on write
    expect('junk' in raw).toBe(false);
  });
});

// ── assignArmsBalanced: greedy lightest-arm bin-packing (fixes unbalanced arms) ──
// Replaces the old `i % arms` round-robin, which dumped the biggest (rank 0/1)
// clusters into arms 0/1 → 2 arms held most of the nodes. Greedy first-fit-
// decreasing by node count keeps per-arm totals near-equal + stays deterministic.
describe('assignArmsBalanced', () => {
  // Helper: given a cluster list + arms, return the per-arm total node count.
  function armLoads(clusters, arms) {
    const map = assignArmsBalanced(clusters, arms);
    const load = new Array(arms).fill(0);
    for (const c of clusters) load[map.get(c.id)] += c.n;
    return load;
  }
  const spread = (load) => Math.max(...load) / Math.max(1, Math.min(...load));

  it('is materially tighter than round-robin on a skewed corpus', () => {
    // Skewed: a few big clusters + many small. Score-sorted (largest-first) input,
    // mirroring model.armClusters.
    const clusters = [
      { id: 0, n: 100 }, { id: 1, n: 90 }, { id: 2, n: 20 }, { id: 3, n: 18 },
      { id: 4, n: 12 }, { id: 5, n: 10 }, { id: 6, n: 8 }, { id: 7, n: 6 },
    ];
    const arms = 4;
    const balanced = armLoads(clusters, arms);
    // round-robin baseline: i % arms
    const rr = new Array(arms).fill(0);
    clusters.forEach((c, i) => { rr[i % arms] += c.n; });
    expect(spread(balanced)).toBeLessThan(spread(rr));
  });

  it('beats round-robin on the user-reported dominant-cluster corpus', () => {
    // The actual complaint: "2 arms have tons of nodes, the other 2 don't." A
    // couple of dominant clusters (rank 0/1) + a small tail. Round-robin dumps
    // both giants into arms 0/1; greedy splits them across arms.
    const clusters = [
      { id: 0, n: 100 }, { id: 1, n: 90 }, { id: 2, n: 20 }, { id: 3, n: 18 },
      { id: 4, n: 12 }, { id: 5, n: 10 }, { id: 6, n: 8 }, { id: 7, n: 6 },
    ];
    const rr = new Array(4).fill(0);
    clusters.forEach((c, i) => { rr[i % 4] += c.n; });
    // greedy ~2.78 vs round-robin ~4.67 — the giants no longer share an arm.
    expect(spread(armLoads(clusters, 4))).toBeLessThan(spread(rr));
  });

  it('is deterministic (ties → lowest arm index)', () => {
    const clusters = [
      { id: 0, n: 10 }, { id: 1, n: 10 }, { id: 2, n: 10 }, { id: 3, n: 10 },
    ];
    const a = assignArmsBalanced(clusters, 4);
    const b = assignArmsBalanced(clusters, 4);
    expect([...a.entries()]).toEqual([...b.entries()]);
    // equal weights → one cluster per arm, in index order.
    expect(a.get(0)).toBe(0);
    expect(a.get(1)).toBe(1);
    expect(a.get(2)).toBe(2);
    expect(a.get(3)).toBe(3);
  });

  it('handles empty input and arms>=1 defensively', () => {
    expect([...assignArmsBalanced([], 4).entries()]).toEqual([]);
    expect([...assignArmsBalanced(null, 4).entries()]).toEqual([]);
    // arms coerced to >=1
    const map = assignArmsBalanced([{ id: 0, n: 5 }], 0);
    expect(map.get(0)).toBe(0);
  });
});

// ── fitDistanceForDisk: camera distance to frame the galaxy disk (Fit button) ────
describe('fitDistanceForDisk', () => {
  it('larger disk / narrower FOV → greater distance (monotonic)', () => {
    const near = fitDistanceForDisk(46, CAM_FOV_DEG);
    const far = fitDistanceForDisk(92, CAM_FOV_DEG);
    expect(far).toBeGreaterThan(near);
    // narrower FOV needs more distance for the same disk.
    expect(fitDistanceForDisk(46, 30)).toBeGreaterThan(fitDistanceForDisk(46, 52));
  });
  it('clamps to the MiniOrbit wheel bounds [14, 320]', () => {
    expect(fitDistanceForDisk(1, 170)).toBeGreaterThanOrEqual(14); // tiny → min
    expect(fitDistanceForDisk(100000, 1)).toBeLessThanOrEqual(320); // huge → max
  });
  it('matches dist = rMax*pad / tan(fov/2)', () => {
    const rMax = 46, fov = 52, pad = 1.15;
    const expected = (rMax * pad) / Math.tan((fov * Math.PI) / 360);
    expect(fitDistanceForDisk(rMax, fov, pad)).toBeCloseTo(expected, 6);
  });
});

// ── buildStarfield: deterministic cosmic-backdrop star buffers ──────────────────
describe('buildStarfield', () => {
  it('returns position + colour buffers of length n*3', () => {
    const { positions, colors } = buildStarfield(500, 123);
    expect(positions).toBeInstanceOf(Float32Array);
    expect(colors).toBeInstanceOf(Float32Array);
    expect(positions.length).toBe(500 * 3);
    expect(colors.length).toBe(500 * 3);
  });

  it('is deterministic for a given seed', () => {
    const a = buildStarfield(200, 777);
    const b = buildStarfield(200, 777);
    expect(Array.from(a.positions)).toEqual(Array.from(b.positions));
    expect(Array.from(a.colors)).toEqual(Array.from(b.colors));
  });

  it('differs across seeds', () => {
    const a = buildStarfield(200, 1);
    const b = buildStarfield(200, 2);
    expect(Array.from(a.positions)).not.toEqual(Array.from(b.positions));
  });

  it('places every star in the 280..1380 radius shell (< camera far 2000)', () => {
    const { positions } = buildStarfield(1000, 42);
    for (let i = 0; i < 1000; i++) {
      // un-squash the vertical axis to recover the sampled radius
      const x = positions[i * 3], y = positions[i * 3 + 1] / 0.7, z = positions[i * 3 + 2];
      const r = Math.sqrt(x * x + y * y + z * z);
      expect(r).toBeGreaterThanOrEqual(280 - 1e-3);
      expect(r).toBeLessThanOrEqual(1380 + 1e-3);
    }
  });

  it('keeps every colour channel in [0,1]', () => {
    const { colors } = buildStarfield(1000, 9);
    for (let i = 0; i < colors.length; i++) {
      expect(colors[i]).toBeGreaterThanOrEqual(0);
      expect(colors[i]).toBeLessThanOrEqual(1);
    }
  });
});

// ── edge rendering (#69): endpoint resolution + role classification + segments ──
describe('edgeEndId', () => {
  it('returns bare id from a node-object endpoint', () => {
    expect(edgeEndId({ id: 'mem:7' })).toBe('mem:7');
  });
  it('passes through an id-string endpoint', () => {
    expect(edgeEndId('entity:9')).toBe('entity:9');
  });
  it('handles null/undefined without throwing', () => {
    expect(edgeEndId(null)).toBe(null);
    expect(edgeEndId(undefined)).toBe(undefined);
  });
});

describe('edgeRole', () => {
  it('prefers an explicit retrieval/informational role on the wire', () => {
    expect(edgeRole({ role: 'retrieval', type: 'temporal' })).toBe('retrieval');
    expect(edgeRole({ role: 'informational', type: 'transition' })).toBe('informational');
  });
  it('falls back to the type→role map when role is absent/garbage', () => {
    const tr = { transition: 'retrieval', temporal: 'informational' };
    expect(edgeRole({ type: 'transition' }, tr)).toBe('retrieval');
    expect(edgeRole({ type: 'temporal' }, tr)).toBe('informational');
    expect(edgeRole({ role: 'bogus', type: 'transition' }, tr)).toBe('retrieval');
  });
  it('defaults to informational when nothing resolves', () => {
    expect(edgeRole({ type: 'mystery' }, {})).toBe('informational');
    expect(edgeRole({})).toBe('informational');
  });
});

describe('edgeSegments', () => {
  // 3 nodes at distinct positions; a→b retrieval, b→c informational.
  const idToIndex = { a: 0, b: 1, c: 2 };
  const diskPos = new Float32Array([
    0, 0, 0, // a
    1, 2, 3, // b
    4, 5, 6, // c
  ]);
  const edges = [
    { source: 'a', target: 'b', type: 'transition', role: 'retrieval' },
    { source: { id: 'b' }, target: { id: 'c' }, type: 'temporal', role: 'informational' },
  ];

  // Colours are RGBA (itemSize 4, v5.154.0 #216 fix) — 2 verts * 4 per edge.
  // Per-vertex layout: [r, g, b, a]. Edge N's first vertex starts at N*8.
  it('emits one 2-vertex segment per edge with both endpoints resolvable', () => {
    const { positions, colors, count } = edgeSegments({ edges, idToIndex, diskPos });
    expect(count).toBe(2);
    expect(positions.length).toBe(12); // 2 edges * 2 verts * 3 (positions stay RGB-free xyz)
    expect(colors.length).toBe(16); // 2 edges * 2 verts * 4 (RGBA)
    // segment 0 endpoints are node a then node b positions
    expect(Array.from(positions.slice(0, 6))).toEqual([0, 0, 0, 1, 2, 3]);
    expect(Array.from(positions.slice(6, 12))).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it('skips an edge whose endpoint is not in the index (orphan)', () => {
    const { count } = edgeSegments({
      edges: [{ source: 'a', target: 'zzz', type: 'temporal' }],
      idToIndex, diskPos,
    });
    expect(count).toBe(0);
  });

  it('colours the 2 backdrop classes by role (retrieval warm, informational cool)', () => {
    const { colors } = edgeSegments({ edges, idToIndex, diskPos });
    // edge 0 = retrieval (rgba at 0..3)
    expect(colors[0]).toBeCloseTo(EDGE_ROLE_COLOR.retrieval.r, 6);
    expect(colors[1]).toBeCloseTo(EDGE_ROLE_COLOR.retrieval.g, 6);
    expect(colors[2]).toBeCloseTo(EDGE_ROLE_COLOR.retrieval.b, 6);
    // edge 1 = informational (its verts start at index 8 under RGBA stride)
    expect(colors[8]).toBeCloseTo(EDGE_ROLE_COLOR.informational.r, 6);
    expect(colors[9]).toBeCloseTo(EDGE_ROLE_COLOR.informational.g, 6);
    // both endpoints of a segment share one colour (RGBA slabs 0..4 and 4..8)
    expect(Array.from(colors.slice(0, 4))).toEqual(Array.from(colors.slice(4, 8)));
    // visible backdrop edges are fully opaque (alpha 1); faintness is the material.
    expect(colors[3]).toBe(1);
    expect(colors[11]).toBe(1);
    // retrieval must read brighter than informational (warm > cool luminance-ish)
    const rSum = colors[0] + colors[1] + colors[2];
    const iSum = colors[8] + colors[9] + colors[10];
    expect(rSum).toBeGreaterThan(iSum);
  });

  it('paints a toggled-off edge type with alpha 0 (truly invisible, #216)', () => {
    const { colors } = edgeSegments({
      edges, idToIndex, diskPos, toggleState: { transition: false },
    });
    // edge 0 (transition) → black RGB + alpha 0 (zero contribution under BOTH blends)
    expect(colors[0]).toBe(0);
    expect(colors[1]).toBe(0);
    expect(colors[2]).toBe(0);
    expect(colors[3]).toBe(0); // alpha 0 — truly hidden, no dark veil over the core
    // edge 1 (temporal, still on) → visible (alpha 1, non-black rgb)
    expect(colors[8]).toBeGreaterThan(0);
    expect(colors[11]).toBe(1);
  });

  it('paints an edge alpha 0 when either endpoint is hidden by the visMask', () => {
    const visMask = new Uint8Array([1, 0, 1]); // b hidden
    const { colors } = edgeSegments({ edges, idToIndex, diskPos, visMask });
    // edge 0 (a→b) touches hidden b → alpha 0
    expect(colors[0]).toBe(0);
    expect(colors[3]).toBe(0);
    // edge 1 (b→c) touches hidden b → alpha 0
    expect(colors[8]).toBe(0);
    expect(colors[11]).toBe(0);
  });

  it('on focus, brightens the focused node incident edge to its per-type colour', () => {
    const { colors } = edgeSegments({ edges, idToIndex, diskPos, focusId: 'a' });
    // edge 0 (a→b) is incident to a → full per-type transition colour, alpha 1
    expect(colors[0]).toBeCloseTo(EDGE_TYPE_COLOR.transition.r, 6);
    expect(colors[1]).toBeCloseTo(EDGE_TYPE_COLOR.transition.g, 6);
    expect(colors[2]).toBeCloseTo(EDGE_TYPE_COLOR.transition.b, 6);
    expect(colors[3]).toBe(1);
  });

  it('on focus, dims a non-incident edge below its backdrop colour (still alpha 1)', () => {
    const { colors } = edgeSegments({ edges, idToIndex, diskPos, focusId: 'a' });
    // edge 1 (b→c) NOT incident to a → dimmed informational backdrop, still shown
    expect(colors[8]).toBeLessThan(EDGE_ROLE_COLOR.informational.r);
    expect(colors[8]).toBeGreaterThan(0);
    expect(colors[11]).toBe(1);
  });

  it('is a no-op-safe empty build with no edges', () => {
    const { positions, colors, count } = edgeSegments({ edges: [], idToIndex, diskPos });
    expect(count).toBe(0);
    expect(positions.length).toBe(0);
    expect(colors.length).toBe(0);
  });
});

describe('edgeMaterialState (#216 at-rest faint / focus pop)', () => {
  it('at rest (no focus) → NormalBlending at faint opacity 0.15', () => {
    for (const f of [null, undefined]) {
      const s = edgeMaterialState(f);
      expect(s.blending).toBe('normal');
      expect(s.opacity).toBeCloseTo(0.15, 6);
    }
  });

  it('on focus (id set) → AdditiveBlending at pop opacity 0.9', () => {
    const s = edgeMaterialState('mem:42');
    expect(s.blending).toBe('additive');
    expect(s.opacity).toBeCloseTo(0.9, 6);
  });

  it('treats a 0 / falsy-but-present id as a real focus (only null/undefined is at-rest)', () => {
    const s = edgeMaterialState(0);
    expect(s.blending).toBe('additive');
  });
});

// ── Car B (ADR-0152): render backend-served x/y/z + backend loose/arm ───────────
describe('buildDiskPositions (renders served x/y/z)', () => {
  it('reads served node.x/y/z into the position buffer (no client compute)', () => {
    const payload = {
      nodes: [
        { id: 'a', type: 'memory', heat: 1, x: 10, y: 20, z: 30, loose: false, arm: 1 },
        { id: 'b', type: 'entity', heat: 0.2, x: -5, y: 0, z: 5, loose: true, arm: -1 },
      ],
      clusters: [],
    };
    const model = buildNodeModel(payload);
    const { pos, served } = buildDiskPositions(model, payload, GALAXY_DEFAULTS);
    expect(served).toBe(true); // used served coords, did NOT client-compute
    expect(Array.from(pos.slice(0, 3))).toEqual([10, 20, 30]);
    expect(Array.from(pos.slice(3, 6))).toEqual([-5, 0, 5]);
  });

  it('falls back to layoutPositions when the payload carries no served coords', () => {
    const payload = {
      nodes: [
        { id: 'a', type: 'memory', heat: 1 },
        { id: 'b', type: 'memory', heat: 1 },
      ],
      clusters: [{ member_node_ids: ['a', 'b'] }],
    };
    const model = buildNodeModel(payload);
    const { pos, served } = buildDiskPositions(model, payload, GALAXY_DEFAULTS);
    expect(served).toBe(false);
    // still produces a full position buffer (client fallback path)
    expect(pos.length).toBe(6);
  });
});

describe('buildNodeModel reads backend loose/arm when stamped', () => {
  it('uses backend node.loose for single (not client cluster derivation)', () => {
    // No clusters in payload, but backend stamped 'a' as arm material (loose:false).
    const payload = {
      nodes: [
        { id: 'a', type: 'entity', heat: 0.5, loose: false, arm: 2 },
        { id: 'b', type: 'memory', heat: 0.5, loose: true, arm: -1 },
      ],
      clusters: [],
    };
    const m = buildNodeModel(payload);
    const byId = Object.fromEntries(m.nodes.map((n) => [n.id, n]));
    expect(byId['a'].single).toBe(false); // backend authority: connected hub → arm
    expect(byId['a'].arm).toBe(2);
    expect(byId['b'].single).toBe(true);
    expect(byId['b'].arm).toBe(-1);
  });
});

describe('edgeSegments suppresses core-core edges via backend loose (bug #3b)', () => {
  const idToIndex = { a: 0, b: 1, c: 2 };
  const diskPos = new Float32Array([0, 0, 0, 1, 1, 1, 2, 2, 2]);
  it('hides an edge where BOTH endpoints are backend-loose (core)', () => {
    // a,b loose (core); c on an arm. a-b must be suppressed; a-c kept.
    const looseById = { a: true, b: true, c: false };
    const edges = [
      { source: 'a', target: 'b', type: 'temporal', role: 'informational' },
      { source: 'a', target: 'c', type: 'transition', role: 'retrieval' },
    ];
    const { colors } = edgeSegments({ edges, idToIndex, diskPos, looseById });
    // edge 0 (core-core) → alpha 0 (suppressed)
    expect(colors[3]).toBe(0);
    // edge 1 (core-arm) → visible (alpha 1)
    expect(colors[11]).toBe(1);
  });
  it('keeps core-core edges when no looseById is supplied (back-compat)', () => {
    const edges = [{ source: 'a', target: 'b', type: 'temporal', role: 'informational' }];
    const { colors } = edgeSegments({ edges, idToIndex, diskPos });
    expect(colors[3]).toBe(1); // visible — no suppression without the flag map
  });
});

// ── Car C (ADR-0152): slider server-recompute re-stamps membership ──────────────
describe('applyServedRelayout (slider recompute)', () => {
  const payload = {
    nodes: [
      { id: 'a', type: 'memory', heat: 1, loose: false, arm: 0 },
      { id: 'b', type: 'entity', heat: 0.3, loose: true, arm: -1 },
    ],
    clusters: [],
  };
  it('re-stamps nd.single/arm from the response membership (arms reassignment)', () => {
    const model = buildNodeModel(payload);
    // server response: 'a' moved to arm 3, 'b' promoted onto arm 1.
    const resp = {
      positions: { a: [1, 2, 3], b: [4, 5, 6] },
      membership: { a: { loose: false, arm: 3 }, b: { loose: false, arm: 1 } },
    };
    const { pos, armCount } = applyServedRelayout(model, resp);
    const byId = Object.fromEntries(model.nodes.map((n) => [n.id, n]));
    expect(byId['a'].arm).toBe(3); // NOT the stale arm 0
    expect(byId['b'].single).toBe(false); // promoted
    expect(byId['b'].arm).toBe(1);
    expect(Array.from(pos.slice(0, 3))).toEqual([1, 2, 3]);
    expect(armCount).toBe(2);
  });
  it('tolerates a missing membership entry (keeps prior stamp)', () => {
    const model = buildNodeModel(payload);
    const resp = { positions: { a: [7, 8, 9] }, membership: {} };
    applyServedRelayout(model, resp);
    const byId = Object.fromEntries(model.nodes.map((n) => [n.id, n]));
    expect(byId['a'].arm).toBe(0); // unchanged (no membership entry)
  });
});
