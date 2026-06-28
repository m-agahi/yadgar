/**
 * viz_helpers.test.js — Layer 3 JS unit tests for viz_helpers.js
 *
 * Tests pure helper functions extracted from index.html.
 * Run with: npx vitest run yadgar/static/viz_helpers.test.js
 *
 * v5.37.0 viz integration testing infrastructure.
 */

import { describe, expect, it } from 'vitest';
import { _fmtBytes, _fmtUptime, _linkWidth, esc, findOrphanEdgeEndpoints, shouldFitOnStop } from './viz_helpers.js';

// ── _fmtBytes ─────────────────────────────────────────────────────────────────

describe('_fmtBytes', () => {
  it('returns — for null', () => {
    expect(_fmtBytes(null)).toBe('—');
  });

  it('formats bytes < 1024 as "N B"', () => {
    expect(_fmtBytes(0)).toBe('0 B');
    expect(_fmtBytes(512)).toBe('512 B');
    expect(_fmtBytes(1023)).toBe('1023 B');
  });

  it('formats 1024 as "1 KB"', () => {
    expect(_fmtBytes(1024)).toBe('1 KB');
  });

  it('formats kilobytes', () => {
    expect(_fmtBytes(2048)).toBe('2 KB');
    expect(_fmtBytes(10240)).toBe('10 KB');
  });

  it('formats megabytes with 1 decimal', () => {
    expect(_fmtBytes(1048576)).toBe('1.0 MB');
    expect(_fmtBytes(1572864)).toBe('1.5 MB');
  });

  it('formats gigabytes with 1 decimal', () => {
    expect(_fmtBytes(1073741824)).toBe('1.0 GB');
    expect(_fmtBytes(2 * 1073741824)).toBe('2.0 GB');
  });
});

// ── _fmtUptime ────────────────────────────────────────────────────────────────

describe('_fmtUptime', () => {
  it('returns — for null', () => {
    expect(_fmtUptime(null)).toBe('—');
  });

  it('formats seconds only', () => {
    expect(_fmtUptime(0)).toBe('0s');
    expect(_fmtUptime(45)).toBe('45s');
    expect(_fmtUptime(59)).toBe('59s');
  });

  it('formats minutes', () => {
    expect(_fmtUptime(60)).toBe('1m 0s');
    expect(_fmtUptime(90)).toBe('1m 30s');
    expect(_fmtUptime(3599)).toBe('59m 59s');
  });

  it('formats hours', () => {
    expect(_fmtUptime(3600)).toBe('1h 0m');
    expect(_fmtUptime(7200)).toBe('2h 0m');
    expect(_fmtUptime(5400)).toBe('1h 30m');
  });
});

// ── esc ───────────────────────────────────────────────────────────────────────

describe('esc', () => {
  it('passes through plain strings unchanged', () => {
    expect(esc('hello world')).toBe('hello world');
  });

  it('escapes &', () => {
    expect(esc('a & b')).toBe('a &amp; b');
  });

  it('escapes < and >', () => {
    expect(esc('<script>')).toBe('&lt;script&gt;');
    expect(esc('a > b')).toBe('a &gt; b');
  });

  it('escapes all three in one string', () => {
    // esc does NOT escape double quotes (matches index.html implementation)
    expect(esc('<div class="a">a & b</div>')).toBe(
      '&lt;div class="a"&gt;a &amp; b&lt;/div&gt;',
    );
  });

  it('coerces non-string to string', () => {
    expect(esc(42)).toBe('42');
    expect(esc(null)).toBe('null');
  });
});

// ── _linkWidth ────────────────────────────────────────────────────────────────

describe('_linkWidth', () => {
  it('returns 1 for semantic edges', () => {
    expect(_linkWidth({ type: 'semantic' })).toBe(1);
  });

  it('returns 1 for wiki_crossref edges', () => {
    expect(_linkWidth({ type: 'wiki_crossref' })).toBe(1);
  });

  it('returns 1.5 for temporal and other edges', () => {
    expect(_linkWidth({ type: 'temporal' })).toBe(1.5);
    expect(_linkWidth({ type: 'causal' })).toBe(1.5);
    expect(_linkWidth({ type: 'memory_wiki' })).toBe(1.5);
  });

  it('returns 1 for transition edges with count=1', () => {
    expect(_linkWidth({ type: 'transition', count: 1 })).toBe(1);
  });

  it('returns >1 for transition edges with count>1', () => {
    const w = _linkWidth({ type: 'transition', count: 4 });
    expect(w).toBeGreaterThan(1);
    expect(w).toBeLessThanOrEqual(4);
  });

  it('caps transition width at 4 for very high counts', () => {
    const w = _linkWidth({ type: 'transition', count: 10000 });
    expect(w).toBe(4);
  });

  it('treats missing count as 1 for transition edges', () => {
    expect(_linkWidth({ type: 'transition' })).toBe(1);
  });
});

