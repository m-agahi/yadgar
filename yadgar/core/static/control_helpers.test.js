/**
 * control_helpers.test.js — pure helpers for the chrome-style settings redesign
 * (v5.89) + the Bug B (active-tab init) and Bug C (overlay→menu) decision helpers.
 *
 * No DOM harness for the render layer (viz convention); these are the extracted
 * pure functions, vitest-covered. Knob shape matches the real GET
 * /api/control/config contract (control.py _enrich_knob):
 *   { name, kind, current, default, source, reload, description, section,
 *     category, locked, enum_choices }
 */

import { describe, it, expect } from 'vitest';
import {
  searchKnobs,
  highlightSegments,
  categoryCounts,
  alphabeticalCategories,
  groupKnobsAlphabetical,
  deriveBadgeState,
  computePending,
  formatConfigStatus,
  shouldInitTab,
  overlaysToMenuDescriptors,
  controlKind,
  isDestructive,
  toggleArmed,
  classify428,
  categoryPendingCounts,
  pendingDiffs,
  armCountdown,
} from './control_helpers.js';

function knob(over = {}) {
  return {
    name: 'YADGAR_VIZ_NODE_SIZE_3D',
    kind: 'float',
    current: '8.0',
    default: '8.0',
    source: 'default',
    reload: 'hot_reload',
    description: 'Node size in the 3D graph.',
    section: 'viz_config',
    category: 'viz',
    locked: false,
    enum_choices: [],
    ...over,
  };
}

const SAMPLE = [
  knob({ name: 'YADGAR_VIZ_NODE_SIZE_3D', category: 'viz', description: 'Node size 3D' }),
  knob({ name: 'YADGAR_RECALL_MEMORY_QUOTA', category: 'retrieval', description: 'Memory quota', kind: 'int', current: '5', default: '5' }),
  knob({ name: 'YADGAR_WIKI_SIM_MODE', category: 'wiki', description: 'Similarity gate mode', kind: 'string', current: 'hard', default: 'hard', enum_choices: ['hard', 'soft'] }),
  knob({ name: 'YADGAR_METRICS_ENABLED', category: 'observability', description: 'Expose Prometheus metrics', kind: 'bool', current: '1', default: '1' }),
];

// ---------------------------------------------------------------------------
// searchKnobs — cross-category
// ---------------------------------------------------------------------------

