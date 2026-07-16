/**
 * viz_filters.test.js — v5.54.3 — Tests for viz_filters.js
 *
 * Tests:
 *   - buildEdgeTypeMap: constructs role/default_on/color/lazy map from legend.edges
 *   - edgeCbKey: type key → checkbox ID
 *   - edgeVisible: toggle state drives visibility
 *   - edgeRole: role lookup from map
 *   - linksChanged: boolean gate for reheat
 *   - edgeLinkColor: role-styled colors
 *   - edgeLinkWidth: role-styled widths
 *   - render-from-source: type absent from map → default visibility
 *   - entity types: co_occurrence/imports/calls/resolved_by/caused_by togglable
 *
 * Run: cd viz-tests && npx vitest run
 */

import { describe, it, expect } from 'vitest';
import {
  buildEdgeTypeMap,
  edgeWeightOf,
  edgePassesWeight,
  edgeCbKey,
  edgeVisible,
  edgeRole,
  linksChanged,
  edgeLinkColor,
  edgeLinkWidth,
  visibleForceLinks,
  _hexToRgba,
} from './viz_filters.js';

// ── Stub legend edges (mirrors EDGE_TYPES in viz_meta.py) ────────────────────

const STUB_LEGEND_EDGES = [
  { key: 'semantic',      color: '#1f6feb', role: 'display',   default_on: false, lazy: true  },
  { key: 'temporal',      color: '#6e40c9', role: 'display',   default_on: true,  lazy: false },
  { key: 'transition',    color: '#3fb950', role: 'retrieval', default_on: true,  lazy: false },
  { key: 'wiki_crossref', color: '#d2a8ff', role: 'display',   default_on: true,  lazy: false },
  { key: 'memory_wiki',   color: '#ffa657', role: 'display',   default_on: true,  lazy: false },
  { key: 'causal',        color: '#484f58', role: 'display',   default_on: true,  lazy: false },
  { key: 'co_occurrence', color: '#e8b86d', role: 'retrieval', default_on: true,  lazy: false },
  { key: 'imports',       color: '#79c0ff', role: 'retrieval', default_on: true,  lazy: false },
  { key: 'calls',         color: '#56d364', role: 'retrieval', default_on: true,  lazy: false },
  { key: 'resolved_by',   color: '#f85149', role: 'retrieval', default_on: true,  lazy: false },
  { key: 'caused_by',     color: '#ff7b72', role: 'retrieval', default_on: true,  lazy: false },
  // viz-rest (#209): derived_from — largest entity rel type, retrieval-active, default ON
  { key: 'derived_from',  color: '#39c5cf', role: 'retrieval', default_on: true,  lazy: false },
];

// ── buildEdgeTypeMap ──────────────────────────────────────────────────────────

describe('buildEdgeTypeMap', () => {
  it('returns a map with all 12 edge types', () => {
    const map = buildEdgeTypeMap(STUB_LEGEND_EDGES);
    expect(Object.keys(map).length).toBe(12);
  });

  it('maps retrieval role correctly for entity types', () => {
    const map = buildEdgeTypeMap(STUB_LEGEND_EDGES);
    expect(map['co_occurrence'].role).toBe('retrieval');
    expect(map['imports'].role).toBe('retrieval');
    expect(map['calls'].role).toBe('retrieval');
    expect(map['resolved_by'].role).toBe('retrieval');
    expect(map['caused_by'].role).toBe('retrieval');
    expect(map['transition'].role).toBe('retrieval');
    // viz-rest (#209): derived_from is retrieval-active (feeds PPR + spreading)
    expect(map['derived_from'].role).toBe('retrieval');
    expect(map['derived_from'].default_on).toBe(true);
  });

  it('maps display role correctly for non-retrieval types', () => {
    const map = buildEdgeTypeMap(STUB_LEGEND_EDGES);
    expect(map['semantic'].role).toBe('display');
    expect(map['temporal'].role).toBe('display');
    expect(map['causal'].role).toBe('display');
    expect(map['wiki_crossref'].role).toBe('display');
    expect(map['memory_wiki'].role).toBe('display');
  });

  it('marks semantic as lazy=true, others lazy=false', () => {
    const map = buildEdgeTypeMap(STUB_LEGEND_EDGES);
    expect(map['semantic'].lazy).toBe(true);
    expect(map['co_occurrence'].lazy).toBe(false);
    expect(map['temporal'].lazy).toBe(false);
  });

  it('marks semantic default_on=false', () => {
    const map = buildEdgeTypeMap(STUB_LEGEND_EDGES);
    expect(map['semantic'].default_on).toBe(false);
  });

  it('marks non-semantic retrieval types default_on=true', () => {
    const map = buildEdgeTypeMap(STUB_LEGEND_EDGES);
    expect(map['co_occurrence'].default_on).toBe(true);
    expect(map['transition'].default_on).toBe(true);
  });

  it('handles empty legend gracefully', () => {
    const map = buildEdgeTypeMap([]);
    expect(Object.keys(map).length).toBe(0);
  });

  it('handles null/undefined legend gracefully', () => {
    expect(Object.keys(buildEdgeTypeMap(null)).length).toBe(0);
    expect(Object.keys(buildEdgeTypeMap(undefined)).length).toBe(0);
  });
});

