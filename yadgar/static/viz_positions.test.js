/**
 * viz_positions.test.js — v5.87 C2 — Tests for viz_positions.js
 *
 * Covers the warm-start serialization / restore contract:
 *   - serializeNodePositions: only finite-position nodes; z optional (2D vs 3D)
 *   - restoreNodePositions: seeds x/y/z in place, never fx/fy; new nodes untouched
 *   - pruneStalePositions: drops saved ids absent from the fresh payload
 *   - parsePositionStore: malformed/empty JSON → {} (never throws)
 *   - round-trip: serialize → store → parse → restore is position-preserving
 *
 * Run: cd viz-tests && npx vitest run
 */

import { describe, it, expect } from 'vitest';
import {
  serializeNodePositions,
  restoreNodePositions,
  pruneStalePositions,
  parsePositionStore,
  serializePositionStore,
} from './viz_positions.js';

describe('serializeNodePositions', () => {
  it('keeps nodes with finite x and y', () => {
    const out = serializeNodePositions([{ id: 'a', x: 1, y: 2 }]);
    expect(out).toEqual({ a: { x: 1, y: 2 } });
  });

  it('includes z when finite (3D)', () => {
    const out = serializeNodePositions([{ id: 'a', x: 1, y: 2, z: 3 }]);
    expect(out).toEqual({ a: { x: 1, y: 2, z: 3 } });
  });

  it('omits z when absent (2D)', () => {
    const out = serializeNodePositions([{ id: 'a', x: 1, y: 2 }]);
    expect(out.a).not.toHaveProperty('z');
  });

  it('skips nodes missing x or y', () => {
    const out = serializeNodePositions([
      { id: 'a', x: 1 },          // no y
      { id: 'b', y: 2 },          // no x
      { id: 'c' },                // neither
      { id: 'd', x: 5, y: 6 },    // good
    ]);
    expect(out).toEqual({ d: { x: 5, y: 6 } });
  });

  it('skips NaN / Infinity positions', () => {
    const out = serializeNodePositions([
      { id: 'a', x: NaN, y: 2 },
      { id: 'b', x: 1, y: Infinity },
      { id: 'c', x: 1, y: 2, z: NaN },
    ]);
    expect(out).toEqual({ c: { x: 1, y: 2 } }); // z dropped, x/y kept
  });

  it('skips nodes with null id', () => {
    const out = serializeNodePositions([{ id: null, x: 1, y: 2 }]);
    expect(out).toEqual({});
  });

  it('handles null / empty input', () => {
    expect(serializeNodePositions(null)).toEqual({});
    expect(serializeNodePositions([])).toEqual({});
  });
});

describe('restoreNodePositions', () => {
  it('seeds x/y onto matching nodes in place', () => {
    const nodes = [{ id: 'a' }, { id: 'b' }];
    const n = restoreNodePositions(nodes, { a: { x: 10, y: 20 } });
    expect(n).toBe(1);
    expect(nodes[0]).toMatchObject({ id: 'a', x: 10, y: 20 });
    expect(nodes[1]).toEqual({ id: 'b' }); // untouched (new node → sim places it)
  });

  it('seeds z when present', () => {
    const nodes = [{ id: 'a' }];
    restoreNodePositions(nodes, { a: { x: 1, y: 2, z: 3 } });
    expect(nodes[0]).toMatchObject({ x: 1, y: 2, z: 3 });
  });

  it('never sets fx/fy/fz (layout must stay free)', () => {
    const nodes = [{ id: 'a' }];
    restoreNodePositions(nodes, { a: { x: 1, y: 2, z: 3 } });
    expect(nodes[0]).not.toHaveProperty('fx');
    expect(nodes[0]).not.toHaveProperty('fy');
    expect(nodes[0]).not.toHaveProperty('fz');
  });

  it('ignores saved records with non-finite x/y', () => {
    const nodes = [{ id: 'a' }];
    const n = restoreNodePositions(nodes, { a: { x: NaN, y: 2 } });
    expect(n).toBe(0);
    expect(nodes[0]).toEqual({ id: 'a' });
  });

  it('returns 0 for null saved map', () => {
    const nodes = [{ id: 'a' }];
    expect(restoreNodePositions(nodes, null)).toBe(0);
  });

  it('handles null nodes input', () => {
    expect(restoreNodePositions(null, { a: { x: 1, y: 2 } })).toBe(0);
  });
});

