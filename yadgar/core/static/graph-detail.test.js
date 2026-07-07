/**
 * graph-detail.test.js — v5.50.12
 *
 * Unit tests for graph-detail.js: detail panel reset/selection-guard,
 * nodeType normalisation, and SSE node ingestion helpers.
 *
 * Run via: cd viz-tests && npx vitest run
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  nodeType,
  createDetailPanel,
  ingestSseNode,
  removeSseNode,
} from './graph-detail.js';

// ── DOM helpers ───────────────────────────────────────────────────────────────

/**
 * Build a minimal detail-panel DOM in jsdom.
 * Returns a teardown function to reset after each test.
 */
function buildDom() {
  document.body.innerHTML = `
    <div id="right"></div>
    <span id="det-type"></span>
    <span id="det-title"></span>
    <div id="det-body"></div>
    <div id="det-heat-bar"><div id="det-heat-fill" style="width:50%;background:red"></div></div>
  `;
}

// ── nodeType normalisation ─────────────────────────────────────────────────────

describe('nodeType()', () => {
  it('returns "wiki" for exact lowercase', () => {
    expect(nodeType({ type: 'wiki' })).toBe('wiki');
  });

  it('normalises "Wiki" (mixed case) to "wiki"', () => {
    expect(nodeType({ type: 'Wiki' })).toBe('wiki');
  });

  it('normalises "WIKI" (upper) to "wiki"', () => {
    expect(nodeType({ type: 'WIKI' })).toBe('wiki');
  });

  it('normalises " wiki " (leading/trailing space) to "wiki"', () => {
    expect(nodeType({ type: ' wiki ' })).toBe('wiki');
  });

  it('returns "memory" for exact lowercase', () => {
    expect(nodeType({ type: 'memory' })).toBe('memory');
  });

  it('normalises "MEMORY" to "memory"', () => {
    expect(nodeType({ type: 'MEMORY' })).toBe('memory');
  });

  it('normalises "Entity" to "entity"', () => {
    expect(nodeType({ type: 'Entity' })).toBe('entity');
  });

  it('returns "" for node with undefined type', () => {
    expect(nodeType({})).toBe('');
  });

  it('returns "" for node with null type', () => {
    expect(nodeType({ type: null })).toBe('');
  });

  it('returns "" for node with numeric type zero', () => {
    expect(nodeType({ type: 0 })).toBe('');
  });
});

// ── Panel reset: wiki → memory ────────────────────────────────────────────────
//
// After a wiki selection, then a memory selection:
//   - det-type must be "MEMORY" (not the prior "WIKI")
//   - det-title must be memory content (not the prior wiki title/slug)
//   - no text from the prior wiki node anywhere in the panel
//
// NOTE: this is a regression guard. In the pre-fix code the memory branch
// already overwrites these elements synchronously, so this sequence is
// actually clean in raw current code — the REAL bug is wiki→wiki with a late
// async fetch. This test stays as a permanent guard against future regressions.