// ── edgeCbKey ─────────────────────────────────────────────────────────────────

describe('edgeCbKey', () => {
  it('converts underscores to hyphens and prefixes fo-show-', () => {
    expect(edgeCbKey('co_occurrence')).toBe('fo-show-co-occurrence');
    expect(edgeCbKey('wiki_crossref')).toBe('fo-show-wiki-crossref');
    expect(edgeCbKey('memory_wiki')).toBe('fo-show-memory-wiki');
    expect(edgeCbKey('resolved_by')).toBe('fo-show-resolved-by');
    expect(edgeCbKey('caused_by')).toBe('fo-show-caused-by');
  });

  it('leaves types without underscores unchanged', () => {
    expect(edgeCbKey('semantic')).toBe('fo-show-semantic');
    expect(edgeCbKey('temporal')).toBe('fo-show-temporal');
    expect(edgeCbKey('causal')).toBe('fo-show-causal');
    expect(edgeCbKey('imports')).toBe('fo-show-imports');
    expect(edgeCbKey('calls')).toBe('fo-show-calls');
  });

  it('derives the fo-show-derived-from checkbox id (viz-rest #209)', () => {
    expect(edgeCbKey('derived_from')).toBe('fo-show-derived-from');
  });
});

// ── edgeVisible ───────────────────────────────────────────────────────────────

describe('edgeVisible — per-type toggle on/off', () => {
  it('returns true when toggle for type is ON', () => {
    expect(edgeVisible({ type: 'semantic' }, { semantic: true })).toBe(true);
    expect(edgeVisible({ type: 'co_occurrence' }, { co_occurrence: true })).toBe(true);
  });

  it('returns false when toggle for type is OFF', () => {
    expect(edgeVisible({ type: 'semantic' }, { semantic: false })).toBe(false);
    expect(edgeVisible({ type: 'co_occurrence' }, { co_occurrence: false })).toBe(false);
    expect(edgeVisible({ type: 'imports' }, { imports: false })).toBe(false);
    expect(edgeVisible({ type: 'calls' }, { calls: false })).toBe(false);
    expect(edgeVisible({ type: 'resolved_by' }, { resolved_by: false })).toBe(false);
    expect(edgeVisible({ type: 'caused_by' }, { caused_by: false })).toBe(false);
    // viz-rest (#209): derived_from is default-ON but user can hide it
    expect(edgeVisible({ type: 'derived_from' }, { derived_from: false })).toBe(false);
    expect(edgeVisible({ type: 'derived_from' }, { derived_from: true })).toBe(true);
  });

  it('returns true by default for types not in toggleState (render-from-source: unknown type not hardcoded hidden)', () => {
    // A type absent from the toggle state map defaults to visible
    expect(edgeVisible({ type: 'unknown_type' }, {})).toBe(true);
    expect(edgeVisible({ type: 'co_occurrence' }, {})).toBe(true);
  });

  it('all entity relation types are individually togglable', () => {
    const toggleAllOff = {
      co_occurrence: false,
      imports: false,
      calls: false,
      resolved_by: false,
      caused_by: false,
    };
    for (const t of ['co_occurrence', 'imports', 'calls', 'resolved_by', 'caused_by']) {
      expect(edgeVisible({ type: t }, toggleAllOff)).toBe(false);
    }
    const toggleAllOn = {
      co_occurrence: true,
      imports: true,
      calls: true,
      resolved_by: true,
      caused_by: true,
    };
    for (const t of ['co_occurrence', 'imports', 'calls', 'resolved_by', 'caused_by']) {
      expect(edgeVisible({ type: t }, toggleAllOn)).toBe(true);
    }
  });
});