describe('pruneStalePositions', () => {
  it('drops ids absent from the current payload', () => {
    const saved = { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } };
    const out = pruneStalePositions(saved, ['a']);
    expect(out).toEqual({ a: { x: 1, y: 2 } });
  });

  it('accepts a Set of ids', () => {
    const saved = { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } };
    const out = pruneStalePositions(saved, new Set(['b']));
    expect(out).toEqual({ b: { x: 3, y: 4 } });
  });

  it('does not mutate the input', () => {
    const saved = { a: { x: 1, y: 2 }, b: { x: 3, y: 4 } };
    pruneStalePositions(saved, ['a']);
    expect(Object.keys(saved)).toEqual(['a', 'b']);
  });

  it('handles null saved / empty ids', () => {
    expect(pruneStalePositions(null, ['a'])).toEqual({});
    expect(pruneStalePositions({ a: { x: 1, y: 2 } }, [])).toEqual({});
  });
});

describe('parsePositionStore', () => {
  it('parses a valid JSON object', () => {
    expect(parsePositionStore('{"a":{"x":1,"y":2}}')).toEqual({ a: { x: 1, y: 2 } });
  });

  it('returns {} for malformed JSON', () => {
    expect(parsePositionStore('{not json')).toEqual({});
  });

  it('returns {} for null / empty / non-string', () => {
    expect(parsePositionStore(null)).toEqual({});
    expect(parsePositionStore('')).toEqual({});
    expect(parsePositionStore(undefined)).toEqual({});
  });

  it('returns {} for JSON that is not a plain object', () => {
    expect(parsePositionStore('[1,2,3]')).toEqual({});
    expect(parsePositionStore('42')).toEqual({});
    expect(parsePositionStore('null')).toEqual({});
  });
});

describe('round-trip (save → store → load → restore)', () => {
  it('preserves positions through a full localStorage cycle', () => {
    const settled = [
      { id: 'mem:1', x: 11.5, y: -22.5, z: 3.25 },
      { id: 'wiki:2', x: 100, y: 200, z: 300 },
      { id: 'entity:3', x: -5, y: -6, z: -7 },
    ];
    // save side (onEngineStop)
    const json = serializePositionStore(serializeNodePositions(settled));
    // ... persisted to localStorage ...
    // load side (next reload): fresh payload re-creates node objects without positions
    const fresh = [{ id: 'mem:1' }, { id: 'wiki:2' }, { id: 'entity:3' }];
    const saved = parsePositionStore(json);
    const n = restoreNodePositions(fresh, saved);
    expect(n).toBe(3);
    expect(fresh).toEqual(settled);
  });

  it('prunes a departed node and integrates a new one', () => {
    const json = serializePositionStore(
      serializeNodePositions([
        { id: 'old', x: 1, y: 2, z: 3 },
        { id: 'keep', x: 4, y: 5, z: 6 },
      ]),
    );
    // next payload: 'old' gone, 'new' added
    const fresh = [{ id: 'keep' }, { id: 'new' }];
    const currentIds = new Set(fresh.map((n) => n.id));
    const pruned = pruneStalePositions(parsePositionStore(json), currentIds);
    expect(pruned).toEqual({ keep: { x: 4, y: 5, z: 6 } }); // 'old' dropped
    const n = restoreNodePositions(fresh, pruned);
    expect(n).toBe(1); // only 'keep' seeded; 'new' left for the sim
    expect(fresh[0]).toMatchObject({ id: 'keep', x: 4, y: 5, z: 6 });
    expect(fresh[1]).toEqual({ id: 'new' });
  });
});
