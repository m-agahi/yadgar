/**
 * viz_helpers.test.js — Layer 3 JS unit tests for viz_helpers.js
 *
 * Tests pure helper functions extracted from index.html.
 * Run with: npx vitest run yadgar/static/viz_helpers.test.js
 *
 * v5.37.0 viz integration testing infrastructure.
 */

import { describe, expect, it } from 'vitest';
import {
  _fmtBytes, _fmtUptime, esc,
  aggregateEdgeCounts, edgeGroupToggleReducer, edgeGroupIsOn, sectionToggleReducer,
} from './viz_helpers.js';

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

// ── #69 unified-panel reducers ──────────────────────────────────────────────────

const LEGEND = [
  { key: 'transition', role: 'retrieval' },
  { key: 'co_occurrence', role: 'retrieval' },
  { key: 'temporal', role: 'informational' },
  { key: 'wiki_crossref', role: 'informational' },
];

describe('aggregateEdgeCounts', () => {
  it('counts per type and per role group', () => {
    const links = [
      { type: 'transition', role: 'retrieval' },
      { type: 'transition', role: 'retrieval' },
      { type: 'temporal', role: 'informational' },
    ];
    const { byType, byGroup } = aggregateEdgeCounts(links, LEGEND);
    expect(byType.transition).toBe(2);
    expect(byType.temporal).toBe(1);
    expect(byGroup.retrieval).toBe(2);
    expect(byGroup.informational).toBe(1);
  });

  it('reports declared-but-empty legend types as 0', () => {
    const { byType } = aggregateEdgeCounts([], LEGEND);
    expect(byType.transition).toBe(0);
    expect(byType.co_occurrence).toBe(0);
    expect(byType.wiki_crossref).toBe(0);
  });

  it('classifies an unknown type via its wire role, defaulting informational', () => {
    const links = [
      { type: 'mystery', role: 'retrieval' },
      { type: 'other' }, // no role → informational
    ];
    const { byType, byGroup, roleOf } = aggregateEdgeCounts(links, LEGEND);
    expect(byType.mystery).toBe(1);
    expect(roleOf.mystery).toBe('retrieval');
    expect(roleOf.other).toBe('informational');
    expect(byGroup.retrieval).toBe(1);
    expect(byGroup.informational).toBe(1);
  });

  it('handles null/empty inputs', () => {
    const { byType, byGroup } = aggregateEdgeCounts(null, null);
    expect(Object.keys(byType).length).toBe(0);
    expect(byGroup).toEqual({ retrieval: 0, informational: 0 });
  });
});

describe('edgeGroupToggleReducer', () => {
  const roleOf = { transition: 'retrieval', co_occurrence: 'retrieval', temporal: 'informational' };

  it('turning a master OFF sets all types in that group off, leaving others alone', () => {
    const next = edgeGroupToggleReducer({}, 'retrieval', false, roleOf);
    expect(next.transition).toBe(false);
    expect(next.co_occurrence).toBe(false);
    expect('temporal' in next).toBe(false); // informational untouched
  });

  it('turning a master ON sets all types in that group on', () => {
    const next = edgeGroupToggleReducer({ transition: false }, 'retrieval', true, roleOf);
    expect(next.transition).toBe(true);
    expect(next.co_occurrence).toBe(true);
  });

  it('does not mutate the input state', () => {
    const input = { transition: true };
    const next = edgeGroupToggleReducer(input, 'retrieval', false, roleOf);
    expect(input.transition).toBe(true);
    expect(next).not.toBe(input);
  });
});

describe('edgeGroupIsOn', () => {
  const roleOf = { transition: 'retrieval', co_occurrence: 'retrieval', temporal: 'informational' };

  it('is on when at least one group type is shown (missing = shown)', () => {
    expect(edgeGroupIsOn({}, 'retrieval', roleOf)).toBe(true); // all missing = shown
    expect(edgeGroupIsOn({ transition: false }, 'retrieval', roleOf)).toBe(true); // co_occurrence still on
  });

  it('is off when every group type is explicitly off', () => {
    expect(edgeGroupIsOn({ transition: false, co_occurrence: false }, 'retrieval', roleOf)).toBe(false);
  });

  it('is off for an empty group', () => {
    expect(edgeGroupIsOn({}, 'retrieval', {})).toBe(false);
  });
});

describe('sectionToggleReducer', () => {
  it('flips an unset section to expanded=true', () => {
    expect(sectionToggleReducer({}, 'edges')).toEqual({ edges: true });
  });
  it('flips an expanded section back to collapsed', () => {
    expect(sectionToggleReducer({ edges: true }, 'edges')).toEqual({ edges: false });
  });
  it('does not mutate the input', () => {
    const input = { nodes: true };
    const next = sectionToggleReducer(input, 'nodes');
    expect(input.nodes).toBe(true);
    expect(next.nodes).toBe(false);
  });
});
