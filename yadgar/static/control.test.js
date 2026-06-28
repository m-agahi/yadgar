/**
 * control.test.js — Behavioral tests for control.js (v5.87.0).
 *
 * Tests:
 *   Pure helpers (no DOM):
 *     1. classifyReload — hot_reload → '●hot', restart_required → '⟳restart'
 *     2. filterKnobs — substring filter
 *     3. filterKnobs — group filter 'viz' shows only YADGAR_VIZ_ knobs
 *     4. filterKnobs — group 'all' shows everything
 *     5. filterKnobs — empty filter + group 'physics' narrows correctly
 *     6. parseEditValue int — valid int string
 *     7. parseEditValue int — float string rejected
 *     8. parseEditValue float — valid float
 *     9. parseEditValue float — non-numeric rejected
 *    10. parseEditValue bool — 'true' / 'yes' / '1' / 'on' → true
 *    11. parseEditValue bool — 'false' / 'no' / '0' / 'off' → false
 *    12. parseEditValue bool — garbage rejected
 *    13. parseEditValue string — accepts anything
 *    14. buildRestartConfirmMsg — 'backend' → 'yadgar-backend'
 *    15. buildRestartConfirmMsg — 'yadgar' → 'yadgar'
 *    16. isRestartEnabled — correct match → true
 *    17. isRestartEnabled — mismatch → false
 *    18. isRestartEnabled — backend typed wrong → false
 *   groupKnobsByCategory (B1.2):
 *    19. returns array of {category, label, knobs} groups
 *    20. groups in deterministic CATEGORY_ORDER, absent categories omitted
 *    21. knobs within each group alpha-sorted by name
 *    22. label maps category to human-friendly Title Case
 *    23. unknown categories appended alpha-sorted after CATEGORY_ORDER
 *    24. empty knob array returns empty groups array
 *   DOM / wiring:
 *    25. config row edit fires POST with correct value
 *    26. restart button disabled until correct name typed
 *    27. restart button fires POST /api/control/restart/<segment> with confirm
 *    28. update button greys out on 404 from /api/control/update
 *    29. update button live on 200 from /api/control/update
 *    30. 403 response shows warning banner and disables sections
 *    31. each knob row has a help icon linking to cfgref anchor
 *    32. clicking help icon calls switchTab('config-ref')
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  classifyReload,
  filterKnobs,
  parseEditValue,
  buildRestartConfirmMsg,
  isRestartEnabled,
  groupKnobsByCategory,
  initControlTab,
} from './control.js';

// ---------------------------------------------------------------------------
// Sample fixture data
// ---------------------------------------------------------------------------

// category matches the real GET /api/control/config contract (control.py _enrich_knob):
// section viz_config -> "viz"; section core -> "ops" (SECTION_TO_CATEGORY).
const _SAMPLE_KNOBS = [
  { name: 'YADGAR_VIZ_NODE_SIZE_3D',          kind: 'float',  current: '8.0',  default: '8.0',  source: 'default', reload: 'hot_reload',        category: 'viz' },
  { name: 'YADGAR_VIZ_EDGE_OPACITY',           kind: 'float',  current: '0.9',  default: '0.9',  source: 'default', reload: 'hot_reload',        category: 'viz' },
  { name: 'YADGAR_VIZ_PHYSICS_CHARGE_STRENGTH',kind: 'float',  current: '-18.0',default: '-18.0',source: 'default', reload: 'hot_reload',        category: 'viz' },
  { name: 'YADGAR_EMBEDDING_MODEL',            kind: 'string', current: 'all-MiniLM-L6-v2', default: 'all-MiniLM-L6-v2', source: 'default', reload: 'restart_required', category: 'ops' },
  { name: 'YADGAR_PORT',                       kind: 'int',    current: '8765', default: '8765', source: 'default', reload: 'restart_required', category: 'ops' },
];

// ---------------------------------------------------------------------------
// 1–2 classifyReload
// ---------------------------------------------------------------------------

describe('classifyReload', () => {
  it('hot_reload → ●hot', () => {
    expect(classifyReload('hot_reload')).toBe('●hot');
  });

  it('restart_required → ⟳restart', () => {
    expect(classifyReload('restart_required')).toBe('⟳restart');
  });
});

// ---------------------------------------------------------------------------
// 2–5 filterKnobs
// ---------------------------------------------------------------------------

describe('filterKnobs', () => {
  it('substring filter matches case-insensitively', () => {
    const result = filterKnobs(_SAMPLE_KNOBS, 'node_size', 'all');
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe('YADGAR_VIZ_NODE_SIZE_3D');
  });

  it('group viz shows only YADGAR_VIZ_ knobs', () => {
    const result = filterKnobs(_SAMPLE_KNOBS, '', 'viz');
    expect(result.every(k => k.name.startsWith('YADGAR_VIZ_'))).toBe(true);
    expect(result.length).toBeGreaterThan(0);
  });

  it('group all shows everything', () => {
    const result = filterKnobs(_SAMPLE_KNOBS, '', 'all');
    expect(result).toHaveLength(_SAMPLE_KNOBS.length);
  });

  it('group physics narrows to YADGAR_VIZ_PHYSICS_ knobs', () => {
    const result = filterKnobs(_SAMPLE_KNOBS, '', 'physics');
    expect(result.every(k => k.name.startsWith('YADGAR_VIZ_PHYSICS_'))).toBe(true);
    expect(result.length).toBeGreaterThan(0);
  });

  it('empty filter + group embedding shows only YADGAR_EMBEDDING_ knobs', () => {
    const result = filterKnobs(_SAMPLE_KNOBS, '', 'embedding');
    expect(result.every(k => k.name.startsWith('YADGAR_EMBEDDING_'))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 6–13 parseEditValue
// ---------------------------------------------------------------------------

describe('parseEditValue', () => {
  it('int: valid integer string → {value: number, error: null}', () => {
    const r = parseEditValue('42', 'int');
    expect(r.error).toBeNull();
    expect(r.value).toBe(42);
  });

  it('int: float string rejected', () => {
    const r = parseEditValue('3.14', 'int');
    expect(r.error).not.toBeNull();
    expect(r.value).toBeNull();
  });

  it('float: valid float string', () => {
    const r = parseEditValue('-18.5', 'float');
    expect(r.error).toBeNull();
    expect(r.value).toBeCloseTo(-18.5);
  });

  it('float: non-numeric string rejected', () => {
    const r = parseEditValue('not-a-number', 'float');
    expect(r.error).not.toBeNull();
    expect(r.value).toBeNull();
  });

  it('bool: true/yes/1/on → true', () => {
    for (const raw of ['true', 'yes', '1', 'on', 'True', 'YES']) {
      const r = parseEditValue(raw, 'bool');
      expect(r.error).toBeNull();
      expect(r.value).toBe(true);
    }
  });

  it('bool: false/no/0/off → false', () => {
    for (const raw of ['false', 'no', '0', 'off', 'False']) {
      const r = parseEditValue(raw, 'bool');
      expect(r.error).toBeNull();
      expect(r.value).toBe(false);
    }
  });

  it('bool: garbage rejected', () => {
    const r = parseEditValue('maybe', 'bool');
    expect(r.error).not.toBeNull();
    expect(r.value).toBeNull();
  });

  it('string: accepts anything', () => {
    const r = parseEditValue('anything goes <here>', 'string');
    expect(r.error).toBeNull();
    expect(r.value).toBe('anything goes <here>');
  });
});

// ---------------------------------------------------------------------------
// 14–18 buildRestartConfirmMsg / isRestartEnabled
// ---------------------------------------------------------------------------

describe('buildRestartConfirmMsg', () => {
  it('backend → yadgar-backend', () => {
    expect(buildRestartConfirmMsg('backend')).toBe('yadgar-backend');
  });

  it('yadgar → yadgar', () => {
    expect(buildRestartConfirmMsg('yadgar')).toBe('yadgar');
  });
});

describe('isRestartEnabled', () => {
  it('correct yadgar match → true', () => {
    expect(isRestartEnabled('yadgar', 'yadgar')).toBe(true);
  });

  it('mismatch → false', () => {
    expect(isRestartEnabled('wrong', 'yadgar')).toBe(false);
  });

  it('backend: "yadgar-backend" typed → true', () => {
    expect(isRestartEnabled('yadgar-backend', 'backend')).toBe(true);
  });

  it('backend: "backend" typed (wrong) → false', () => {
    expect(isRestartEnabled('backend', 'backend')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// groupKnobsByCategory (B1.2)
// ---------------------------------------------------------------------------

// Mixed-category fixture with out-of-order names
const _CAT_KNOBS = [
  { name: 'YADGAR_VIZ_EDGE_OPACITY',    kind: 'float',  category: 'viz',          reload: 'hot_reload'      },
  { name: 'YADGAR_VIZ_NODE_SIZE',       kind: 'float',  category: 'viz',          reload: 'hot_reload'      },
  { name: 'YADGAR_EMBED_MODEL',         kind: 'string', category: 'retrieval',    reload: 'restart_required'},
  { name: 'YADGAR_PORT',                kind: 'int',    category: 'ops',          reload: 'restart_required'},
  { name: 'YADGAR_LOG_LEVEL',           kind: 'string', category: 'ops',          reload: 'hot_reload'      },
  { name: 'YADGAR_CONSOLIDATE_ENABLED', kind: 'bool',   category: 'write-path',   reload: 'hot_reload'      },
  { name: 'YADGAR_UNKNOWN_FUTURE',      kind: 'string', category: 'new-category', reload: 'hot_reload'      },
];

describe('groupKnobsByCategory', () => {
  it('returns array of {category, label, knobs} groups', () => {
    const groups = groupKnobsByCategory(_CAT_KNOBS);
    expect(Array.isArray(groups)).toBe(true);
    for (const g of groups) {
      expect(g).toHaveProperty('category');
      expect(g).toHaveProperty('label');
      expect(g).toHaveProperty('knobs');
      expect(Array.isArray(g.knobs)).toBe(true);
    }
  });

  it('groups in deterministic CATEGORY_ORDER; absent categories omitted', () => {
    const groups = groupKnobsByCategory(_CAT_KNOBS);
    const cats = groups.map(g => g.category);
    // retrieval comes before write-path comes before viz comes before ops
    expect(cats.indexOf('retrieval')).toBeLessThan(cats.indexOf('write-path'));
    expect(cats.indexOf('write-path')).toBeLessThan(cats.indexOf('viz'));
    expect(cats.indexOf('viz')).toBeLessThan(cats.indexOf('ops'));
    // enrichment has no knobs — must be absent
    expect(cats).not.toContain('enrichment');
  });

  it('knobs within each group are alpha-sorted by name', () => {
    const groups = groupKnobsByCategory(_CAT_KNOBS);
    const vizGroup = groups.find(g => g.category === 'viz');
    expect(vizGroup).toBeDefined();
    const names = vizGroup.knobs.map(k => k.name);
    expect(names).toEqual([...names].sort());
    // ops: LOG before PORT
    const opsGroup = groups.find(g => g.category === 'ops');
    const opsNames = opsGroup.knobs.map(k => k.name);
    expect(opsNames).toEqual([...opsNames].sort());
  });

  it('label maps category to human-friendly Title Case', () => {
    const groups = groupKnobsByCategory(_CAT_KNOBS);
    const bycat = Object.fromEntries(groups.map(g => [g.category, g.label]));
    expect(bycat['viz']).toBe('Viz');
    expect(bycat['ops']).toBe('Ops');
    expect(bycat['write-path']).toBe('Write Path');
    expect(bycat['retrieval']).toBe('Retrieval');
  });

  it('unknown categories are appended alpha-sorted after CATEGORY_ORDER entries', () => {
    const groups = groupKnobsByCategory(_CAT_KNOBS);
    const cats = groups.map(g => g.category);
    // new-category is unknown → must appear after all known categories
    const knownIdx = Math.max(
      cats.indexOf('retrieval'), cats.indexOf('ops'),
      cats.indexOf('write-path'), cats.indexOf('viz'),
    );
    const unknownIdx = cats.indexOf('new-category');
    expect(unknownIdx).toBeGreaterThan(knownIdx);
  });

  it('empty knob array returns empty groups array', () => {
    const groups = groupKnobsByCategory([]);
    expect(groups).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// DOM / wiring tests
// ---------------------------------------------------------------------------

function _makeContainer() {
  const div = document.createElement('div');
  div.id = 'tab-control';
  document.body.appendChild(div);
  return div;
}

function _cleanup(div) {
  if (div && div.parentNode) div.parentNode.removeChild(div);
}

describe('initControlTab DOM wiring', () => {
  let root;
  let originalFetch;

  beforeEach(() => {
    root = _makeContainer();
    originalFetch = globalThis.fetch;
  });

  afterEach(() => {
    _cleanup(root);
    globalThis.fetch = originalFetch;
  });

  // 19. Config row edit fires POST with correct value
  it('config row edit fires POST /api/control/config with typed value', async () => {
    const posts = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && (!opts?.method || opts.method !== 'POST')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [
          { name: 'YADGAR_VIZ_NODE_SIZE_3D', kind: 'float', current: '8.0', default: '8.0', source: 'default', reload: 'hot_reload' },
        ] }) };
      }
      if (typeof url === 'string' && url.includes('/api/control/config') && opts?.method === 'POST') {
        posts.push(JSON.parse(opts.body));
        return { ok: true, status: 200, json: async () => ({ name: 'YADGAR_VIZ_NODE_SIZE_3D', value: '12.5', reload: 'hot_reload' }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);

    // Click the edit button on the first row
    const editBtn = root.querySelector('.ctrl-btn--sm[aria-label*="YADGAR_VIZ_NODE_SIZE_3D"]');
    expect(editBtn).not.toBeNull();
    editBtn.click();

    // Should have an input now
    const input = root.querySelector('.ctrl-edit-input');
    expect(input).not.toBeNull();

    // Change value and click save
    input.value = '12.5';
    input.dispatchEvent(new Event('input'));
    const saveBtn = root.querySelector('.ctrl-btn--save');
    expect(saveBtn).not.toBeNull();
    await saveBtn.click();

    // Wait for async POST
    await new Promise(r => setTimeout(r, 50));
    expect(posts.length).toBeGreaterThan(0);
    expect(posts[0].name).toBe('YADGAR_VIZ_NODE_SIZE_3D');
    expect(posts[0].value).toBeCloseTo(12.5);
  });

  // 20. Restart button disabled until correct name typed
  it('restart button disabled until correct name typed', async () => {
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && (!opts?.method || opts.method === 'GET')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [] }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);

    const confirmInput = root.querySelector('.ctrl-restart-confirm-input[data-segment="yadgar"]');
    const restartBtn   = root.querySelector('.ctrl-btn--restart');
    expect(confirmInput).not.toBeNull();
    expect(restartBtn).not.toBeNull();

    // Initially disabled
    expect(restartBtn.disabled).toBe(true);

    // Wrong name → still disabled
    confirmInput.value = 'wrong';
    confirmInput.dispatchEvent(new Event('input'));
    expect(restartBtn.disabled).toBe(true);

    // Correct name → enabled
    confirmInput.value = 'yadgar';
    confirmInput.dispatchEvent(new Event('input'));
    expect(restartBtn.disabled).toBe(false);
  });

  // 21. Restart button fires POST /api/control/restart/<segment> with confirm
  it('restart button fires POST /api/control/restart/yadgar with confirm body', async () => {
    const restartPosts = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [] }) };
      }
      if (typeof url === 'string' && url.includes('/api/control/restart/yadgar')) {
        restartPosts.push(JSON.parse(opts.body));
        return { ok: true, status: 202, json: async () => ({ status: 'sentinel_written', service: 'yadgar' }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);

    const confirmInput = root.querySelector('.ctrl-restart-confirm-input[data-segment="yadgar"]');
    confirmInput.value = 'yadgar';
    confirmInput.dispatchEvent(new Event('input'));

    const restartBtn = root.querySelector('.ctrl-btn--restart');
    restartBtn.click();
    await new Promise(r => setTimeout(r, 50));

    expect(restartPosts.length).toBe(1);
    expect(restartPosts[0].confirm).toBe('yadgar');
  });

  // 22. Update button greys out on 404 from /api/control/update
  it('update button greys out on 404 from /api/control/update', async () => {
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [] }) };
      }
      if (typeof url === 'string' && url.includes('/api/control/update')) {
        return { ok: false, status: 404, json: async () => ({}) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);
    // Wait for the update-check fetch to resolve
    await new Promise(r => setTimeout(r, 50));

    const updateBtn = root.querySelector('.ctrl-btn--update');
    expect(updateBtn).not.toBeNull();
    expect(updateBtn.disabled).toBe(true);
    expect(updateBtn.classList.contains('ctrl-btn--disabled')).toBe(true);
  });

  // 23. Update button live on 200 from /api/control/update
  it('update button is live on 200 from /api/control/update', async () => {
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [] }) };
      }
      if (typeof url === 'string' && url.includes('/api/control/update')) {
        return {
          ok: true, status: 200,
          json: async () => ({
            current_version: '5.50.2', available_version: '5.50.2',
            update_available: false, upgrade_command: 'pipx upgrade yadgar',
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);
    await new Promise(r => setTimeout(r, 50));

    const updateBtn = root.querySelector('.ctrl-btn--update');
    expect(updateBtn).not.toBeNull();
    expect(updateBtn.disabled).toBe(false);
    expect(updateBtn.classList.contains('ctrl-btn--disabled')).toBe(false);
  });

  // 24. 403 shows banner and disables sections
  it('403 response from /api/control/config shows warning banner', async () => {
    globalThis.fetch = vi.fn(async (url) => {
      return { ok: false, status: 403, json: async () => ({ error: 'debug APIs disabled' }) };
    });

    await initControlTab(root);

    const banner = root.querySelector('.ctrl-banner');
    expect(banner).not.toBeNull();
    expect(banner.style.display).not.toBe('none');
    expect(banner.textContent).toContain('YADGAR_DEBUG_APIS_ENABLED');

    // Actions section should be disabled (pointer-events: none)
    const actionsRow = root.querySelector('.ctrl-actions-row');
    expect(actionsRow.style.pointerEvents).toBe('none');
  });

  // 25. Each knob row has a help icon (ctrl-knob-help) with href targeting cfgref anchor
  it('each knob row has a help icon linking to cfgref-<name> anchor', async () => {
    const knobFixture = [
      { name: 'YADGAR_VIZ_NODE_SIZE', kind: 'float', current: '8.0', default: '8.0',
        source: 'default', reload: 'hot_reload', category: 'viz', description: 'Node size' },
      { name: 'YADGAR_PORT', kind: 'int', current: '8765', default: '8765',
        source: 'default', reload: 'restart_required', category: 'ops', description: 'Port' },
    ];
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && !(opts?.method === 'POST')) {
        return { ok: true, status: 200, json: async () => ({ knobs: knobFixture }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);

    for (const knob of knobFixture) {
      const row = root.querySelector(`tr[data-name="${knob.name}"]`);
      expect(row, `row for ${knob.name}`).not.toBeNull();
      const helpEl = row.querySelector('.ctrl-knob-help');
      expect(helpEl, `help icon for ${knob.name}`).not.toBeNull();
      const expectedAnchor = `cfgref-${knob.name}`;
      // href or dataset must reference the anchor
      const href = helpEl.getAttribute('href') || helpEl.dataset.anchor || '';
      expect(href).toContain(expectedAnchor);
    }
  });

  // 26. Clicking help icon calls window.YadgarTabs.switchTab('config-ref')
  it('clicking help icon calls switchTab("config-ref") and scrolls to anchor', async () => {
    const switchTabMock = vi.fn();
    window.YadgarTabs = { switchTab: switchTabMock };

    const knobFixture = [
      { name: 'YADGAR_VIZ_NODE_SIZE', kind: 'float', current: '8.0', default: '8.0',
        source: 'default', reload: 'hot_reload', category: 'viz', description: 'Node size' },
    ];
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && !(opts?.method === 'POST')) {
        return { ok: true, status: 200, json: async () => ({ knobs: knobFixture }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    // Set up a target element that scrollIntoView can be called on
    const anchorEl = document.createElement('div');
    anchorEl.id = 'cfgref-YADGAR_VIZ_NODE_SIZE';
    anchorEl.scrollIntoView = vi.fn();
    document.body.appendChild(anchorEl);

    await initControlTab(root);

    const helpEl = root.querySelector('.ctrl-knob-help');
    expect(helpEl).not.toBeNull();
    helpEl.click();

    expect(switchTabMock).toHaveBeenCalledWith('config-ref');

    // Cleanup
    document.body.removeChild(anchorEl);
    delete window.YadgarTabs;
  });
});