// ── visibleForceLinks — C1 physics: force set excludes hidden edge types ──────

describe('visibleForceLinks — d3 force link set (v5.87 C1)', () => {
  const LINKS = [
    { source: 'a', target: 'b', type: 'temporal' },
    { source: 'b', target: 'c', type: 'temporal' },
    { source: 'c', target: 'd', type: 'transition' },
    { source: 'd', target: 'e', type: 'co_occurrence' },
  ];

  it('drops links whose edge type is toggled OFF (so the force no longer binds them)', () => {
    const state = { temporal: false, transition: true, co_occurrence: true };
    const out = visibleForceLinks(LINKS, state);
    expect(out.map(l => l.type)).toEqual(['transition', 'co_occurrence']);
    // No temporal links survive → their endpoints are no longer force-bound.
    expect(out.some(l => l.type === 'temporal')).toBe(false);
  });

  it('keeps all links when every type is ON', () => {
    const state = { temporal: true, transition: true, co_occurrence: true };
    expect(visibleForceLinks(LINKS, state).length).toBe(4);
  });

  it('keeps a type absent from toggle state (render-from-source default-visible)', () => {
    // co_occurrence not in state → defaults visible; temporal explicitly off
    const out = visibleForceLinks(LINKS, { temporal: false });
    expect(out.map(l => l.type)).toEqual(['transition', 'co_occurrence']);
  });

  it('handles null/undefined inputs gracefully', () => {
    expect(visibleForceLinks(null, {})).toEqual([]);
    expect(visibleForceLinks(undefined, null)).toEqual([]);
    expect(visibleForceLinks(LINKS, null).length).toBe(4); // null state → all visible
  });
});

// ── edgeRole ──────────────────────────────────────────────────────────────────

describe('edgeRole', () => {
  const map = buildEdgeTypeMap(STUB_LEGEND_EDGES);

  it('returns retrieval for entity typed-relation types', () => {
    for (const t of ['co_occurrence', 'imports', 'calls', 'resolved_by', 'caused_by', 'transition']) {
      expect(edgeRole(t, map)).toBe('retrieval');
    }
  });

  it('returns display for non-retrieval types', () => {
    for (const t of ['semantic', 'temporal', 'causal', 'wiki_crossref', 'memory_wiki']) {
      expect(edgeRole(t, map)).toBe('display');
    }
  });

  it('returns null for unknown type', () => {
    expect(edgeRole('not_a_type', map)).toBe(null);
  });

  it('returns null for null/empty inputs', () => {
    expect(edgeRole(null, map)).toBe(null);
    expect(edgeRole('', map)).toBe(null);
    expect(edgeRole('semantic', null)).toBe(null);
  });
});

// ── linksChanged ──────────────────────────────────────────────────────────────

describe('linksChanged', () => {
  it('returns true when link count changes (lazy edge append)', () => {
    expect(linksChanged(50, 100)).toBe(true);
    expect(linksChanged(0, 42)).toBe(true);
  });

  it('returns false when link count is unchanged (visibility toggle — no reheat)', () => {
    expect(linksChanged(50, 50)).toBe(false);
    expect(linksChanged(0, 0)).toBe(false);
    expect(linksChanged(100, 100)).toBe(false);
  });
});

// ── edgeLinkColor — role-styling ──────────────────────────────────────────────