describe('showDetail — wiki → memory sequence', () => {
  let showDetail;
  let fetchWikiContent;

  beforeEach(() => {
    buildDom();
    const wikiCatColor = () => ({ architecture: '#f0f' });
    const heatColorFn = (h) => `hsl(${h * 100},60%,50%)`;
    const allLinksFn = () => [];
    // fetchImpl that never resolves (simulates pending network)
    const fetchImpl = () => new Promise(() => {});
    ({ showDetail, fetchWikiContent: fetchWikiContent } = createDetailPanel({
      wikiCatColor,
      heatColorFn,
      allLinksFn,
      fetchImpl,
    }));
  });

  it('det-type is MEMORY after wiki→memory', () => {
    const wikiNode = { type: 'wiki', id: 'wiki:arch', slug: 'arch-decisions', label: 'Architecture', category: 'architecture' };
    const memNode  = { type: 'memory', id: 'mem:42', content: 'YADGAR DEV WORKFLOW notes', heat: 0.8 };

    showDetail(wikiNode);
    showDetail(memNode);

    expect(document.getElementById('det-type').textContent).toBe('MEMORY');
  });

  it('det-title is memory content after wiki→memory', () => {
    const wikiNode = { type: 'wiki', id: 'wiki:arch', slug: 'arch-decisions', label: 'Architecture', category: 'architecture' };
    const memNode  = { type: 'memory', id: 'mem:42', content: 'YADGAR DEV WORKFLOW notes', heat: 0.8 };

    showDetail(wikiNode);
    showDetail(memNode);

    expect(document.getElementById('det-title').textContent).toBe('YADGAR DEV WORKFLOW notes');
  });

  it('wiki slug is NOT present anywhere in panel after wiki→memory', () => {
    const wikiNode = { type: 'wiki', id: 'wiki:arch', slug: 'arch-decisions', label: 'Architecture', category: 'architecture' };
    const memNode  = { type: 'memory', id: 'mem:42', content: 'YADGAR DEV WORKFLOW notes', heat: 0.8 };

    showDetail(wikiNode);
    showDetail(memNode);

    const panelText = document.getElementById('right').textContent +
                      document.getElementById('det-type').textContent +
                      document.getElementById('det-title').textContent +
                      document.getElementById('det-body').innerHTML;
    expect(panelText).not.toContain('arch-decisions');
    expect(panelText).not.toContain('Architecture');
  });
});

describe('showDetail — memory → wiki sequence', () => {
  let showDetail;

  beforeEach(() => {
    buildDom();
    const wikiCatColor = () => ({ architecture: '#f0f' });
    const heatColorFn = (h) => `hsl(${h * 100},60%,50%)`;
    const allLinksFn = () => [];
    const fetchImpl = () => new Promise(() => {});
    ({ showDetail } = createDetailPanel({
      wikiCatColor,
      heatColorFn,
      allLinksFn,
      fetchImpl,
    }));
  });

  it('det-type is WIKI after memory→wiki', () => {
    const memNode  = { type: 'memory', id: 'mem:42', content: 'some memory content', heat: 0.5 };
    const wikiNode = { type: 'wiki', id: 'wiki:arch', slug: 'arch-decisions', label: 'Architecture', category: 'architecture' };

    showDetail(memNode);
    showDetail(wikiNode);

    expect(document.getElementById('det-type').textContent).toBe('WIKI');
  });

  it('det-title is wiki label after memory→wiki', () => {
    const memNode  = { type: 'memory', id: 'mem:42', content: 'some memory content', heat: 0.5 };
    const wikiNode = { type: 'wiki', id: 'wiki:arch', slug: 'arch-decisions', label: 'Architecture', category: 'architecture' };

    showDetail(memNode);
    showDetail(wikiNode);

    expect(document.getElementById('det-title').textContent).toBe('Architecture');
  });

  it('memory content is NOT present anywhere in panel after memory→wiki', () => {
    const memNode  = { type: 'memory', id: 'mem:42', content: 'some memory content', heat: 0.5 };
    const wikiNode = { type: 'wiki', id: 'wiki:arch', slug: 'arch-decisions', label: 'Architecture', category: 'architecture' };

    showDetail(memNode);
    showDetail(wikiNode);

    const panelText = document.getElementById('det-type').textContent +
                      document.getElementById('det-title').textContent +
                      document.getElementById('det-body').innerHTML;
    expect(panelText).not.toContain('some memory content');
  });
});

// ── Selection guard: late wiki fetch must NOT bleed into a newer selection ────
//
// This is the REAL repro of the reported bug:
// wiki A selected → wiki B selected → A's _fetchWikiContent resolves late
// → A's content must NOT overwrite B's panel.

