/**
 * traces-replay.test.js — vitest unit tests for the pure replay logic.
 * Covers fixed-lane layout, timeline dwell + monotonic-clamp, scrub mapping,
 * play-clock advance/clamp, speed cycling, and fault detection.
 */

import { describe, it, expect } from 'vitest';
import {
  laneX,
  layoutStages,
  scatterLayout,
  computeTimeline,
  stageStateAt,
  scrubFractionToMs,
  msToFraction,
  playheadX,
  advanceClock,
  loadSpeedId,
  saveSpeedId,
  speedById,
  meshHasFault,
  firstFaultStage,
  edgeLaneClass,
  LANE_Y,
  LANE_BAND,
  LANE_DIVIDER_Y,
  MESH,
  SPEED_PRESETS,
  DEFAULT_SPEED_ID,
} from './traces-replay.js';

// Minimal injectable localStorage double (galaxy-view test pattern).
function fakeStore(initial = {}) {
  const m = new Map(Object.entries(initial));
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    _map: m,
  };
}

// ── layout ────────────────────────────────────────────────────────────────

describe('laneX', () => {
  it('centers a single node', () => {
    expect(laneX(0, 1)).toBe(MESH.x0 + (MESH.x1 - MESH.x0) * 0.5);
  });
  it('spreads endpoints to x0 and x1', () => {
    expect(laneX(0, 3)).toBe(MESH.x0);
    expect(laneX(2, 3)).toBe(MESH.x1);
  });
  it('places the middle node halfway', () => {
    expect(laneX(1, 3)).toBeCloseTo((MESH.x0 + MESH.x1) / 2, 6);
  });
});

describe('layoutStages', () => {
  it('assigns lane Y by svc lane and does not mutate input', () => {
    const nodes = [
      { id: 's0', lane: 'core' },
      { id: 's1', lane: 'backend' },
    ];
    const out = layoutStages(nodes);
    expect(out[0].y).toBe(LANE_Y.core);
    expect(out[1].y).toBe(LANE_Y.backend);
    expect(out[0].x).toBe(MESH.x0);
    expect(out[1].x).toBe(MESH.x1);
    // input untouched
    expect(nodes[0].x).toBeUndefined();
  });
  it('falls back to core lane for unknown lane', () => {
    const out = layoutStages([{ id: 's0', lane: 'weird' }]);
    expect(out[0].y).toBe(LANE_Y.core);
  });
});

// ── timeline ──────────────────────────────────────────────────────────────

describe('computeTimeline', () => {
  it('computes sequential dwell = next start - start; last ends at total', () => {
    const nodes = [
      { id: 's0', rel_ms: 0 },
      { id: 's1', rel_ms: 10 },
      { id: 's2', rel_ms: 30 },
    ];
    const st = computeTimeline(nodes, 50);
    expect(st[0].start).toBe(0);
    expect(st[0].end).toBe(10);
    expect(st[0].dwell).toBe(10);
    expect(st[1].dwell).toBe(20);
    expect(st[2].end).toBe(50);
    expect(st[2].dwell).toBe(20);
  });

  it('monotonic-clamps an out-of-order (early) start', () => {
    // s1 reports rel_ms=1.7 (early http-send) but must not precede s0 at 5
    const nodes = [
      { id: 's0', rel_ms: 5 },
      { id: 's1', rel_ms: 1.7 },
      { id: 's2', rel_ms: 20 },
    ];
    const st = computeTimeline(nodes, 30);
    expect(st[1].start).toBeGreaterThanOrEqual(st[0].start);
  });

  it('gives a floor dwell of 0.01 for zero-width stages', () => {
    const st = computeTimeline([{ id: 's0', rel_ms: 5 }], 5);
    expect(st[0].dwell).toBeCloseTo(0.01, 6);
  });
});

describe('stageStateAt', () => {
  const stage = { start: 10, end: 20 };
  it('is pending before start', () => expect(stageStateAt(stage, 5)).toBe('pending'));
  it('is armed within window', () => expect(stageStateAt(stage, 15)).toBe('armed'));
  it('is done at/after end', () => expect(stageStateAt(stage, 20)).toBe('done'));
});

// ── scrub mapping ─────────────────────────────────────────────────────────

describe('scrub mapping', () => {
  it('maps fraction to ms and clamps out-of-range', () => {
    expect(scrubFractionToMs(0.5, 100)).toBe(50);
    expect(scrubFractionToMs(-1, 100)).toBe(0);
    expect(scrubFractionToMs(2, 100)).toBe(100);
  });
  it('maps ms back to fraction and clamps', () => {
    expect(msToFraction(50, 100)).toBe(0.5);
    expect(msToFraction(200, 100)).toBe(1);
    expect(msToFraction(5, 0)).toBe(0); // zero total → 0, no div-by-zero
  });
  it('round-trips fraction → ms → fraction', () => {
    const f = 0.37;
    expect(msToFraction(scrubFractionToMs(f, 250), 250)).toBeCloseTo(f, 6);
  });
  it('playheadX scales fraction by width', () => {
    expect(playheadX(50, 100, 1000)).toBe(500);
  });
});

