/**
 * overlays.test.js — overlay localStorage round-trip tests
 *
 * Tests loadOverlayState, saveOverlayPosition, saveOverlayCollapsed
 * imported from overlays.js (pure functions, no DOM required).
 *
 * Run: cd viz-tests && npm test
 */

import { describe, expect, it } from 'vitest';
import {
  loadOverlayState,
  saveOverlayPosition,
  saveOverlayCollapsed,
} from './overlays.js';

// ── Minimal localStorage stub ─────────────────────────────────────────────────

function makeLocalStorage() {
  const store = {};
  return {
    getItem: (key) => (key in store ? store[key] : null),
    setItem: (key, val) => { store[key] = String(val); },
    removeItem: (key) => { delete store[key]; },
    clear: () => { Object.keys(store).forEach(k => delete store[k]); },
  };
}

// ── Position persistence round-trip ───────────────────────────────────────────

describe('overlay position localStorage round-trip', () => {
  it('saves and restores position', () => {
    const ls = makeLocalStorage();
    saveOverlayPosition(ls, 'heat-slider', 120, 340);
    const state = loadOverlayState(ls, 'heat-slider', { position: { x: 0, y: 0 } });
    expect(state.position.x).toBe(120);
    expect(state.position.y).toBe(340);
  });

  it('returns default position when key absent', () => {
    const ls = makeLocalStorage();
    const state = loadOverlayState(ls, 'heat-slider', { position: { x: 50, y: 80 } });
    expect(state.position.x).toBe(50);
    expect(state.position.y).toBe(80);
  });

  it('handles multiple overlays independently', () => {
    const ls = makeLocalStorage();
    saveOverlayPosition(ls, 'heat-slider', 10, 20);
    saveOverlayPosition(ls, 'graph-stats', 300, 400);
    const heatState = loadOverlayState(ls, 'heat-slider', { position: { x: 0, y: 0 } });
    const statsState = loadOverlayState(ls, 'graph-stats', { position: { x: 0, y: 0 } });
    expect(heatState.position).toEqual({ x: 10, y: 20 });
    expect(statsState.position).toEqual({ x: 300, y: 400 });
  });
});

// ── Collapse persistence round-trip ───────────────────────────────────────────

describe('overlay collapse localStorage round-trip', () => {
  it('saves and restores collapsed=true', () => {
    const ls = makeLocalStorage();
    saveOverlayCollapsed(ls, 'heat-slider', true);
    const state = loadOverlayState(ls, 'heat-slider', { position: { x: 0, y: 0 }, collapsed: false });
    expect(state.collapsed).toBe(true);
  });

  it('saves and restores collapsed=false', () => {
    const ls = makeLocalStorage();
    saveOverlayCollapsed(ls, 'heat-slider', false);
    const state = loadOverlayState(ls, 'heat-slider', { position: { x: 0, y: 0 }, collapsed: true });
    expect(state.collapsed).toBe(false);
  });

  it('returns default collapsed=false when key absent', () => {
    const ls = makeLocalStorage();
    const state = loadOverlayState(ls, 'heat-slider', { position: { x: 0, y: 0 } });
    expect(state.collapsed).toBe(false);
  });
});

// ── Corrupt JSON fallback ──────────────────────────────────────────────────────

describe('corrupt localStorage JSON fallback', () => {
  it('falls back to defaults when position JSON is corrupt', () => {
    const ls = makeLocalStorage();
    ls.setItem('viz.overlay.heat-slider.position', '{ NOT VALID JSON }}');
    const state = loadOverlayState(ls, 'heat-slider', { position: { x: 99, y: 77 } });
    expect(state.position).toEqual({ x: 99, y: 77 });
  });

  it('falls back to defaults when position is missing x/y', () => {
    const ls = makeLocalStorage();
    ls.setItem('viz.overlay.heat-slider.position', JSON.stringify({ left: 10, top: 20 }));
    const state = loadOverlayState(ls, 'heat-slider', { position: { x: 55, y: 66 } });
    expect(state.position).toEqual({ x: 55, y: 66 });
  });

  it('falls back to defaults when position value is null', () => {
    const ls = makeLocalStorage();
    ls.setItem('viz.overlay.heat-slider.position', 'null');
    const state = loadOverlayState(ls, 'heat-slider', { position: { x: 1, y: 2 } });
    expect(state.position).toEqual({ x: 1, y: 2 });
  });
});
