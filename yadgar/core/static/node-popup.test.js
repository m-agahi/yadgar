import { describe, it, expect } from 'vitest';
import { popupFieldModel, clampPopupPosition, isWideType } from './node-popup.js';

const links = [
  { source: 'mem:1', target: 'wiki:2', type: 'memory_wiki' },
  { source: 'mem:1', target: 'mem:9', type: 'memory_similarity_link' },
  { source: 'wiki:2', target: 'wiki:3', type: 'wiki_crossref' },
  { source: 'entity:e', target: 'mem:1', type: 'co_occurrence' },
];

describe('popupFieldModel — memory', () => {
  const node = {
    id: 'mem:1',
    type: 'memory',
    content: 'hello memory content',
    heat: 0.82,
    tags: ['_anchor', 'release'],
    directory: '/home/max/git/yadgar',
    created_at: '2026-06-16T16:11:34Z',
    last_accessed: '2026-07-17T09:25:01Z',
  };
  it('produces a memory field model with heat + connections grouped by type', () => {
    const m = popupFieldModel(node, links);
    expect(m.type).toBe('memory');
    expect(m.badge).toBe('Memory');
    expect(m.title).toContain('hello memory');
    expect(m.showHeat).toBe(true);
    expect(m.heat).toBeCloseTo(0.82);
    expect(m.wide).toBe(false);
    // connections: mem:1 appears in 3 links (memory_wiki, similarity, co_occurrence)
    const byType = Object.fromEntries(m.connections.map((c) => [c.type, c.count]));
    expect(byType.memory_wiki).toBe(1);
    expect(byType.memory_similarity_link).toBe(1);
    expect(byType.co_occurrence).toBe(1);
    // tags + fields present
    expect(m.tags).toEqual(['_anchor', 'release']);
    expect(m.fields.find((f) => f.label === 'Project').value).toContain('/home/max');
    expect(m.nodeId).toBe('mem:1');
  });
});

describe('popupFieldModel — wiki', () => {
  const node = {
    id: 'wiki:2',
    type: 'wiki',
    label: 'Viz Triage Checklist',
    slug: 'viz-triage',
    category: 'decision',
    tags: ['viz', 'triage'],
    updated_at: '2026-07-16T18:25:28Z',
  };
  it('produces a wiki field model — no heat, category accent, wide, async slug', () => {
    const m = popupFieldModel(node, links);
    expect(m.type).toBe('wiki');
    expect(m.badge).toBe('Wiki');
    expect(m.title).toBe('Viz Triage Checklist');
    expect(m.showHeat).toBe(false);
    expect(m.wide).toBe(true); // wiki auto-widens 340→500
    expect(m.slug).toBe('viz-triage');
    expect(m.category).toBe('decision');
    // wiki:2 appears in memory_wiki + wiki_crossref
    const byType = Object.fromEntries(m.connections.map((c) => [c.type, c.count]));
    expect(byType.memory_wiki).toBe(1);
    expect(byType.wiki_crossref).toBe(1);
  });
});

describe('popupFieldModel — entity', () => {
  const node = { id: 'entity:e', type: 'entity', label: 'galaxy-view.js', entity_type: 'file' };
  it('produces an entity field model — no heat, no content', () => {
    const m = popupFieldModel(node, links);
    expect(m.type).toBe('entity');
    expect(m.badge).toBe('Entity');
    expect(m.showHeat).toBe(false);
    expect(m.wide).toBe(false);
    expect(m.title).toBe('galaxy-view.js');
    const byType = Object.fromEntries(m.connections.map((c) => [c.type, c.count]));
    expect(byType.co_occurrence).toBe(1);
  });
});

describe('isWideType', () => {
  it('only wiki widens', () => {
    expect(isWideType('wiki')).toBe(true);
    expect(isWideType('memory')).toBe(false);
    expect(isWideType('entity')).toBe(false);
  });
});

describe('clampPopupPosition', () => {
  const viewport = { width: 1000, height: 800 };
  const size = { width: 340, height: 400 };
  it('offsets +16/+16 from anchor when it fits', () => {
    expect(clampPopupPosition({ x: 100, y: 100 }, size, viewport)).toEqual({ left: 116, top: 116 });
  });
  it('clamps right edge so popup never clips', () => {
    // anchor near right edge → left clamped to width - popup.width - 8
    const pos = clampPopupPosition({ x: 980, y: 100 }, size, viewport);
    expect(pos.left).toBe(1000 - 340 - 8); // 652
  });
  it('clamps bottom edge', () => {
    const pos = clampPopupPosition({ x: 100, y: 780 }, size, viewport);
    expect(pos.top).toBe(800 - 400 - 8); // 392
  });
  it('never goes below 8 on either axis', () => {
    const pos = clampPopupPosition({ x: -50, y: -50 }, size, viewport);
    expect(pos.left).toBeGreaterThanOrEqual(8);
    expect(pos.top).toBeGreaterThanOrEqual(8);
  });
});