describe('selectionId guard — late wiki fetch cannot overwrite newer selection', () => {
  let showDetail;
  let resolveA;
  let resolveB;

  beforeEach(() => {
    buildDom();
    const wikiCatColor = () => ({});
    const heatColorFn = () => '#888';
    const allLinksFn = () => [];

    // fetchImpl returns different promises per slug so we can resolve them independently
    const fetchImpl = (url) => {
      if (url.includes('slug-a')) {
        return new Promise((res) => { resolveA = res; });
      }
      return new Promise((res) => { resolveB = res; });
    };

    ({ showDetail } = createDetailPanel({
      wikiCatColor,
      heatColorFn,
      allLinksFn,
      fetchImpl,
    }));
  });

  it('A fetch resolving after B is selected does NOT overwrite B panel content', async () => {
    const wikiA = { type: 'wiki', id: 'wiki:a', slug: 'slug-a', label: 'Wiki A', category: 'general' };
    const wikiB = { type: 'wiki', id: 'wiki:b', slug: 'slug-b', label: 'Wiki B', category: 'general' };

    // Select A — triggers fetch for slug-a
    showDetail(wikiA);
    // Select B (newer) — triggers fetch for slug-b, increments selectionId
    showDetail(wikiB);

    // Now resolve A's fetch with distinct content
    resolveA({
      ok: true,
      json: async () => ({ content: 'CONTENT FROM WIKI A — must not appear' }),
    });
    // Let microtasks flush
    await new Promise((r) => setTimeout(r, 0));

    // wiki-content-body should still be "Loading…" (B's fetch pending) or B's content;
    // it must NOT contain A's content.
    const body = document.getElementById('wiki-content-body');
    // Body element might be gone if B replaced det-body; if it exists, must not have A's content
    if (body) {
      expect(body.textContent).not.toContain('CONTENT FROM WIKI A — must not appear');
    }
    // det-title must still show B's label, not A's
    expect(document.getElementById('det-title').textContent).toBe('Wiki B');
  });

  it('A fetch resolving after B is selected does NOT touch det-type', async () => {
    const wikiA = { type: 'wiki', id: 'wiki:a', slug: 'slug-a', label: 'Wiki A', category: 'general' };
    const wikiB = { type: 'wiki', id: 'wiki:b', slug: 'slug-b', label: 'Wiki B', category: 'general' };

    showDetail(wikiA);
    showDetail(wikiB);

    resolveA({
      ok: true,
      json: async () => ({ content: 'A content' }),
    });
    await new Promise((r) => setTimeout(r, 0));

    // det-type must still be WIKI (B's render), not mutated by late A fetch
    expect(document.getElementById('det-type').textContent).toBe('WIKI');
  });
});

// ── Panel reset: det-heat-fill cleared before branching ──────────────────────

describe('showDetail — full panel reset before branching', () => {
  let showDetail;

  beforeEach(() => {
    buildDom();
    // Pre-poison the fill element with stale state
    const fill = document.getElementById('det-heat-fill');
    fill.style.width = '80%';
    fill.style.background = 'red';

    const wikiCatColor = () => ({});
    const heatColorFn = (h) => `hsl(${h},60%,50%)`;
    const allLinksFn = () => [];
    const fetchImpl = () => new Promise(() => {});
    ({ showDetail } = createDetailPanel({
      wikiCatColor,
      heatColorFn,
      allLinksFn,
      fetchImpl,
    }));
  });

  it('det-heat-fill is reset to 0% before memory branch renders', () => {
    // After calling showDetail, fill.style.width should reflect memory heat
    // (not the pre-poisoned 80%)
    const memNode = { type: 'memory', id: 'mem:1', heat: 0.3, content: 'test' };
    showDetail(memNode);
    // memory branch sets fill to heat * 100% = 30%
    expect(document.getElementById('det-heat-fill').style.width).toBe('30%');
  });
});

// ── SSE ingestion helpers ─────────────────────────────────────────────────────