describe('edgeLinkColor — role-distinguished styling', () => {
  const map = buildEdgeTypeMap(STUB_LEGEND_EDGES);

  it('retrieval edges get full-opacity color', () => {
    // co_occurrence is retrieval → returns the raw hex color (no rgba dimming)
    const color = edgeLinkColor({ type: 'co_occurrence' }, map);
    expect(color).toBe('#e8b86d');
  });

  it('display edges get reduced-opacity rgba color', () => {
    const color = edgeLinkColor({ type: 'temporal' }, map);
    expect(color).toMatch(/^rgba\(/);
    expect(color).toContain('0.45');
  });

  it('semantic (display) edge gets dimmed color', () => {
    const color = edgeLinkColor({ type: 'semantic' }, map);
    expect(color).toMatch(/^rgba\(/);
  });

  it('transition (retrieval) edge gets full-opacity color', () => {
    const color = edgeLinkColor({ type: 'transition' }, map);
    expect(color).toBe('#3fb950');
  });

  it('returns fallback for unknown type', () => {
    const color = edgeLinkColor({ type: 'unknown' }, map);
    expect(color).toContain('rgba');
  });
});

// ── edgeLinkWidth ─────────────────────────────────────────────────────────────

describe('edgeLinkWidth', () => {
  const map = buildEdgeTypeMap(STUB_LEGEND_EDGES);

  it('retrieval entity edges return 1.5', () => {
    expect(edgeLinkWidth({ type: 'co_occurrence' }, map)).toBe(1.5);
    expect(edgeLinkWidth({ type: 'imports' }, map)).toBe(1.5);
    expect(edgeLinkWidth({ type: 'calls' }, map)).toBe(1.5);
    expect(edgeLinkWidth({ type: 'resolved_by' }, map)).toBe(1.5);
    expect(edgeLinkWidth({ type: 'caused_by' }, map)).toBe(1.5);
  });

  it('transition scales with count', () => {
    const w = edgeLinkWidth({ type: 'transition', count: 4 }, map);
    expect(w).toBeGreaterThan(1);
    expect(w).toBeLessThanOrEqual(4);
  });

  it('semantic display edge returns 0.8', () => {
    expect(edgeLinkWidth({ type: 'semantic' }, map)).toBe(0.8);
  });

  it('other display edges return 1.0', () => {
    expect(edgeLinkWidth({ type: 'temporal' }, map)).toBe(1.0);
    expect(edgeLinkWidth({ type: 'causal' }, map)).toBe(1.0);
  });
});

// ── _hexToRgba ────────────────────────────────────────────────────────────────

describe('_hexToRgba', () => {
  it('converts valid hex to rgba', () => {
    expect(_hexToRgba('#1f6feb', 0.45)).toBe('rgba(31,111,235,0.45)');
    expect(_hexToRgba('#e8b86d', 1.0)).toBe('rgba(232,184,109,1)');
  });

  it('falls back on invalid hex', () => {
    const result = _hexToRgba('not-hex', 0.5);
    expect(result).toContain('rgba(130,130,130');
  });
});

// ── render-from-source: type absent from EDGE_TYPES does not crash ────────────

describe('render-from-source: unknown type handling', () => {
  it('edgeVisible with unknown type defaults to visible (no crash)', () => {
    expect(edgeVisible({ type: 'phantom_type' }, {})).toBe(true);
  });

  it('edgeRole with type absent from map returns null (not hardcoded)', () => {
    const smallMap = buildEdgeTypeMap([
      { key: 'semantic', role: 'display', default_on: false, color: '#1f6feb', lazy: true },
    ]);
    // 'co_occurrence' absent → null role (not hardcoded as retrieval)
    expect(edgeRole('co_occurrence', smallMap)).toBe(null);
  });

  it('edgeLinkColor for type absent from map returns fallback rgba', () => {
    const smallMap = buildEdgeTypeMap([
      { key: 'semantic', role: 'display', default_on: false, color: '#1f6feb', lazy: true },
    ]);
    const color = edgeLinkColor({ type: 'co_occurrence' }, smallMap);
    expect(color).toContain('rgba');
  });
});

// ── viz-rest #70: edge weight threshold filter ─────────────────────────────────

describe('edgeWeightOf', () => {
  it('returns count when present', () => {
    expect(edgeWeightOf({ type: 'transition', count: 5 })).toBe(5);
  });
  it('returns weight when count absent', () => {
    expect(edgeWeightOf({ type: 'co_occurrence', weight: 2.5 })).toBe(2.5);
  });
  it('prefers count over weight', () => {
    expect(edgeWeightOf({ count: 3, weight: 9 })).toBe(3);
  });
  it('returns null for an unweighted edge', () => {
    expect(edgeWeightOf({ type: 'temporal' })).toBe(null);
  });
  it('returns null for null input', () => {
    expect(edgeWeightOf(null)).toBe(null);
  });
});

describe('edgePassesWeight', () => {
  it('threshold 0 passes everything', () => {
    expect(edgePassesWeight({ count: 1 }, 0)).toBe(true);
    expect(edgePassesWeight({ type: 'temporal' }, 0)).toBe(true);
  });
  it('unweighted edge always passes even above threshold', () => {
    expect(edgePassesWeight({ type: 'temporal' }, 5)).toBe(true);
  });
  it('weighted edge below threshold is pruned', () => {
    expect(edgePassesWeight({ count: 1 }, 2)).toBe(false);
  });
  it('weighted edge at/above threshold passes', () => {
    expect(edgePassesWeight({ count: 2 }, 2)).toBe(true);
    expect(edgePassesWeight({ weight: 3 }, 2)).toBe(true);
  });
});
