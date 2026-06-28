/**
 * config-ref.test.js — Behavioral tests for config-ref.js (v5.87.0 car B2b).
 *
 * Tests:
 *   Pure helpers (no DOM):
 *     1. buildConfigRefModel — returns ordered groups matching groupKnobsByCategory
 *     2. buildConfigRefModel — each entry has name, description, kind, default, category, enum_choices
 *     3. buildConfigRefModel — groups alpha-sorted within group
 *     4. buildConfigRefModel — enum_choices preserved when non-empty
 *     5. buildConfigRefModel — empty knobs returns empty array
 *   DOM / initConfigRefTab:
 *     6. each knob has an element with id="cfgref-<name>"
 *     7. each knob entry contains name, description, type (kind), default value
 *     8. enum_choices list rendered when non-empty
 *     9. category group headers present with correct label text
 *    10. no XSS — knob data rendered as textContent, not innerHTML
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { buildConfigRefModel, initConfigRefTab } from './config-ref.js';

// ---------------------------------------------------------------------------
// Fixture
// ---------------------------------------------------------------------------

const _KNOBS = [
  {
    name: 'YADGAR_VIZ_NODE_SIZE',
    kind: 'float',
    current: '8.0',
    default: '8.0',
    source: 'default',
    reload: 'hot_reload',
    category: 'viz',
    description: 'Size of graph nodes in 3D view.',
    enum_choices: [],
  },
  {
    name: 'YADGAR_VIZ_EDGE_OPACITY',
    kind: 'float',
    current: '0.9',
    default: '0.9',
    source: 'default',
    reload: 'hot_reload',
    category: 'viz',
    description: 'Opacity for edges in graph view.',
    enum_choices: [],
  },
  {
    name: 'YADGAR_PORT',
    kind: 'int',
    current: '8765',
    default: '8765',
    source: 'default',
    reload: 'restart_required',
    category: 'ops',
    description: 'HTTP port for the yadgar server.',
    enum_choices: [],
  },
  {
    name: 'YADGAR_LOG_LEVEL',
    kind: 'string',
    current: 'INFO',
    default: 'INFO',
    source: 'default',
    reload: 'hot_reload',
    category: 'ops',
    description: 'Logging level for the yadgar server.',
    enum_choices: ['DEBUG', 'INFO', 'WARNING', 'ERROR'],
  },
];

// ---------------------------------------------------------------------------
// 1–5: buildConfigRefModel (pure)
// ---------------------------------------------------------------------------

describe('buildConfigRefModel', () => {
  it('returns ordered groups matching groupKnobsByCategory order', () => {
    const model = buildConfigRefModel(_KNOBS);
    expect(Array.isArray(model)).toBe(true);
    // viz comes before ops in CATEGORY_ORDER
    const cats = model.map(g => g.category);
    expect(cats.indexOf('viz')).toBeLessThan(cats.indexOf('ops'));
  });

  it('each entry has name, description, kind, default, category, enum_choices', () => {
    const model = buildConfigRefModel(_KNOBS);
    for (const group of model) {
      for (const entry of group.knobs) {
        expect(entry).toHaveProperty('name');
        expect(entry).toHaveProperty('description');
        expect(entry).toHaveProperty('kind');
        expect(entry).toHaveProperty('default');
        expect(entry).toHaveProperty('category');
        expect(entry).toHaveProperty('enum_choices');
      }
    }
  });

  it('knobs within groups are alpha-sorted by name', () => {
    const model = buildConfigRefModel(_KNOBS);
    for (const group of model) {
      const names = group.knobs.map(e => e.name);
      expect(names).toEqual([...names].sort());
    }
  });

  it('enum_choices preserved when non-empty', () => {
    const model = buildConfigRefModel(_KNOBS);
    const opsGroup = model.find(g => g.category === 'ops');
    const logEntry = opsGroup.knobs.find(e => e.name === 'YADGAR_LOG_LEVEL');
    expect(logEntry.enum_choices).toEqual(['DEBUG', 'INFO', 'WARNING', 'ERROR']);
  });

  it('empty knobs array returns empty model', () => {
    const model = buildConfigRefModel([]);
    expect(model).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 6–10: initConfigRefTab DOM (jsdom)
// ---------------------------------------------------------------------------

function _makeRoot() {
  const div = document.createElement('div');
  document.body.appendChild(div);
  return div;
}

function _cleanup(el) {
  if (el && el.parentNode) el.parentNode.removeChild(el);
}

describe('initConfigRefTab DOM', () => {
  let root;

  beforeEach(() => {
    root = _makeRoot();
  });

  afterEach(() => {
    _cleanup(root);
  });

  it('each knob has an element with id="cfgref-<name>"', () => {
    initConfigRefTab(root, _KNOBS);
    for (const knob of _KNOBS) {
      const el = root.querySelector(`#cfgref-${knob.name}`);
      expect(el, `#cfgref-${knob.name}`).not.toBeNull();
    }
  });

  it('each knob entry contains name, description, kind, and default', () => {
    initConfigRefTab(root, _KNOBS);
    for (const knob of _KNOBS) {
      const el = root.querySelector(`#cfgref-${knob.name}`);
      const text = el.textContent;
      expect(text).toContain(knob.name);
      expect(text).toContain(knob.description);
      expect(text).toContain(knob.kind);
      expect(text).toContain(knob.default);
    }
  });

  it('enum_choices list is rendered for knobs with non-empty choices', () => {
    initConfigRefTab(root, _KNOBS);
    const logEl = root.querySelector('#cfgref-YADGAR_LOG_LEVEL');
    const text = logEl.textContent;
    for (const choice of ['DEBUG', 'INFO', 'WARNING', 'ERROR']) {
      expect(text).toContain(choice);
    }
  });

  it('category group headers are present with correct label text', () => {
    initConfigRefTab(root, _KNOBS);
    // Should have group header elements for viz and ops
    const text = root.textContent;
    expect(text).toContain('Viz');
    expect(text).toContain('Ops');
  });

  it('knob data rendered as textContent (no XSS via innerHTML)', () => {
    const xssKnob = {
      name: 'YADGAR_XSS_TEST',
      kind: 'string',
      current: '<script>alert(1)</script>',
      default: '<img onerror=alert(1) src=x>',
      source: 'default',
      reload: 'hot_reload',
      category: 'ops',
      description: '<b>bold</b> description',
      enum_choices: [],
    };
    initConfigRefTab(root, [xssKnob]);
    const el = root.querySelector('#cfgref-YADGAR_XSS_TEST');
    // The raw angle-bracket strings should appear as text, not be parsed as HTML
    expect(el.querySelector('script')).toBeNull();
    expect(el.querySelector('img')).toBeNull();
    expect(el.querySelector('b')).toBeNull();
    // But the text content IS there (escaped)
    expect(el.textContent).toContain('<b>bold</b> description');
  });
});
