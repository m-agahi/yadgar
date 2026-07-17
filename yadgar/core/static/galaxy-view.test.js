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
  expRadius,
  sizeOf,
  mulberry32,
  GALAXY_DEFAULTS,
  GALAXY_SEED,
  R_MAX,
  HEAT_H0,
} from './galaxy-view.js';

// ── normalizeHeat: [0,inf) → [0,1) soft-saturation ──────────────────────────────
describe('normalizeHeat', () => {
  it('maps 0 → 0', () => {
    expect(normalizeHeat(0)).toBe(0);
  });
  it('maps H0 → 0.5 (soft-saturation half-point)', () => {
    expect(normalizeHeat(HEAT_H0)).toBeCloseTo(0.5, 10);
  });
  it('is monotone increasing and stays < 1 for large heat', () => {
    const a = normalizeHeat(2);
    const b = normalizeHeat(10);
    const c = normalizeHeat(1000);
    expect(a).toBeGreaterThan(0.5);
    expect(b).toBeGreaterThan(a);
    expect(c).toBeGreaterThan(b);
    expect(c).toBeLessThan(1);
  });
  it('clamps NaN / negative / undefined to 0', () => {
    expect(normalizeHeat(NaN)).toBe(0);
    expect(normalizeHeat(-5)).toBe(0);
    expect(normalizeHeat(undefined)).toBe(0);
    expect(normalizeHeat(null)).toBe(0);
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
  it('normalizes heat and zeroes wiki heat', () => {
    const byId = Object.fromEntries(m.nodes.map((n) => [n.id, n]));
    expect(byId['mem:1'].heat).toBeCloseTo(0.5, 6); // heat 1.0 → 0.5
    expect(byId['wiki:1'].heat).toBe(0); // wiki has no heat
    expect(byId['mem:2'].heat).toBeCloseTo(0.75, 6); // heat 3.0 → 0.75
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