// ── findOrphanEdgeEndpoints ───────────────────────────────────────────────────

describe('findOrphanEdgeEndpoints', () => {
  it('returns empty set when no edges', () => {
    const payload = {
      nodes: [{ id: 'mem:1' }, { id: 'mem:2' }],
      edges: [],
    };
    expect(findOrphanEdgeEndpoints(payload).size).toBe(0);
  });

  it('returns empty set when all edges are valid', () => {
    const payload = {
      nodes: [{ id: 'mem:1' }, { id: 'mem:2' }],
      edges: [{ source: 'mem:1', target: 'mem:2', type: 'semantic' }],
    };
    expect(findOrphanEdgeEndpoints(payload).size).toBe(0);
  });

  it('catches orphan source endpoint (the v5.10.9 bug class)', () => {
    // entity:172 exists in edges but NOT in nodes — the exact failure mode
    const payload = {
      nodes: [{ id: 'mem:1' }, { id: 'mem:2' }],
      edges: [
        { source: 'mem:1', target: 'mem:2', type: 'semantic' },
        { source: 'entity:172', target: 'mem:1', type: 'causal' },
      ],
    };
    const orphans = findOrphanEdgeEndpoints(payload);
    expect(orphans.has('entity:172')).toBe(true);
    expect(orphans.size).toBe(1);
  });

  it('catches orphan target endpoint', () => {
    const payload = {
      nodes: [{ id: 'mem:1' }],
      edges: [{ source: 'mem:1', target: 'entity:999', type: 'causal' }],
    };
    const orphans = findOrphanEdgeEndpoints(payload);
    expect(orphans.has('entity:999')).toBe(true);
  });

  it('catches multiple orphan endpoints', () => {
    const payload = {
      nodes: [{ id: 'mem:1' }],
      edges: [
        { source: 'entity:1', target: 'entity:2', type: 'causal' },
        { source: 'entity:3', target: 'entity:4', type: 'causal' },
      ],
    };
    const orphans = findOrphanEdgeEndpoints(payload);
    expect(orphans.size).toBe(4);
  });

  it('handles empty payload gracefully', () => {
    expect(findOrphanEdgeEndpoints({ nodes: [], edges: [] }).size).toBe(0);
    expect(findOrphanEdgeEndpoints({}).size).toBe(0);
  });
});

// ── shouldFitOnStop (v5.87.1: warm-start blank-canvas fix) ──────────────────────
//
// Bug: warm-start caps cooldownTicks(60) but the inline auto-fit fires only at
// engineTickCount === auto_zoom_fit_tick_threshold (80). 60 < 80 → the engine
// stops before the camera is ever fitted → nodes off-screen → blank canvas until
// the user tab-aways/back + Resets (which reheats the sim past tick 80). This
// helper decides, inside onEngineStop, whether a catch-up fit is still owed.

describe('shouldFitOnStop', () => {
  it('returns false before the engine has actually run (settle guard)', () => {
    // tickCount below minTicks → layout never ran; do not fit (or pause).
    expect(shouldFitOnStop(0, false, 50)).toBe(false);
    expect(shouldFitOnStop(49, false, 50)).toBe(false);
  });

  it('returns true on warm-start stop (60) when fit never fired', () => {
    // The exact bug: cooldownTicks(60) stops below the 80-tick fit threshold,
    // so the inline tick-80 fit never fired → catch-up owed.
    expect(shouldFitOnStop(60, false, 50)).toBe(true);
  });

  it('returns false when a fit already fired (cold / inline-fit path)', () => {
    // Cold load runs past 80 → onEngineTick already fitted (sets zoomFitDone) →
    // no catch-up owed. zoomFitDone also guards against stomping its 800ms pan.
    expect(shouldFitOnStop(120, true, 50)).toBe(false);
  });

  it('returns true past the threshold when no fit fired (Reload-button path)', () => {
    // Reload calls loadGraph WITHOUT initGraph, so _engineTickCount is not reset
    // and is already > the inline fit threshold → the `=== 80` inline fit can
    // never fire. The catch-up must still fit, else the canvas stays blank.
    expect(shouldFitOnStop(80, false, 50)).toBe(true);
    expect(shouldFitOnStop(81, false, 50)).toBe(true);
    expect(shouldFitOnStop(180, false, 50)).toBe(true);
  });

  it('owes a fit iff settled and not yet fitted (regardless of threshold)', () => {
    expect(shouldFitOnStop(50, false, 50)).toBe(true);
    expect(shouldFitOnStop(79, false, 50)).toBe(true);
    expect(shouldFitOnStop(79, true, 50)).toBe(false);
  });
});