describe('ingestSseNode — memory_added', () => {
  it('sets type to "memory" regardless of payload', () => {
    const allNodes = [];
    const node = { id: 'mem:100', content: 'hello', heat: 0.5 };
    ingestSseNode({ event: 'memory_added', node }, allNodes);
    expect(allNodes[0].type).toBe('memory');
  });

  it('deduplicates by id (does not add duplicate)', () => {
    const existing = { id: 'mem:100', type: 'memory', content: 'old' };
    const allNodes = [existing];
    ingestSseNode({ event: 'memory_added', node: { id: 'mem:100', content: 'new' } }, allNodes);
    expect(allNodes).toHaveLength(1);
  });

  it('adds node when id is new', () => {
    const allNodes = [];
    ingestSseNode({ event: 'memory_added', node: { id: 'mem:101' } }, allNodes);
    expect(allNodes).toHaveLength(1);
    expect(allNodes[0].id).toBe('mem:101');
  });

  it('sets node.label from content slice', () => {
    const allNodes = [];
    const longContent = 'a'.repeat(80);
    ingestSseNode({ event: 'memory_added', node: { id: 'mem:102', content: longContent } }, allNodes);
    expect(allNodes[0].label).toBe('a'.repeat(60));
  });
});

describe('ingestSseNode — wiki_added', () => {
  it('sets type to "wiki" regardless of payload', () => {
    const allNodes = [];
    ingestSseNode({ event: 'wiki_added', node: { id: 'wiki:1', slug: 'test', title: 'Test' } }, allNodes);
    expect(allNodes[0].type).toBe('wiki');
  });

  it('upserts: adds node when id is new', () => {
    const allNodes = [];
    ingestSseNode({ event: 'wiki_added', node: { id: 'wiki:1', slug: 'test', title: 'Test' } }, allNodes);
    expect(allNodes).toHaveLength(1);
  });

  it('upserts: updates existing node by id', () => {
    const existing = { id: 'wiki:1', type: 'wiki', title: 'Old Title' };
    const allNodes = [existing];
    ingestSseNode({ event: 'wiki_added', node: { id: 'wiki:1', slug: 'test', title: 'New Title' } }, allNodes);
    expect(allNodes).toHaveLength(1);
    expect(allNodes[0].title).toBe('New Title');
  });
});

describe('ingestSseNode — wiki_updated', () => {
  it('sets type to "wiki"', () => {
    const allNodes = [];
    ingestSseNode({ event: 'wiki_updated', node: { id: 'wiki:2', slug: 'foo', title: 'Foo' } }, allNodes);
    expect(allNodes[0].type).toBe('wiki');
  });

  it('upserts existing node', () => {
    const allNodes = [{ id: 'wiki:2', type: 'wiki', title: 'Old' }];
    ingestSseNode({ event: 'wiki_updated', node: { id: 'wiki:2', slug: 'foo', title: 'Updated' } }, allNodes);
    expect(allNodes).toHaveLength(1);
    expect(allNodes[0].title).toBe('Updated');
  });
});

describe('removeSseNode — wiki_deleted', () => {
  it('removes node by slug match', () => {
    const allNodes = [
      { id: 'wiki:3', type: 'wiki', slug: 'to-delete' },
      { id: 'wiki:4', type: 'wiki', slug: 'keep-this' },
    ];
    removeSseNode({ event: 'wiki_deleted', slug: 'to-delete' }, allNodes);
    expect(allNodes).toHaveLength(1);
    expect(allNodes[0].slug).toBe('keep-this');
  });

  it('is a no-op when slug not found', () => {
    const allNodes = [{ id: 'wiki:3', type: 'wiki', slug: 'keep' }];
    removeSseNode({ event: 'wiki_deleted', slug: 'nonexistent' }, allNodes);
    expect(allNodes).toHaveLength(1);
  });

  it('removes only the matching slug, not all wiki nodes', () => {
    const allNodes = [
      { id: 'wiki:10', type: 'wiki', slug: 'del-me' },
      { id: 'wiki:11', type: 'wiki', slug: 'del-me' }, // edge: duplicate slugs
      { id: 'wiki:12', type: 'wiki', slug: 'keep' },
    ];
    removeSseNode({ event: 'wiki_deleted', slug: 'del-me' }, allNodes);
    // Should remove all matching slugs
    expect(allNodes.every((n) => n.slug !== 'del-me')).toBe(true);
    expect(allNodes.some((n) => n.slug === 'keep')).toBe(true);
  });
});

// ── F1 fidelity: connection count from all edge types ────────────────────────
//
// The panel should count ALL incident edges, not a hardcoded subset of 4.
// Entity nodes wired only by co_occurrence/imports showed "0 connections"
// with the old formula. The new formula groups all edge types dynamically.

