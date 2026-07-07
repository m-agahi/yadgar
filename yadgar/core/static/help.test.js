/**
 * help.test.js — v5.50.13 — Behavioral tests for the Help tab renderer.
 *
 * Verifies that renderHelp() is a pure renderer over config.legend:
 *   - Produces one row per legend entry (categories, edges, node_types)
 *   - Matches color + description from config
 *   - Renders nothing hardcoded — stub config missing a category → that row absent
 *   - 'help' tab is in VALID_TABS (tabs.js) — routes correctly, doesn't fall back to Home
 *
 * Run: cd viz-tests && npx vitest run
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { renderHelp } from './help.js';
import { VALID_TABS, resolveTab } from './tabs.js';

// ── Stub config fixtures ───────────────────────────────────────────────────────

const STUB_CONFIG = {
  legend: {
    categories: [
      { key: 'architecture', color: '#58a6ff', label: 'Architecture', description: 'Architecture pages.' },
      { key: 'decision',     color: '#ffa657', label: 'Decision',     description: 'Decision pages.' },
      { key: 'fact',         color: '#a5d6ff', label: 'Fact',         description: 'Fact pages.' },
    ],
    edges: [
      { key: 'semantic',    color: '#1f6feb', label: 'Semantic',  description: 'Cosine-similarity link.' },
      { key: 'temporal',    color: '#6e40c9', label: 'Temporal',  description: 'Co-occurrence in time.' },
      { key: 'causal',      color: '#484f58', label: 'Causal',    description: 'Causal relationship.' },
    ],
    node_types: [
      { key: 'memory', shape: 'sphere',     color_rule: 'heat gradient', description: 'Episodic memory.' },
      { key: 'wiki',   shape: 'octahedron', color_rule: 'category color', description: 'Wiki page.' },
    ],
    heat: {
      description: 'Heat rises per recall.',
      gradient: 'blue (cold) → red (hot)',
    },
  },
};

function makeContainer() {
  const div = document.createElement('div');
  document.body.innerHTML = '';
  document.body.appendChild(div);
  return div;
}

// ── renderHelp — categories ────────────────────────────────────────────────────

describe('renderHelp — categories', () => {
  let container;
  beforeEach(() => {
    container = makeContainer();
    renderHelp(STUB_CONFIG, container);
  });

  it('produces one row per legend.categories entry', () => {
    const rows = container.querySelectorAll('.help-row-category');
    expect(rows.length).toBe(STUB_CONFIG.legend.categories.length);
  });

  it('renders the correct label text for each category', () => {
    const rows = container.querySelectorAll('.help-row-category');
    const labels = [...rows].map(r => r.querySelector('.help-label')?.textContent);
    for (const cat of STUB_CONFIG.legend.categories) {
      expect(labels).toContain(cat.label);
    }
  });

  it('renders the correct color swatch for each category', () => {
    // jsdom normalises hex to rgb(...) — compare by checking swatch style is non-empty
    // and that each category has a distinct swatch (not a default fallback for all).
    const rows = container.querySelectorAll('.help-row-category');
    // Each row must have a swatch with a non-empty background
    for (const row of rows) {
      const swatch = row.querySelector('.help-swatch');
      expect(swatch).not.toBeNull();
      expect(swatch.style.background.length).toBeGreaterThan(0);
    }
    // Swatches are not all identical (colors are passed through from config)
    const colors = [...rows].map(r => r.querySelector('.help-swatch')?.style.background);
    const uniqueColors = new Set(colors);
    expect(uniqueColors.size).toBeGreaterThan(1);
  });

  it('renders the description for each category', () => {
    const rows = container.querySelectorAll('.help-row-category');
    const descs = [...rows].map(r => r.querySelector('.help-desc')?.textContent);
    for (const cat of STUB_CONFIG.legend.categories) {
      expect(descs).toContain(cat.description);
    }
  });
});

// ── renderHelp — render-from-source (stub config missing a category) ──────────

describe('renderHelp — render-from-source proof', () => {
  it('renders only the categories present in the stub — absent category produces no row', () => {
    const container = makeContainer();
    // Stub with only 1 category — 'analysis' deliberately absent
    const stubSmall = {
      legend: {
        ...STUB_CONFIG.legend,
        categories: [
          { key: 'architecture', color: '#58a6ff', label: 'Architecture', description: 'Arch pages.' },
        ],
      },
    };
    renderHelp(stubSmall, container);
    const rows = container.querySelectorAll('.help-row-category');
    // Only 1 category row rendered
    expect(rows.length).toBe(1);
    // 'decision' row absent (proves nothing hardcoded)
    const labels = [...rows].map(r => r.querySelector('.help-label')?.textContent);
    expect(labels).not.toContain('Decision');
    expect(labels).not.toContain('decision');
  });

  it('renders 0 edge rows when legend.edges is empty', () => {
    const container = makeContainer();
    renderHelp({ legend: { ...STUB_CONFIG.legend, edges: [] } }, container);
    const rows = container.querySelectorAll('.help-row-edge');
    expect(rows.length).toBe(0);
  });

  it('renders 0 node_type rows when legend.node_types is empty', () => {
    const container = makeContainer();
    renderHelp({ legend: { ...STUB_CONFIG.legend, node_types: [] } }, container);
    const rows = container.querySelectorAll('.help-row-node');
    expect(rows.length).toBe(0);
  });
});

// ── renderHelp — edges ────────────────────────────────────────────────────────

describe('renderHelp — edges', () => {
  let container;
  beforeEach(() => {
    container = makeContainer();
    renderHelp(STUB_CONFIG, container);
  });

  it('produces one row per legend.edges entry', () => {
    const rows = container.querySelectorAll('.help-row-edge');
    expect(rows.length).toBe(STUB_CONFIG.legend.edges.length);
  });

  it('renders edge color swatches from config', () => {
    // jsdom normalises hex to rgb — verify each row has a non-empty swatch background
    const rows = container.querySelectorAll('.help-row-edge');
    for (const row of rows) {
      const swatch = row.querySelector('.help-swatch');
      expect(swatch).not.toBeNull();
      expect(swatch.style.background.length).toBeGreaterThan(0);
    }
    // Swatches are not all identical (colors differ per edge type)
    const colors = [...rows].map(r => r.querySelector('.help-swatch')?.style.background);
    const uniqueColors = new Set(colors);
    expect(uniqueColors.size).toBeGreaterThan(1);
  });

  it('renders edge descriptions from config', () => {
    const rows = container.querySelectorAll('.help-row-edge');
    const descs = [...rows].map(r => r.querySelector('.help-desc')?.textContent);
    for (const edge of STUB_CONFIG.legend.edges) {
      expect(descs).toContain(edge.description);
    }
  });
});

// ── renderHelp — node types ───────────────────────────────────────────────────

describe('renderHelp — node_types', () => {
  let container;
  beforeEach(() => {
    container = makeContainer();
    renderHelp(STUB_CONFIG, container);
  });

  it('produces one row per legend.node_types entry', () => {
    const rows = container.querySelectorAll('.help-row-node');
    expect(rows.length).toBe(STUB_CONFIG.legend.node_types.length);
  });

  it('renders node type labels including shape suffix', () => {
    const rows = container.querySelectorAll('.help-row-node');
    const labels = [...rows].map(r => r.querySelector('.help-label')?.textContent);
    expect(labels.some(l => l.includes('memory'))).toBe(true);
    expect(labels.some(l => l.includes('wiki'))).toBe(true);
    // shape is appended in brackets
    expect(labels.some(l => l.includes('[sphere]'))).toBe(true);
    expect(labels.some(l => l.includes('[octahedron]'))).toBe(true);
  });
});

// ── renderHelp — null/missing config ─────────────────────────────────────────

describe('renderHelp — null config', () => {
  it('renders an error message for null config', () => {
    const container = makeContainer();
    renderHelp(null, container);
    const err = container.querySelector('.help-error');
    expect(err).not.toBeNull();
    expect(err.textContent.length).toBeGreaterThan(0);
  });

  it('renders an error message when config has no legend', () => {
    const container = makeContainer();
    renderHelp({ node: {} }, container);
    const err = container.querySelector('.help-error');
    expect(err).not.toBeNull();
  });
});

// ── renderHelp — heat section ─────────────────────────────────────────────────

describe('renderHelp — heat', () => {
  it('renders the heat gradient row when config.legend.heat is present', () => {
    const container = makeContainer();
    renderHelp(STUB_CONFIG, container);
    const heatRow = container.querySelector('.help-row-heat');
    expect(heatRow).not.toBeNull();
    const label = heatRow.querySelector('.help-label');
    expect(label?.textContent).toContain('blue');
    expect(label?.textContent).toContain('red');
  });

  it('renders the heat description', () => {
    const container = makeContainer();
    renderHelp(STUB_CONFIG, container);
    const heatDesc = container.querySelector('.help-heat-desc');
    expect(heatDesc).not.toBeNull();
    expect(heatDesc.textContent).toBe(STUB_CONFIG.legend.heat.description);
  });
});

// ── tabs.js — 'help' routing ──────────────────────────────────────────────────

describe('tabs.js — help tab routing', () => {
  it("'help' is in VALID_TABS", () => {
    expect(VALID_TABS.has('help')).toBe(true);
  });

  it("resolveTab('#help') returns 'help', not 'home'", () => {
    expect(resolveTab('#help')).toBe('help');
  });

  it("unknown hash still falls back to 'home' (regression guard)", () => {
    expect(resolveTab('#nonexistent')).toBe('home');
  });
});