describe('searchKnobs', () => {
  it('matches across ALL categories on name', () => {
    const r = searchKnobs(SAMPLE, 'wiki');
    expect(r.map(k => k.name)).toContain('YADGAR_WIKI_SIM_MODE');
  });

  it('matches on description (case-insensitive)', () => {
    const r = searchKnobs(SAMPLE, 'prometheus');
    expect(r.map(k => k.name)).toEqual(['YADGAR_METRICS_ENABLED']);
  });

  it('matches on category', () => {
    const r = searchKnobs(SAMPLE, 'retrieval');
    expect(r.map(k => k.name)).toEqual(['YADGAR_RECALL_MEMORY_QUOTA']);
  });

  it('empty query returns all (cross-category)', () => {
    expect(searchKnobs(SAMPLE, '')).toHaveLength(SAMPLE.length);
  });

  it('no match returns empty array', () => {
    expect(searchKnobs(SAMPLE, 'zzzznotathing')).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// highlightSegments
// ---------------------------------------------------------------------------

describe('highlightSegments', () => {
  it('returns a single unmarked segment when query empty', () => {
    expect(highlightSegments('hello', '')).toEqual([{ text: 'hello', mark: false }]);
  });

  it('marks the matched substring (case-insensitive)', () => {
    expect(highlightSegments('Prometheus Metrics', 'metric')).toEqual([
      { text: 'Prometheus ', mark: false },
      { text: 'Metric', mark: true },
      { text: 's', mark: false },
    ]);
  });

  it('marks every occurrence', () => {
    expect(highlightSegments('aXaXa', 'x')).toEqual([
      { text: 'a', mark: false },
      { text: 'X', mark: true },
      { text: 'a', mark: false },
      { text: 'X', mark: true },
      { text: 'a', mark: false },
    ]);
  });

  it('no match returns the whole string unmarked', () => {
    expect(highlightSegments('abc', 'z')).toEqual([{ text: 'abc', mark: false }]);
  });
});

// ---------------------------------------------------------------------------
// categoryCounts + alphabeticalCategories
// ---------------------------------------------------------------------------

describe('categoryCounts', () => {
  it('counts knobs per category', () => {
    const c = categoryCounts(SAMPLE);
    expect(c).toEqual({ viz: 1, retrieval: 1, wiki: 1, observability: 1 });
  });

  it('treats missing category as config', () => {
    const c = categoryCounts([knob({ category: undefined })]);
    expect(c).toEqual({ config: 1 });
  });
});

describe('alphabeticalCategories', () => {
  it('returns categories sorted ALPHABETICALLY with counts and labels', () => {
    const cats = alphabeticalCategories(SAMPLE);
    expect(cats.map(c => c.category)).toEqual(['observability', 'retrieval', 'viz', 'wiki']);
    expect(cats[0]).toEqual({ category: 'observability', label: 'Observability', count: 1 });
  });

  it('Title-cases hyphenated categories', () => {
    const cats = alphabeticalCategories([knob({ category: 'write-path' }), knob({ category: 'brain-dynamics' })]);
    expect(cats.map(c => c.label)).toEqual(['Brain Dynamics', 'Write Path']);
  });
});

// ---------------------------------------------------------------------------
// groupKnobsAlphabetical — categories alpha; sections within
// ---------------------------------------------------------------------------

describe('groupKnobsAlphabetical', () => {
  it('groups by category alphabetically, sections then knobs alpha within', () => {
    const knobs = [
      knob({ name: 'YADGAR_VIZ_B', category: 'viz', section: 'viz_config' }),
      knob({ name: 'YADGAR_VIZ_A', category: 'viz', section: 'viz_config' }),
      knob({ name: 'YADGAR_ALPHA', category: 'ops', section: 'core' }),
    ];
    const groups = groupKnobsAlphabetical(knobs);
    expect(groups.map(g => g.category)).toEqual(['ops', 'viz']);
    const viz = groups.find(g => g.category === 'viz');
    expect(viz.count).toBe(2);
    expect(viz.sections[0].knobs.map(k => k.name)).toEqual(['YADGAR_VIZ_A', 'YADGAR_VIZ_B']);
  });

  it('empty input → empty array', () => {
    expect(groupKnobsAlphabetical([])).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// deriveBadgeState — 3-way
// ---------------------------------------------------------------------------

describe('deriveBadgeState', () => {
  it('default → grey, editable, not resettable', () => {
    expect(deriveBadgeState(knob({ source: 'default', locked: false }))).toEqual({
      state: 'default', label: 'Default', editable: true, resettable: false, locked: false,
    });
  });

  it('yaml → green, editable AND resettable', () => {
    expect(deriveBadgeState(knob({ source: 'yaml', locked: false }))).toEqual({
      state: 'yaml', label: 'YAML', editable: true, resettable: true, locked: false,
    });
  });

  it('env → red, NOT editable, NOT resettable, locked', () => {
    expect(deriveBadgeState(knob({ source: 'env', locked: true }))).toEqual({
      state: 'env', label: 'ENV', editable: false, resettable: false, locked: true,
    });
  });

  it('locked flag forces env even if source omitted', () => {
    const b = deriveBadgeState(knob({ source: 'default', locked: true }));
    expect(b.state).toBe('env');
    expect(b.editable).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// computePending — diff reducer
// ---------------------------------------------------------------------------

describe('computePending', () => {
  const knobs = [
    knob({ name: 'A', reload: 'hot_reload' }),
    knob({ name: 'B', reload: 'restart_required' }),
  ];

  it('no edits → count 0, no restart, destructiveCount 0', () => {
    const p = computePending(knobs, { A: '8.0', B: '8.0' }, { A: '8.0', B: '8.0' });
    expect(p.count).toBe(0);
    expect(p.restartRequired).toBe(false);
    expect(p.destructiveCount).toBe(0);
    expect([...p.dirty]).toEqual([]);
  });

  it('one hot-reload edit → count 1, no restart', () => {
    const p = computePending(knobs, { A: '8.0', B: '8.0' }, { A: '9.0', B: '8.0' });
    expect(p.count).toBe(1);
    expect([...p.dirty]).toEqual(['A']);
    expect(p.restartRequired).toBe(false);
  });

  it('a restart-required knob edited → restartRequired true', () => {
    const p = computePending(knobs, { A: '8.0', B: '8.0' }, { A: '8.0', B: '9.0' });
    expect(p.count).toBe(1);
    expect(p.restartRequired).toBe(true);
  });

  it('counts dirty destructive knobs in destructiveCount', () => {
    const kk = [
      knob({ name: 'A', reload: 'hot_reload', destructive: true }),
      knob({ name: 'B', reload: 'restart_required', destructive: false }),
      knob({ name: 'C', reload: 'hot_reload', destructive: true }),
    ];
    // A + C edited (both destructive), B unchanged → destructiveCount 2, count 2.
    const p = computePending(kk, { A: '8', B: '8', C: '8' }, { A: '9', B: '8', C: '9' });
    expect(p.count).toBe(2);
    expect(p.destructiveCount).toBe(2);
  });

  it('a non-destructive dirty knob does not raise destructiveCount', () => {
    const kk = [knob({ name: 'A', destructive: false })];
    const p = computePending(kk, { A: '8' }, { A: '9' });
    expect(p.count).toBe(1);
    expect(p.destructiveCount).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// Car D — isDestructive
// ---------------------------------------------------------------------------

describe('isDestructive', () => {
  it('true when knob.destructive is true', () => {
    expect(isDestructive(knob({ destructive: true }))).toBe(true);
  });
  it('false when destructive is false / missing', () => {
    expect(isDestructive(knob({ destructive: false }))).toBe(false);
    expect(isDestructive(knob({ destructive: undefined }))).toBe(false);
    expect(isDestructive({})).toBe(false);
  });
  it('coerces truthy/falsy to a strict boolean', () => {
    expect(isDestructive({ destructive: 1 })).toBe(true);
    expect(isDestructive({ destructive: 0 })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Car D — toggleArmed (armed-state reducer for destructive rows)
// ---------------------------------------------------------------------------

describe('toggleArmed', () => {
  it('arms a row (returns a new Set containing it)', () => {
    const next = toggleArmed(new Set(), 'YADGAR_X', true);
    expect(next.has('YADGAR_X')).toBe(true);
  });
  it('disarms a row', () => {
    const next = toggleArmed(new Set(['YADGAR_X']), 'YADGAR_X', false);
    expect(next.has('YADGAR_X')).toBe(false);
  });
  it('does not mutate the input set (pure)', () => {
    const orig = new Set(['A']);
    const next = toggleArmed(orig, 'B', true);
    expect([...orig]).toEqual(['A']);
    expect(next.has('B')).toBe(true);
    expect(next.has('A')).toBe(true);
  });
  it('arming an already-armed row is idempotent', () => {
    const next = toggleArmed(new Set(['A']), 'A', true);
    expect([...next]).toEqual(['A']);
  });
});

// ---------------------------------------------------------------------------
// Car D — classify428 (defensive 428 → needs-arming classifier)
// ---------------------------------------------------------------------------

describe('classify428', () => {
  it('classifies a 428 destructive response as needs-arming', () => {
    const r = classify428({ status: 428, body: { destructive: true, hint: 'resend with "armed": true' } });
    expect(r.needsArming).toBe(true);
    expect(r.hint).toContain('armed');
  });
  it('a non-428 response is not needs-arming', () => {
    expect(classify428({ status: 200, body: {} }).needsArming).toBe(false);
    expect(classify428({ status: 409, body: {} }).needsArming).toBe(false);
  });
  it('428 without a destructive flag still needs arming (defensive)', () => {
    // Some proxies may strip the body; a 428 status alone implies arming.
    expect(classify428({ status: 428, body: {} }).needsArming).toBe(true);
  });
  it('tolerates a missing body', () => {
    expect(classify428({ status: 428 }).needsArming).toBe(true);
    expect(classify428({ status: 200 }).needsArming).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// controlKind — control type from knob
// ---------------------------------------------------------------------------

describe('controlKind', () => {
  it('bool → toggle', () => expect(controlKind(knob({ kind: 'bool' }))).toBe('toggle'));
  it('int → slider', () => expect(controlKind(knob({ kind: 'int' }))).toBe('slider'));
  it('float → slider', () => expect(controlKind(knob({ kind: 'float' }))).toBe('slider'));
  it('string with enum_choices → select', () =>
    expect(controlKind(knob({ kind: 'string', enum_choices: ['a', 'b'] }))).toBe('select'));
  it('free string → text', () =>
    expect(controlKind(knob({ kind: 'string', enum_choices: [] }))).toBe('text'));
});

// ---------------------------------------------------------------------------
// Bug B — shouldInitTab
// ---------------------------------------------------------------------------

describe('shouldInitTab (Bug B)', () => {
  it('inits when the active tab matches the lazy tab', () => {
    expect(shouldInitTab('control', 'control')).toBe(true);
  });
  it('does NOT init when active tab is home (avoids gated 403 probe)', () => {
    expect(shouldInitTab('home', 'control')).toBe(false);
  });
  it('config-ref active inits config-ref, not control', () => {
    expect(shouldInitTab('config-ref', 'config-ref')).toBe(true);
    expect(shouldInitTab('config-ref', 'control')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Bug C — overlaysToMenuDescriptors
// ---------------------------------------------------------------------------

describe('overlaysToMenuDescriptors (Bug C)', () => {
  function fakeOverlay(name, gripText, hidden) {
    return {
      getAttribute: (a) => (a === 'data-overlay-name' ? name : null),
      classList: { contains: (c) => (c === 'overlay-hidden' ? hidden : false) },
      querySelector: () => ({ textContent: gripText }),
    };
  }

  it('produces one descriptor per overlay (all 5)', () => {
    const els = [
      fakeOverlay('heat-slider', '⋮ Heat Filter −', false),
      fakeOverlay('graph-stats', '⋮ Graph Stats −', false),
      fakeOverlay('node-types', '⋮ Node Types −', false),
      fakeOverlay('edge-legend', '⋮ Edge Types −', false),
      fakeOverlay('clusters', '⋮ Memory Clusters −', true),
    ];
    const d = overlaysToMenuDescriptors(els);
    expect(d).toHaveLength(5);
    expect(d.map(x => x.name)).toEqual(['heat-slider', 'graph-stats', 'node-types', 'edge-legend', 'clusters']);
  });

  it('derives label from the overlay grip title (strips the ⋮ and collapse glyph)', () => {
    const d = overlaysToMenuDescriptors([fakeOverlay('heat-slider', '⋮ Heat Filter −', false)]);
    expect(d[0].label).toBe('Heat Filter');
  });

  it('reflects current visibility (overlay-hidden → checked=false)', () => {
    const d = overlaysToMenuDescriptors([
      fakeOverlay('node-types', '⋮ Node Types', false),
      fakeOverlay('clusters', '⋮ Memory Clusters', true),
    ]);
    expect(d.find(x => x.name === 'node-types').checked).toBe(true);
    expect(d.find(x => x.name === 'clusters').checked).toBe(false);
  });

  it('falls back to the overlay name when no grip text', () => {
    const el = {
      getAttribute: (a) => (a === 'data-overlay-name' ? 'edge-legend' : null),
      classList: { contains: () => false },
      querySelector: () => null,
    };
    expect(overlaysToMenuDescriptors([el])[0].label).toBe('edge-legend');
  });
});

// ── viz-rest #29: formatConfigStatus ───────────────────────────────────────────

describe('formatConfigStatus', () => {
  it('shows version + no-pending when clean', () => {
    expect(formatConfigStatus('5.146.0', 0, false)).toBe('v5.146.0 · no pending changes');
  });
  it('shows pending count', () => {
    expect(formatConfigStatus('5.146.0', 2, false)).toBe('v5.146.0 · 2 pending');
  });
  it('appends restart indicator', () => {
    expect(formatConfigStatus('5.146.0', 1, true)).toBe('v5.146.0 · 1 pending · ↻ restart');
  });
  it('strips a leading v from the version', () => {
    expect(formatConfigStatus('v5.146.0', 0, false)).toBe('v5.146.0 · no pending changes');
  });
  it('omits version when unknown', () => {
    expect(formatConfigStatus('', 3, false)).toBe('3 pending');
  });

  // Surface 2 (neural-console restyle): an optional 4th arg surfaces destructive
  // count. Omitted / 0 → identical output to the 3-arg form (back-compat).
  it('appends a destructive segment when destructiveCount > 0', () => {
    expect(formatConfigStatus('5.146.0', 2, false, 1)).toBe('v5.146.0 · 2 pending · 1 destructive');
  });
  it('destructiveCount 0 or omitted keeps the legacy 3-arg output', () => {
    expect(formatConfigStatus('5.146.0', 2, false, 0)).toBe('v5.146.0 · 2 pending');
    expect(formatConfigStatus('5.146.0', 2, false)).toBe('v5.146.0 · 2 pending');
  });
  it('orders segments version · pending · destructive · restart', () => {
    expect(formatConfigStatus('5.146.0', 2, true, 1)).toBe('v5.146.0 · 2 pending · 1 destructive · ↻ restart');
  });
});

// ── Surface 2: categoryPendingCounts ───────────────────────────────────────────
// Per-category dirty counts for the left-rail pending badges. Reuses the same
// string-comparison dirty logic as computePending so the rail can never diverge
// from the tray / header count.

describe('categoryPendingCounts', () => {
  const knobs = [
    knob({ name: 'A', category: 'viz' }),
    knob({ name: 'B', category: 'viz' }),
    knob({ name: 'C', category: 'wiki' }),
    knob({ name: 'D', category: undefined }),  // → 'config'
  ];

  it('no edits → empty map', () => {
    const state = { knobs, originalValues: { A: '1', B: '1', C: '1', D: '1' }, currentValues: { A: '1', B: '1', C: '1', D: '1' } };
    expect(categoryPendingCounts(state)).toEqual({});
  });

  it('counts dirty knobs bucketed by category', () => {
    const state = {
      knobs,
      originalValues: { A: '1', B: '1', C: '1', D: '1' },
      currentValues:  { A: '2', B: '1', C: '9', D: '1' },  // A (viz) + C (wiki) dirty
    };
    expect(categoryPendingCounts(state)).toEqual({ viz: 1, wiki: 1 });
  });

  it('two dirty in the same category sum', () => {
    const state = {
      knobs,
      originalValues: { A: '1', B: '1', C: '1', D: '1' },
      currentValues:  { A: '2', B: '3', C: '1', D: '1' },  // A + B both viz
    };
    expect(categoryPendingCounts(state)).toEqual({ viz: 2 });
  });

  it('missing knob category buckets under config', () => {
    const state = {
      knobs,
      originalValues: { A: '1', B: '1', C: '1', D: '1' },
      currentValues:  { A: '1', B: '1', C: '1', D: '9' },  // D → config
    };
    expect(categoryPendingCounts(state)).toEqual({ config: 1 });
  });
});

// ── Surface 2: pendingDiffs ────────────────────────────────────────────────────
// One descriptor per dirty knob for the commit tray: name, old→new, restart flag,
// destructive flag. Ordering follows the knobs array (stable render order).

describe('pendingDiffs', () => {
  const knobs = [
    knob({ name: 'A', reload: 'hot_reload', destructive: false }),
    knob({ name: 'B', reload: 'restart_required', destructive: false }),
    knob({ name: 'C', reload: 'hot_reload', destructive: true }),
  ];

  it('no edits → empty array', () => {
    const state = { knobs, originalValues: { A: '1', B: '1', C: '1' }, currentValues: { A: '1', B: '1', C: '1' } };
    expect(pendingDiffs(state)).toEqual([]);
  });

  it('emits a diff descriptor per dirty knob with old→new', () => {
    const state = {
      knobs,
      originalValues: { A: '1', B: '2', C: '3' },
      currentValues:  { A: '9', B: '2', C: '3' },
    };
    expect(pendingDiffs(state)).toEqual([
      { name: 'A', oldValue: '1', newValue: '9', restart: false, destructive: false },
    ]);
  });

  it('flags restart and destructive', () => {
    const state = {
      knobs,
      originalValues: { A: '1', B: '1', C: '1' },
      currentValues:  { A: '1', B: '2', C: '2' },  // B restart, C destructive
    };
    const diffs = pendingDiffs(state);
    expect(diffs.map(d => d.name)).toEqual(['B', 'C']);
    expect(diffs.find(d => d.name === 'B').restart).toBe(true);
    expect(diffs.find(d => d.name === 'C').destructive).toBe(true);
  });

  it('preserves knobs-array order (stable render)', () => {
    const state = {
      knobs,
      originalValues: { A: '1', B: '1', C: '1' },
      currentValues:  { A: '2', B: '2', C: '2' },
    };
    expect(pendingDiffs(state).map(d => d.name)).toEqual(['A', 'B', 'C']);
  });
});

// ── Surface 2: armCountdown ────────────────────────────────────────────────────
// Given an expiry timestamp (ms) and a now (ms), compute the remaining whole
// seconds and whether the arm has expired. Used by the destructive-row arm button
// countdown label ("expires in Ns"). PRESENTATION ONLY — never feeds armed:true.

describe('armCountdown', () => {
  it('returns whole seconds remaining, not expired', () => {
    expect(armCountdown(10000, 3000)).toEqual({ seconds: 7, expired: false });
  });
  it('rounds up a partial second so the label never shows 0 while live', () => {
    expect(armCountdown(10000, 3500)).toEqual({ seconds: 7, expired: false });  // 6.5s → 7
  });
  it('exactly at expiry → 0 and expired', () => {
    expect(armCountdown(10000, 10000)).toEqual({ seconds: 0, expired: true });
  });
  it('past expiry → 0 and expired (never negative)', () => {
    expect(armCountdown(10000, 12000)).toEqual({ seconds: 0, expired: true });
  });
  it('a null / undefined expiry is treated as not-armed → expired', () => {
    expect(armCountdown(null, 1000)).toEqual({ seconds: 0, expired: true });
    expect(armCountdown(undefined, 1000)).toEqual({ seconds: 0, expired: true });
  });
});