// ── scatter layout (item-2) ─────────────────────────────────────────────────

describe('scatterLayout', () => {
  it('returns [] for an empty node list', () => {
    expect(scatterLayout([], 100)).toEqual([]);
    expect(scatterLayout(null, 100)).toEqual([]);
  });

  it('centers a single node in x and at its lane centre in y', () => {
    const out = scatterLayout([{ rel_ms: 40, lane: 'core' }], 100);
    expect(out).toHaveLength(1);
    expect(out[0].x).toBe(MESH.x0 + (MESH.x1 - MESH.x0) * 0.4);
    expect(out[0].y).toBe(LANE_Y.core);
  });

  it('x is monotonic non-decreasing in rel_ms within a lane', () => {
    const nodes = [
      { rel_ms: 0, lane: 'backend' },
      { rel_ms: 50, lane: 'backend' },
      { rel_ms: 90, lane: 'backend' },
    ];
    const out = scatterLayout(nodes, 100);
    const xs = out.map((n) => n.x);
    for (let i = 1; i < xs.length; i++) expect(xs[i]).toBeGreaterThanOrEqual(xs[i - 1]);
  });

  it('keeps y strictly inside the node lane band (never crosses the divider)', () => {
    // 8 backend nodes all at nearly the same rel_ms → maximum y spread.
    const nodes = Array.from({ length: 8 }, (_, i) => ({ rel_ms: 40 + i * 0.1, lane: 'backend' }));
    const out = scatterLayout(nodes, 100);
    for (const n of out) {
      expect(n.y).toBeGreaterThanOrEqual(LANE_Y.backend - LANE_BAND.half);
      expect(n.y).toBeLessThanOrEqual(LANE_Y.backend + LANE_BAND.half);
      // band stays below the divider midline
      expect(n.y).toBeGreaterThan(LANE_DIVIDER_Y);
    }
  });

  it('de-overlaps a cluster: nodes closer than minGapX in x get distinct y', () => {
    const nodes = [
      { rel_ms: 40.0, lane: 'core' },
      { rel_ms: 40.1, lane: 'core' },
      { rel_ms: 40.2, lane: 'core' },
    ];
    const out = scatterLayout(nodes, 100);
    const ys = out.map((n) => n.y);
    expect(new Set(ys).size).toBeGreaterThan(1); // not all on one line
  });

  it('no two same-lane nodes overlap: NOT (Δx<minGapX AND Δy<ring-diameter)', () => {
    // dense backend lane (the 18-recall-stage case) all clustered in time.
    const RING = 2 * MESH.r; // circle diameter — overlap threshold
    const nodes = Array.from({ length: 18 }, (_, i) => ({ rel_ms: 40 + i * 0.1, lane: 'backend' }));
    const out = scatterLayout(nodes, 100);
    for (let i = 0; i < out.length; i++) {
      for (let j = i + 1; j < out.length; j++) {
        const dx = Math.abs(out[i].x - out[j].x);
        const dy = Math.abs(out[i].y - out[j].y);
        expect(dx >= LANE_BAND.minGapX || dy >= RING).toBe(true);
      }
    }
  });

  it('nodes stay in their own lane band (core vs backend never mix)', () => {
    const nodes = [
      { rel_ms: 10, lane: 'core' },
      { rel_ms: 20, lane: 'backend' },
    ];
    const out = scatterLayout(nodes, 100);
    expect(out[0].y).toBeLessThan(LANE_DIVIDER_Y); // core above
    expect(out[1].y).toBeGreaterThan(LANE_DIVIDER_Y); // backend below
  });

  it('falls back to index spacing when totalMs<=0', () => {
    const nodes = [
      { rel_ms: 0, lane: 'core' },
      { rel_ms: 0, lane: 'core' },
    ];
    const out = scatterLayout(nodes, 0);
    expect(out[0].x).toBe(MESH.x0);
    expect(out[1].x).toBe(MESH.x1);
  });

  it('is deterministic — same input twice → identical output', () => {
    const nodes = [
      { rel_ms: 5, lane: 'core' },
      { rel_ms: 5, lane: 'core' },
      { rel_ms: 60, lane: 'backend' },
    ];
    expect(scatterLayout(nodes, 100)).toEqual(scatterLayout(nodes, 100));
  });

  it('does not crash on an empty backend lane (core-only tool)', () => {
    const nodes = [
      { rel_ms: 0, lane: 'core' },
      { rel_ms: 20, lane: 'core' },
    ];
    const out = scatterLayout(nodes, 22);
    expect(out).toHaveLength(2);
    expect(out.every((n) => n.y < LANE_DIVIDER_Y)).toBe(true);
  });

  it('does not mutate input nodes', () => {
    const nodes = [{ rel_ms: 10, lane: 'core' }];
    scatterLayout(nodes, 100);
    expect(nodes[0].x).toBeUndefined();
    expect(nodes[0].y).toBeUndefined();
  });
});

