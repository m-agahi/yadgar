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
 *    25. editing a control + Apply fires POST with correct value (v5.89 redesign)
 *    26. restart button disabled until correct name typed
 *    27. restart button fires POST /api/control/restart/<segment> with confirm
 *    28. update button greys out on 404 from /api/control/update
 *    29. update button live on 200 from /api/control/update
 *    30. 403 response shows warning banner and disables sections
 *    31. renders a 3-way source badge per row (default/yaml/env) (v5.89 redesign)
 *    32. cross-category search filters across all categories + <mark> (v5.89)
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  classifyReload,
  filterKnobs,
  parseEditValue,
  buildRestartConfirmMsg,
  isRestartEnabled,
  groupKnobsByCategory,
  displayKnobValue,
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
// displayKnobValue (ADR-0013 bool-display fix)
// ---------------------------------------------------------------------------

describe('displayKnobValue', () => {
  it('bool: normalizes every truthy form to lowercase "true"', () => {
    // Server GET sends lowercase strings; POST historically sent capitalized
    // Python str(True); JS booleans render as "true". All must collapse to "true".
    for (const v of ['true', 'True', '1', 'yes', 'on', true]) {
      expect(displayKnobValue(v, 'bool')).toBe('true');
    }
  });

  it('bool: normalizes every falsy form to lowercase "false"', () => {
    for (const v of ['false', 'False', '0', 'no', 'off', false]) {
      expect(displayKnobValue(v, 'bool')).toBe('false');
    }
  });

  it('non-bool kinds pass through unchanged', () => {
    expect(displayKnobValue('8.0', 'float')).toBe('8.0');
    expect(displayKnobValue('all-MiniLM-L6-v2', 'string')).toBe('all-MiniLM-L6-v2');
    expect(displayKnobValue('12', 'int')).toBe('12');
  });

  it('bool: unrecognized value passes through (no silent corruption)', () => {
    expect(displayKnobValue('maybe', 'bool')).toBe('maybe');
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
  it('editing a control + Apply fires POST /api/control/config with typed value', async () => {
    // v5.89 redesign: edit the typed control (number input), the sticky pending
    // bar appears, and Apply POSTs the changed knob with its coerced value.
    const posts = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && (!opts?.method || opts.method !== 'POST')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [
          { name: 'YADGAR_VIZ_NODE_SIZE_3D', kind: 'float', current: '8.0', default: '8.0', source: 'default', reload: 'hot_reload', category: 'viz', section: 'viz_config', description: 'Node size', locked: false, enum_choices: [] },
        ] }) };
      }
      if (typeof url === 'string' && url.includes('/api/control/config') && opts?.method === 'POST') {
        posts.push(JSON.parse(opts.body));
        return { ok: true, status: 200, json: async () => ({ name: 'YADGAR_VIZ_NODE_SIZE_3D', value: '12.5', reload: 'hot_reload' }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);

    // The row + its number control are present in the category pane.
    const row = root.querySelector('.setting-row[data-name="YADGAR_VIZ_NODE_SIZE_3D"]');
    expect(row).not.toBeNull();
    const numInput = row.querySelector('.num-input');
    expect(numInput).not.toBeNull();

    // Edit the value → pending bar becomes visible.
    numInput.value = '12.5';
    numInput.dispatchEvent(new Event('input'));
    const pendingBar = root.querySelector('.cfg-pending-bar');
    expect(pendingBar.style.display).not.toBe('none');

    // Apply → POST fires with the coerced value.
    const applyBtn = root.querySelector('.cfg-btn-apply');
    expect(applyBtn).not.toBeNull();
    await applyBtn.click();
    await new Promise(r => setTimeout(r, 50));

    expect(posts.length).toBeGreaterThan(0);
    expect(posts[0].name).toBe('YADGAR_VIZ_NODE_SIZE_3D');
    expect(posts[0].value).toBeCloseTo(12.5);
  });

  // Car D + Surface 2: destructive row is armed by CLICKING the arm button (the
  // typed-name confirm was replaced by a button + countdown). The POST still
  // carries armed:true (behavior unchanged, only presentation).
  it('destructive row: control disabled until arm button clicked, then Apply POSTs {armed:true}', async () => {
    const posts = [];
    const KNOB = 'YADGAR_MEMORY_ARCHIVE_RETENTION_DAYS';
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && (!opts?.method || opts.method === 'GET')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [
          { name: KNOB, kind: 'int', current: '90', default: '90', source: 'default', reload: 'restart_required', category: 'write-path', section: 'memory_archive_retention', description: 'archive retention', locked: false, enum_choices: [], destructive: true },
        ] }) };
      }
      if (typeof url === 'string' && url.includes('/api/control/config') && opts?.method === 'POST') {
        posts.push(JSON.parse(opts.body));
        return { ok: true, status: 200, json: async () => ({ name: KNOB, value: '120', reload: 'restart_required' }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);

    const row = root.querySelector(`.setting-row[data-name="${KNOB}"]`);
    expect(row).not.toBeNull();
    expect(row.classList.contains('destructive')).toBe(true);
    // The ⚠ marker is present.
    expect(row.querySelector('.destructive-marker')).not.toBeNull();

    // The numeric control starts DISABLED (unarmed).
    const numInput = row.querySelector('.num-input');
    expect(numInput).not.toBeNull();
    expect(numInput.disabled).toBe(true);

    // Click the arm button → control becomes enabled INLINE (no rerender
    // teardown) and the button flips to its armed state with a countdown label.
    const armBtn = row.querySelector('.cfg-arm-btn');
    expect(armBtn).not.toBeNull();
    armBtn.click();
    expect(numInput.disabled).toBe(false);
    expect(armBtn.classList.contains('armed')).toBe(true);
    const countdown = row.querySelector('.cfg-arm-countdown');
    expect(countdown).not.toBeNull();
    expect(countdown.textContent).toMatch(/expires in \d+s/);
    // Same arm-button node still in the DOM (focus not destroyed by a rerender).
    expect(root.querySelector(`.setting-row[data-name="${KNOB}"] .cfg-arm-btn`)).toBe(armBtn);

    // Edit + Apply → POST carries armed:true.
    numInput.value = '120';
    numInput.dispatchEvent(new Event('input'));
    const applyBtn = root.querySelector('.cfg-btn-apply');
    await applyBtn.click();
    await new Promise(r => setTimeout(r, 50));

    expect(posts.length).toBeGreaterThan(0);
    expect(posts[0].name).toBe(KNOB);
    expect(posts[0].armed).toBe(true);
  });

  // Surface 2: clicking the arm button a second time disarms (re-disables the
  // control) — the countdown/expiry is presentation only.
  it('destructive row: clicking arm button again disarms and re-disables the control', async () => {
    const KNOB = 'YADGAR_MEMORY_ARCHIVE_RETENTION_DAYS';
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && (!opts?.method || opts.method === 'GET')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [
          { name: KNOB, kind: 'int', current: '90', default: '90', source: 'default', reload: 'restart_required', category: 'write-path', section: 'memory_archive_retention', description: 'archive retention', locked: false, enum_choices: [], destructive: true },
        ] }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);
    const row = root.querySelector(`.setting-row[data-name="${KNOB}"]`);
    const numInput = row.querySelector('.num-input');
    const armBtn = row.querySelector('.cfg-arm-btn');

    armBtn.click();               // arm
    expect(numInput.disabled).toBe(false);
    armBtn.click();               // disarm
    expect(numInput.disabled).toBe(true);
    expect(armBtn.classList.contains('armed')).toBe(false);
    expect(row.querySelector('.cfg-arm-countdown').textContent).toBe('');
  });

  // Surface 2: an edited knob renders a diff card in the commit tray.
  it('editing a knob renders a diff card (name + old→new) in the commit tray', async () => {
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && (!opts?.method || opts.method !== 'POST')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [
          { name: 'YADGAR_VIZ_NODE_SIZE_3D', kind: 'float', current: '8.0', default: '8.0', source: 'default', reload: 'hot_reload', category: 'viz', section: 'viz_config', description: 'Node size', locked: false, enum_choices: [] },
        ] }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);
    const trayBody = root.querySelector('.cfg-tray-body');
    expect(trayBody).not.toBeNull();
    // Empty state before any edit.
    expect(trayBody.querySelector('.cfg-tray-empty')).not.toBeNull();

    const numInput = root.querySelector('.setting-row[data-name="YADGAR_VIZ_NODE_SIZE_3D"] .num-input');
    numInput.value = '12.5';
    numInput.dispatchEvent(new Event('input'));

    const chg = trayBody.querySelector('.cfg-chg');
    expect(chg).not.toBeNull();
    expect(chg.querySelector('.cfg-chg-name').textContent).toBe('YADGAR_VIZ_NODE_SIZE_3D');
    expect(chg.querySelector('.cfg-chg-old').textContent).toBe('8.0');
    expect(chg.querySelector('.cfg-chg-new').textContent).toBe('12.5');
  });

  // ADR-0013: vacuum button requires explicit confirm before POSTing
  it('vacuum button POSTs {confirm:"vacuum"} only after confirm', async () => {
    const posts = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && (!opts?.method || opts.method === 'GET')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [] }) };
      }
      if (typeof url === 'string' && url.includes('/api/control/action/vacuum')) {
        posts.push(opts?.body);
        return { ok: true, status: 200, json: async () => ({ action: 'vacuum', result: {} }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });
    const origConfirm = window.confirm;
    window.confirm = vi.fn(() => true);

    await initControlTab(root);
    const vacuumBtn = root.querySelector('.ctrl-btn[data-action="vacuum"]');
    expect(vacuumBtn).not.toBeNull();
    await vacuumBtn.click();
    await new Promise(r => setTimeout(r, 50));

    expect(window.confirm).toHaveBeenCalled();
    expect(posts.length).toBe(1);
    expect(JSON.parse(posts[0]).confirm).toBe('vacuum');
    window.confirm = origConfirm;
  });

  it('vacuum button does NOT POST when confirm is cancelled', async () => {
    const posts = [];
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && (!opts?.method || opts.method === 'GET')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [] }) };
      }
      if (typeof url === 'string' && url.includes('/api/control/action/vacuum')) {
        posts.push(opts?.body);
        return { ok: true, status: 200, json: async () => ({}) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });
    const origConfirm = window.confirm;
    window.confirm = vi.fn(() => false);

    await initControlTab(root);
    const vacuumBtn = root.querySelector('.ctrl-btn[data-action="vacuum"]');
    await vacuumBtn.click();
    await new Promise(r => setTimeout(r, 50));

    expect(window.confirm).toHaveBeenCalled();
    expect(posts.length).toBe(0);
    window.confirm = origConfirm;
  });

  // Car 10: HTTP 200 does not mean the vacuum actually started — the button
  // must surface result.started/skipped_reason, not a blind "✓".
  it('vacuum button shows the skip reason, not ✓, when the server reports started:false', async () => {
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && (!opts?.method || opts.method === 'GET')) {
        return { ok: true, status: 200, json: async () => ({ knobs: [] }) };
      }
      if (typeof url === 'string' && url.includes('/api/control/action/vacuum')) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            action: 'vacuum',
            result: { started: false, skipped_reason: 'db_below_threshold', trigger_path: null },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });
    const origConfirm = window.confirm;
    window.confirm = vi.fn(() => true);

    await initControlTab(root);
    const vacuumBtn = root.querySelector('.ctrl-btn[data-action="vacuum"]');
    await vacuumBtn.click();
    await new Promise(r => setTimeout(r, 50));

    expect(vacuumBtn.textContent).not.toContain('✓');
    expect(vacuumBtn.textContent).toContain('db_below_threshold');
    window.confirm = origConfirm;
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

  // 25. (v5.89 redesign) Each row renders the 3-way source badge; the active
  //     category is shown and other categories are reachable via the rail.
  it('renders a 3-way source badge per row (default / yaml / env)', async () => {
    const knobFixture = [
      { name: 'YADGAR_VIZ_NODE_SIZE', kind: 'float', current: '8.0', default: '8.0',
        source: 'default', reload: 'hot_reload', category: 'viz', section: 'viz_config', description: 'Node size', locked: false, enum_choices: [] },
      { name: 'YADGAR_VIZ_MAX_WIKI', kind: 'int', current: '300', default: '200',
        source: 'yaml', reload: 'hot_reload', category: 'viz', section: 'viz_config', description: 'Max wiki nodes', locked: false, enum_choices: [] },
      { name: 'YADGAR_VIZ_PROXY', kind: 'bool', current: 'true', default: 'true',
        source: 'env', reload: 'restart_required', category: 'viz', section: 'viz_config', description: 'Viz proxy', locked: true, enum_choices: [] },
    ];
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && !(opts?.method === 'POST')) {
        return { ok: true, status: 200, json: async () => ({ knobs: knobFixture }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);

    // All three live in category 'viz' (first alphabetically here), so all render.
    const defRow = root.querySelector('.setting-row[data-name="YADGAR_VIZ_NODE_SIZE"]');
    const yamlRow = root.querySelector('.setting-row[data-name="YADGAR_VIZ_MAX_WIKI"]');
    const envRow = root.querySelector('.setting-row[data-name="YADGAR_VIZ_PROXY"]');
    expect(defRow.querySelector('.badge-default')).not.toBeNull();
    expect(yamlRow.querySelector('.badge-yaml')).not.toBeNull();
    expect(envRow.querySelector('.badge-env')).not.toBeNull();
    // env-locked row is read-only: its control input is disabled, no reset btn.
    expect(envRow.classList.contains('env-locked')).toBe(true);
    expect(envRow.querySelector('input').disabled).toBe(true);
    expect(envRow.querySelector('.cfg-reset-btn')).toBeNull();
  });

  // 26. (v5.89 redesign) Cross-category search filters across ALL categories and
  //     highlights the matched substring with <mark>.
  it('cross-category search filters across all categories and <mark>-highlights', async () => {
    const knobFixture = [
      { name: 'YADGAR_VIZ_NODE_SIZE', kind: 'float', current: '8.0', default: '8.0',
        source: 'default', reload: 'hot_reload', category: 'viz', section: 'viz_config', description: 'Node size', locked: false, enum_choices: [] },
      { name: 'YADGAR_WIKI_SIM_MODE', kind: 'string', current: 'hard', default: 'hard',
        source: 'default', reload: 'hot_reload', category: 'wiki', section: 'wiki_similarity_gate', description: 'Similarity gate mode', locked: false, enum_choices: ['hard', 'soft'] },
    ];
    globalThis.fetch = vi.fn(async (url, opts) => {
      if (typeof url === 'string' && url.includes('/api/control/config') && !(opts?.method === 'POST')) {
        return { ok: true, status: 200, json: async () => ({ knobs: knobFixture }) };
      }
      return { ok: true, status: 200, json: async () => ({}) };
    });

    await initControlTab(root);

    // Active category is 'viz' (alpha-first); WIKI knob is in another category.
    const search = root.querySelector('.cfg-search');
    expect(search).not.toBeNull();
    search.value = 'wiki';
    search.dispatchEvent(new Event('input'));

    const searchPane = root.querySelector('.cfg-search-pane');
    expect(searchPane.style.display).not.toBe('none');
    // The wiki knob — in a DIFFERENT category than the active one — is found.
    const hit = searchPane.querySelector('.setting-row[data-name="YADGAR_WIKI_SIM_MODE"]');
    expect(hit).not.toBeNull();
    // The match is highlighted.
    expect(searchPane.querySelector('mark')).not.toBeNull();

    // Clearing the search restores the category view.
    search.value = '';
    search.dispatchEvent(new Event('input'));
    expect(searchPane.style.display).toBe('none');
    expect(root.querySelector('.cfg-category-pane').style.display).not.toBe('none');
  });
});