describe('showDetail — F1 connection count covers all edge types', () => {
  let showDetail;

  function makePanel(links) {
    buildDom();
    const wikiCatColor = () => ({});
    const heatColorFn = (h) => `hsl(${h * 100},60%,50%)`;
    const allLinksFn = () => links;
    const fetchImpl = () => new Promise(() => {});
    ({ showDetail } = createDetailPanel({
      wikiCatColor,
      heatColorFn,
      allLinksFn,
      fetchImpl,
    }));
  }

  it('entity node with co_occurrence edge shows non-zero count', () => {
    // entity:1 connected via co_occurrence — old formula (4 types) gave 0
    const links = [{ source: 'entity:1', target: 'entity:2', type: 'co_occurrence' }];
    makePanel(links);
    const node = { type: 'entity', id: 'entity:1', label: 'E1', heat: 0.5 };
    showDetail(node);
    const body = document.getElementById('det-body').innerHTML;
    // Should NOT show "0 connections"
    expect(body).not.toContain('0 connections');
    // Should contain "1 co_occurrence"
    expect(body).toContain('1 co_occurrence');
  });

  it('entity node with no edges shows "0 connections"', () => {
    const links = [{ source: 'entity:99', target: 'entity:100', type: 'co_occurrence' }];
    makePanel(links);
    const node = { type: 'entity', id: 'entity:1', label: 'E1', heat: 0.5 };
    showDetail(node);
    const body = document.getElementById('det-body').innerHTML;
    expect(body).toContain('0 connections');
  });

  it('memory node with semantic + temporal edges counts both', () => {
    const links = [
      { source: 'mem:1', target: 'mem:2', type: 'semantic' },
      { source: 'mem:1', target: 'mem:3', type: 'temporal' },
    ];
    makePanel(links);
    const node = { type: 'memory', id: 'mem:1', content: 'hello', heat: 0.7 };
    showDetail(node);
    const body = document.getElementById('det-body').innerHTML;
    expect(body).toContain('1 semantic');
    expect(body).toContain('1 temporal');
  });

  it('entity node with imports + calls edges counts all types', () => {
    const links = [
      { source: 'entity:5', target: 'entity:6', type: 'imports' },
      { source: 'entity:5', target: 'entity:7', type: 'calls' },
      { source: 'entity:5', target: 'entity:8', type: 'caused_by' },
    ];
    makePanel(links);
    const node = { type: 'entity', id: 'entity:5', label: 'E5', heat: 0.3 };
    showDetail(node);
    const body = document.getElementById('det-body').innerHTML;
    expect(body).toContain('1 imports');
    expect(body).toContain('1 calls');
    expect(body).toContain('1 caused_by');
    // Must NOT show "0 connections"
    expect(body).not.toContain('0 connections');
  });

  it('counts target-side edges too (not just source)', () => {
    // entity:2 is the TARGET, not source
    const links = [{ source: 'entity:1', target: 'entity:2', type: 'co_occurrence' }];
    makePanel(links);
    const node = { type: 'entity', id: 'entity:2', label: 'E2', heat: 0.5 };
    showDetail(node);
    const body = document.getElementById('det-body').innerHTML;
    expect(body).toContain('1 co_occurrence');
    expect(body).not.toContain('0 connections');
  });

  it('wiki node connections use wiki edge types (wiki_crossref, memory_wiki)', () => {
    const links = [
      { source: 'wiki:1', target: 'wiki:2', type: 'wiki_crossref' },
      { source: 'mem:10', target: 'wiki:1', type: 'memory_wiki' },
    ];
    makePanel(links);
    const node = { type: 'wiki', id: 'wiki:1', slug: 'test', label: 'Test', category: 'analysis' };
    showDetail(node);
    // Wiki branch uses separate byXref/byMemWiki counts (different code path)
    const body = document.getElementById('det-body').innerHTML;
    expect(body).toContain('1 cross-refs');
    expect(body).toContain('1 source memories');
  });
});
