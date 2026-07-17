import { describe, it, expect } from 'vitest';
import { parseNodeRef, buildSearchResults, routeSearchResult } from './viz-search.js';

describe('parseNodeRef', () => {
  it('parses mem: prefix', () => {
    expect(parseNodeRef('mem:530093')).toEqual({ type: 'memory', rawId: '530093' });
  });
  it('parses wiki: prefix', () => {
    expect(parseNodeRef('wiki:6782')).toEqual({ type: 'wiki', rawId: '6782' });
  });
  it('parses entity: prefix (id may contain colons)', () => {
    expect(parseNodeRef('entity:galaxy-view.js')).toEqual({
      type: 'entity',
      rawId: 'galaxy-view.js',
    });
  });
  it('returns unknown type for an unprefixed id', () => {
    expect(parseNodeRef('12345')).toEqual({ type: '', rawId: '12345' });
  });
  it('handles null/empty', () => {
    expect(parseNodeRef('')).toEqual({ type: '', rawId: '' });
    expect(parseNodeRef(null)).toEqual({ type: '', rawId: '' });
  });
});

describe('buildSearchResults', () => {
  const nodesById = new Map([
    ['mem:1', { id: 'mem:1', type: 'memory', content: 'hello world memory', heat: 0.8 }],
    ['wiki:2', { id: 'wiki:2', type: 'wiki', label: 'Viz Triage', slug: 'viz-triage', category: 'decision' }],
    ['entity:e', { id: 'entity:e', type: 'entity', label: 'galaxy-view.js' }],
  ]);

  it('resolves in-graph nodes with type + title + node ref', () => {
    const rows = buildSearchResults(['mem:1', 'wiki:2', 'entity:e'], nodesById);
    expect(rows).toHaveLength(3);
    expect(rows[0]).toMatchObject({ id: 'mem:1', type: 'memory', inGraph: true });
    expect(rows[0].title).toContain('hello world');
    expect(rows[1]).toMatchObject({ id: 'wiki:2', type: 'wiki', slug: 'viz-triage', inGraph: true });
    expect(rows[1].title).toBe('Viz Triage');
    expect(rows[2]).toMatchObject({ id: 'entity:e', type: 'entity', inGraph: true });
  });

  it('handles out-of-graph hits via prefix parsing (500-node cap)', () => {
    const rows = buildSearchResults(['mem:999', 'wiki:888'], nodesById);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({ id: 'mem:999', type: 'memory', inGraph: false, node: null });
    // wiki out-of-graph has no slug → routing must degrade gracefully
    expect(rows[1]).toMatchObject({ id: 'wiki:888', type: 'wiki', inGraph: false, node: null });
    expect(rows[1].slug).toBeNull();
  });

  it('preserves order and dedupes', () => {
    const rows = buildSearchResults(['mem:1', 'mem:1', 'wiki:2'], nodesById);
    expect(rows.map((r) => r.id)).toEqual(['mem:1', 'wiki:2']);
  });

  it('returns [] for empty input', () => {
    expect(buildSearchResults([], nodesById)).toEqual([]);
    expect(buildSearchResults(null, nodesById)).toEqual([]);
  });
});

describe('routeSearchResult', () => {
  it('routes a memory result to graph focus', () => {
    const row = { id: 'mem:1', type: 'memory', node: { id: 'mem:1' }, inGraph: true };
    expect(routeSearchResult(row)).toEqual({ action: 'focus-graph', nodeId: 'mem:1' });
  });
  it('routes an entity result to graph focus', () => {
    const row = { id: 'entity:e', type: 'entity', node: { id: 'entity:e' }, inGraph: true };
    expect(routeSearchResult(row)).toEqual({ action: 'focus-graph', nodeId: 'entity:e' });
  });
  it('routes a wiki result with slug to open-wiki', () => {
    const row = { id: 'wiki:2', type: 'wiki', slug: 'viz-triage', inGraph: true };
    expect(routeSearchResult(row)).toEqual({ action: 'open-wiki', slug: 'viz-triage', nodeId: 'wiki:2' });
  });
  it('wiki without slug but in graph falls back to focus-graph', () => {
    const row = { id: 'wiki:2', type: 'wiki', slug: null, inGraph: true, node: { id: 'wiki:2' } };
    expect(routeSearchResult(row)).toEqual({ action: 'focus-graph', nodeId: 'wiki:2' });
  });
  it('out-of-graph node with no slug → none action', () => {
    const row = { id: 'mem:999', type: 'memory', node: null, inGraph: false, slug: null };
    expect(routeSearchResult(row)).toEqual({ action: 'none', nodeId: 'mem:999' });
  });
});
