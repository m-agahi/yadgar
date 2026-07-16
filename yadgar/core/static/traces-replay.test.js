/**
 * traces-replay.test.js — vitest unit tests for the pure replay logic.
 * Covers fixed-lane layout, timeline dwell + monotonic-clamp, scrub mapping,
 * play-clock advance/clamp, speed cycling, and fault detection.
 */

import { describe, it, expect } from 'vitest';
import {
  laneX,
  layoutStages,
  computeTimeline,
  stageStateAt,
  scrubFractionToMs,
  msToFraction,
  playheadX,
  advanceClock,
  nextSpeedIdx,
  meshHasFault,
  firstFaultStage,
  edgeLaneClass,
  LANE_Y,
  MESH,
  SPEEDS,
  DILATION,
} from './traces-replay.js';

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

// ── play clock ────────────────────────────────────────────────────────────

describe('advanceClock', () => {
  it('advances t by dt/DILATION*speed at ×1', () => {
    const { t, playing } = advanceClock(0, DILATION, 1, 1000); // speedIdx 1 = ×1
    expect(t).toBeCloseTo(1, 6); // DILATION wall-ms → 1 trace-ms
    expect(playing).toBe(true);
  });
  it('scales by speed (×2 index)', () => {
    const idx2 = SPEEDS.indexOf(2);
    const { t } = advanceClock(0, DILATION, idx2, 1000);
    expect(t).toBeCloseTo(2, 6);
  });
  it('clamps to total and stops playing at the end', () => {
    const { t, playing } = advanceClock(999, DILATION * 100, 1, 1000);
    expect(t).toBe(1000);
    expect(playing).toBe(false);
  });
});

describe('nextSpeedIdx', () => {
  it('cycles and wraps', () => {
    expect(nextSpeedIdx(0)).toBe(1);
    expect(nextSpeedIdx(SPEEDS.length - 1)).toBe(0);
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