// ── speed presets + persistence (item-4) ────────────────────────────────────

describe('SPEED_PRESETS', () => {
  it('has the 6 requested presets with realtime default', () => {
    expect(SPEED_PRESETS.map((p) => p.id)).toEqual([
      'slow',
      'medium',
      'fast',
      'realtime',
      '2x',
      '10x',
    ]);
    expect(DEFAULT_SPEED_ID).toBe('realtime');
    expect(speedById('realtime').msPerMs).toBe(1);
    expect(speedById('10x').msPerMs).toBe(0.1);
    expect(speedById('slow').msPerMs).toBe(100);
  });
});

describe('speedById', () => {
  it('returns the preset for a known id', () => {
    expect(speedById('fast').label).toBe('Fast');
  });
  it('falls back to realtime for an unknown id', () => {
    expect(speedById('nope').id).toBe('realtime');
  });
});

describe('loadSpeedId / saveSpeedId', () => {
  it('empty store → default', () => {
    expect(loadSpeedId(fakeStore())).toBe(DEFAULT_SPEED_ID);
  });
  it('garbage value → default', () => {
    expect(loadSpeedId(fakeStore({ 'yadgar.traces.speed': 'bogus' }))).toBe(DEFAULT_SPEED_ID);
  });
  it('valid id → that id', () => {
    expect(loadSpeedId(fakeStore({ 'yadgar.traces.speed': '10x' }))).toBe('10x');
  });
  it('saveSpeedId round-trips through a fake store', () => {
    const s = fakeStore();
    saveSpeedId('fast', s);
    expect(loadSpeedId(s)).toBe('fast');
  });
  it('saveSpeedId coerces an unknown id to default', () => {
    const s = fakeStore();
    saveSpeedId('nope', s);
    expect(loadSpeedId(s)).toBe(DEFAULT_SPEED_ID);
  });
  it('load swallows a private-mode getItem throw → default', () => {
    const throwing = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {},
    };
    expect(loadSpeedId(throwing)).toBe(DEFAULT_SPEED_ID);
  });
});

// ── play clock ────────────────────────────────────────────────────────────

describe('advanceClock', () => {
  it('advances t by dt / msPerMs at realtime (1:1)', () => {
    const { t, playing } = advanceClock(0, 10, 1, 1000); // msPerMs=1 → 10ms wall = 10ms trace
    expect(t).toBeCloseTo(10, 6);
    expect(playing).toBe(true);
  });
  it('slow preset (msPerMs=100) advances 100× slower', () => {
    const { t } = advanceClock(0, 100, 100, 1000); // 100ms wall → 1ms trace
    expect(t).toBeCloseTo(1, 6);
  });
  it('10× preset (msPerMs=0.1) advances 10× faster than realtime', () => {
    const { t } = advanceClock(0, 10, 0.1, 1000); // 10ms wall → 100ms trace
    expect(t).toBeCloseTo(100, 6);
  });
  it('clamps to total and stops playing at the end', () => {
    const { t, playing } = advanceClock(999, 1000, 1, 1000);
    expect(t).toBe(1000);
    expect(playing).toBe(false);
  });
  it('guards a non-positive msPerMs → treated as realtime', () => {
    const { t } = advanceClock(0, 5, 0, 1000);
    expect(t).toBeCloseTo(5, 6);
  });
});

// ── fault ─────────────────────────────────────────────────────────────────

describe('fault detection', () => {
  const clean = { nodes: [{ id: 's0', error: false }] };
  const faulty = { nodes: [{ id: 's0', error: false }, { id: 's1', error: true }] };
  it('meshHasFault true only with an error node', () => {
    expect(meshHasFault(clean)).toBe(false);
    expect(meshHasFault(faulty)).toBe(true);
    expect(meshHasFault(null)).toBe(false);
  });
  it('firstFaultStage returns the erroring node', () => {
    expect(firstFaultStage(clean)).toBeNull();
    expect(firstFaultStage(faulty).id).toBe('s1');
  });
});

// ── edges ─────────────────────────────────────────────────────────────────

describe('edgeLaneClass', () => {
  it('classifies lane crossings', () => {
    expect(edgeLaneClass({ lane: 'core' }, { lane: 'core' })).toBe('');
    expect(edgeLaneClass({ lane: 'core' }, { lane: 'backend' })).toBe('to-backend');
    expect(edgeLaneClass({ lane: 'backend' }, { lane: 'core' })).toBe('from-backend');
  });
});
