import { describe, it, expect } from 'vitest';
import { ndcToScreen, haloScale } from './galaxy-halo.js';

describe('ndcToScreen', () => {
  const W = 800;
  const H = 600;
  it('maps NDC center (0,0) to screen center', () => {
    expect(ndcToScreen(0, 0, W, H)).toMatchObject({ x: 400, y: 300 });
  });
  it('maps NDC top-right (1,1) to screen top-right (y flips)', () => {
    // NDC +1 x → right edge; NDC +1 y → TOP of screen (y=0)
    expect(ndcToScreen(1, 1, W, H)).toMatchObject({ x: 800, y: 0 });
  });
  it('maps NDC bottom-left (-1,-1) to screen bottom-left', () => {
    expect(ndcToScreen(-1, -1, W, H)).toMatchObject({ x: 0, y: 600 });
  });
  it('reports offscreen when NDC z beyond [-1,1] is passed as third arg', () => {
    const r = ndcToScreen(0, 0, W, H, 1.5);
    expect(r.onscreen).toBe(false);
  });
  it('onscreen true for NDC within frustum', () => {
    const r = ndcToScreen(0.5, -0.5, W, H, 0.2);
    expect(r.onscreen).toBe(true);
    expect(r.x).toBe(600);
    expect(r.y).toBe(450);
  });
});

describe('haloScale — pulsing halo size over time', () => {
  it('oscillates between min and max', () => {
    const base = 2;
    const vals = [0, 0.25, 0.5, 0.75, 1].map((t) => haloScale(base, t * 1000));
    for (const v of vals) {
      expect(v).toBeGreaterThanOrEqual(base * 0.82 - 1e-6);
      expect(v).toBeLessThanOrEqual(base * 1.12 + 1e-6);
    }
  });
  it('is deterministic for a given time', () => {
    expect(haloScale(3, 1234)).toBeCloseTo(haloScale(3, 1234));
  });
});
