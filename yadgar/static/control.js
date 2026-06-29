/**
 * control.js — Control tab implementation for the yadgar viz SPA (v5.87.0).
 *
 * Fills the #tab-control shell.
 *
 * Features:
 *   - Actions row: consolidate / vacuum / re-embed / update
 *   - Config editor: filter + category groups (B1.2) + inline type-aware edit
 *     + reload/restart pill + source badge + locked badge + description tooltip
 *     + per-knob help icon deep-linking to Config Reference page (B2a)
 *     + validation hint on save
 *   - Restart buttons: typed-name confirmation required before firing
 *
 * Gate (ADR-0013, v5.88.2): control + operational endpoints are auth-gated, NOT
 *       behind YADGAR_DEBUG_APIS_ENABLED. config read/write, action
 *       consolidate/reembed/vacuum, and restart all work with a bearer token and
 *       the debug flag off; only /api/logs/* stays debug-gated. A missing token
 *       still yields 401; the banner surfaces that. vacuum and restart require an
 *       explicit confirm before firing.
 *
 * Wire-in: imported from index.html <script type="module">; call initControlTab(container).
 *
 * Exports (pure / testable):
 *   classifyReload         — "hot_reload" | "restart_required" → display label
 *   filterKnobs            — filter + group a knob list
 *   parseEditValue         — coerce input string to typed value for POST
 *   displayKnobValue       — normalize a knob value for display (bool → lowercase)
 *   buildRestartConfirmMsg — return the expected confirm string for a service URL segment
 *   isRestartEnabled       — check confirm input matches expected service name
 *   getKnobCategory        — extract category from knob (with 'all' pass-through)
 *   groupKnobsByCategory   — group sorted knobs by capability category (B1.2)
 */

// ---------------------------------------------------------------------------
// Pure / exported helpers (fully testable without DOM)
// ---------------------------------------------------------------------------

/**
 * Map reload classification to a display label.
 * @param {string} reload - "hot_reload" | "restart_required"
 * @returns {string}
 */
export function classifyReload(reload) {
  return reload === 'hot_reload' ? '●hot' : '⟳restart';
}

/**
 * Filter and optionally group-filter a knob list.
 *
 * @param {Array<Object>} knobs  - array from GET /api/control/config
 * @param {string} filterText    - case-insensitive substring match on name or description
 * @param {string} group         - 'all' | capability category (from knob.category)
 *                                 | legacy prefix groups: 'viz' | 'physics' | 'embedding' | 'storage' | 'misc'
 * @returns {Array<Object>} filtered knobs
 */
export function filterKnobs(knobs, filterText, group) {
  let result = knobs;

  if (filterText) {
    const lower = filterText.toLowerCase();
    result = result.filter(k =>
      k.name.toLowerCase().includes(lower) ||
      (k.description && k.description.toLowerCase().includes(lower)),
    );
  }

  // Category-based filtering (v5.85): if the knob has a 'category' field,
  // use it directly. Otherwise fall back to legacy prefix matching.
  const capabilityCategories = new Set([
    'retrieval', 'storage', 'write-path', 'consolidation', 'enrichment',
    'gate', 'wiki', 'curation', 'mcp-tool', 'observability', 'security',
    'ops', 'brain-dynamics', 'viz', 'config',
  ]);

  if (group && group !== 'all') {
    if (capabilityCategories.has(group)) {
      // Category-based filter: use knob.category if present, else skip
      result = result.filter(k => (k.category || 'config') === group);
    } else {
      // Legacy prefix-based filter (backwards compat)
      const groupPrefixes = {
        viz:       'YADGAR_VIZ_',
        physics:   'YADGAR_VIZ_PHYSICS_',
        embedding: 'YADGAR_EMBEDDING_',
        storage:   'YADGAR_DB_',
      };
      if (group === 'misc') {
        const knownPrefixes = Object.values(groupPrefixes).filter(Boolean);
        result = result.filter(k => !knownPrefixes.some(p => k.name.startsWith(p)));
      } else {
        const prefix = groupPrefixes[group];
        if (prefix) {
          result = result.filter(k => k.name.startsWith(prefix));
        }
      }
    }
  }

  return result;
}

/**
 * Return the capability category of a knob, or 'config' if not present.
 * Pass-through for 'all'.
 *
 * @param {Object} knob - knob object from GET /api/control/config
 * @returns {string}
 */
export function getKnobCategory(knob) {
  return knob.category || 'config';
}

/**
 * Deterministic order for known capability categories.
 * Any category NOT in this list is appended at the end, alpha-sorted.
 */
const CATEGORY_ORDER = [
  'retrieval', 'write-path', 'brain-dynamics', 'enrichment',
  'gate', 'wiki', 'viz', 'observability', 'ops', 'config',
];

/**
 * Map a category key to a human-friendly Title Case display label.
 * Hyphen-separated words are split and each word Title-Cased.
 *
 * @param {string} cat - category key e.g. 'write-path', 'brain-dynamics'
 * @returns {string} display label e.g. 'Write Path', 'Brain Dynamics'
 */
function _categoryLabel(cat) {
  return cat
    .split('-')
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

/**
 * Group a knob array by capability category, in deterministic order.
 * Within each group, knobs are sorted alphabetically by name.
 * Categories absent from the knob set are omitted.
 * Unknown categories (not in CATEGORY_ORDER) are appended alpha-sorted at the end.
 *
 * @param {Array<Object>} knobs - knob objects from GET /api/control/config
 * @returns {Array<{category: string, label: string, knobs: Array<Object>}>}
 */
export function groupKnobsByCategory(knobs) {
  // Bucket knobs by category
  const byCategory = new Map();
  for (const knob of knobs) {
    const cat = getKnobCategory(knob);
    if (!byCategory.has(cat)) byCategory.set(cat, []);
    byCategory.get(cat).push(knob);
  }

  const result = [];

  // Emit known categories in order
  for (const cat of CATEGORY_ORDER) {
    if (!byCategory.has(cat)) continue;
    const sorted = byCategory.get(cat).slice().sort((a, b) => a.name.localeCompare(b.name));
    result.push({ category: cat, label: _categoryLabel(cat), knobs: sorted });
  }

  // Emit unknown categories alpha-sorted
  const knownSet = new Set(CATEGORY_ORDER);
  const unknown = [...byCategory.keys()].filter(c => !knownSet.has(c)).sort();
  for (const cat of unknown) {
    const sorted = byCategory.get(cat).slice().sort((a, b) => a.name.localeCompare(b.name));
    result.push({ category: cat, label: _categoryLabel(cat), knobs: sorted });
  }

  return result;
}

/**
 * Coerce a user-entered string to the right typed value for POST /api/control/config.
 *
 * @param {string} raw   - user input string
 * @param {string} kind  - "int" | "float" | "bool" | "string"
 * @returns {{ value: any, error: string|null }}
 */
export function parseEditValue(raw, kind) {
  if (kind === 'int') {
    const n = parseInt(raw, 10);
    if (isNaN(n) || String(n) !== raw.trim()) {
      return { value: null, error: `Expected integer, got: ${raw}` };
    }
    return { value: n, error: null };
  }
  if (kind === 'float') {
    const n = parseFloat(raw);
    if (isNaN(n)) {
      return { value: null, error: `Expected number, got: ${raw}` };
    }
    return { value: n, error: null };
  }
  if (kind === 'bool') {
    const lc = raw.trim().toLowerCase();
    if (['true', '1', 'yes', 'on'].includes(lc)) return { value: true, error: null };
    if (['false', '0', 'no', 'off'].includes(lc)) return { value: false, error: null };
    return { value: null, error: `Expected true/false, got: ${raw}` };
  }
  // string — accept as-is
  return { value: raw, error: null };
}

/**
 * Normalize a knob value to its canonical display string.
 *
 * Bool knobs are rendered lowercase ("true"/"false") regardless of the source
 * form. Two paths previously produced inconsistent casing: the GET read sends
 * lowercase env strings, while the POST-save response carried Python's
 * capitalized str(True) ("True"/"False"), and JS booleans stringify as
 * "true"/"false". This collapses all of them to the YAML/JSON convention so the
 * config editor never shows a mix of "True" and "true" (ADR-0013).
 *
 * Non-bool kinds and unrecognized bool inputs pass through unchanged (no silent
 * corruption of an unexpected value).
 *
 * @param {string|boolean} value - raw value from GET knob.current/default or POST response
 * @param {string} kind          - knob kind ("bool" | "int" | "float" | "string")
 * @returns {string}
 */
export function displayKnobValue(value, kind) {
  if (kind !== 'bool') return String(value);
  if (value === true) return 'true';
  if (value === false) return 'false';
  const lc = String(value).trim().toLowerCase();
  if (['true', '1', 'yes', 'on'].includes(lc)) return 'true';
  if (['false', '0', 'no', 'off'].includes(lc)) return 'false';
  return String(value);
}

/**
 * Return the expected confirmation string for a restart service URL segment.
 * URL segment "yadgar" → "yadgar"; "backend" → "yadgar-backend".
 *
 * @param {string} segment - URL path segment ("yadgar" | "backend")
 * @returns {string} expected confirm string
 */
export function buildRestartConfirmMsg(segment) {
  return segment === 'backend' ? 'yadgar-backend' : segment;
}

/**
 * Check whether a typed confirmation input enables the restart button.
 *
 * @param {string} inputValue  - user-typed value
 * @param {string} segment     - URL segment ("yadgar" | "backend")
 * @returns {boolean}
 */
export function isRestartEnabled(inputValue, segment) {
  return inputValue.trim() === buildRestartConfirmMsg(segment);
}

// ---------------------------------------------------------------------------
// DOM / API wiring (only runs in-browser; not imported by tests directly)
// ---------------------------------------------------------------------------

const _BASE = '';  // same origin

/**
 * Fetch wrapper that includes bearer token from localStorage.
 * @param {string} url
 * @param {RequestInit} [opts]
 * @returns {Promise<Response>}
 */
function _apiFetch(url, opts = {}) {
  const token = (typeof localStorage !== 'undefined') ? localStorage.getItem('yadgar_token') : '';
  const headers = Object.assign(
    { 'Content-Type': 'application/json' },
    token ? { 'Authorization': `Bearer ${token}` } : {},
    opts.headers || {},
  );
  return fetch(url, Object.assign({}, opts, { headers }));
}

/**
 * Initialise the control tab.
 * Called from the module block in index.html.
 *
 * @param {HTMLElement} root - the #tab-control div
 */
export async function initControlTab(root) {
  if (!root) return;

  // Render skeleton
  root.innerHTML = _buildShell();

  const banner     = root.querySelector('.ctrl-banner');
  const actionsRow = root.querySelector('.ctrl-actions-row');
  const configSec  = root.querySelector('.ctrl-config-section');
  const restartSec = root.querySelector('.ctrl-restart-section');

  // Check gate by attempting GET /api/control/config
  let knobs = [];
  try {
    const r = await _apiFetch(`${_BASE}/api/control/config`);
    if (r.status === 403) {
      _showBanner(banner, '⚠ CONTROL — requires YADGAR_DEBUG_APIS_ENABLED=on', 'warn');
      _disableSection(actionsRow);
      _disableSection(configSec);
      _disableSection(restartSec);
      return;
    }
    if (r.status === 401) {
      _showBanner(banner, '⚠ CONTROL — authentication required (set bearer token in localStorage.yadgar_token)', 'warn');
      _disableSection(actionsRow);
      _disableSection(configSec);
      _disableSection(restartSec);
      return;
    }
    if (!r.ok) {
      _showBanner(banner, `⚠ CONTROL — error ${r.status} fetching config`, 'error');
      return;
    }
    const data = await r.json();
    knobs = data.knobs || [];
  } catch (err) {
    _showBanner(banner, `⚠ CONTROL — network error: ${err.message}`, 'error');
    return;
  }

  banner.style.display = 'none';

  // ── Actions row ───────────────────────────────────────────────────────────
  _renderActions(actionsRow, root);

  // ── Config editor ─────────────────────────────────────────────────────────
  _renderConfigEditor(configSec, knobs);

  // ── Restart section ───────────────────────────────────────────────────────
  _renderRestartSection(restartSec);
}

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------

function _buildShell() {
  return `
<div class="ctrl-banner" style="display:none;"></div>
<div class="ctrl-actions-row ctrl-section">
  <div class="ctrl-section-title">ACTIONS</div>
  <div class="ctrl-buttons"></div>
</div>
<div class="ctrl-config-section ctrl-section">
  <div class="ctrl-section-title">CONFIG EDITOR <span class="ctrl-knob-count"></span></div>
  <div class="ctrl-config-controls">
    <input type="text" class="ctrl-filter" placeholder="search name or description…" />
    <select class="ctrl-group">
      <option value="all">all categories</option>
      <option value="retrieval">retrieval</option>
      <option value="storage">storage</option>
      <option value="write-path">write-path</option>
      <option value="consolidation">consolidation</option>
      <option value="enrichment">enrichment</option>
      <option value="gate">gate</option>
      <option value="wiki">wiki</option>
      <option value="curation">curation</option>
      <option value="observability">observability</option>
      <option value="security">security</option>
      <option value="ops">ops</option>
      <option value="brain-dynamics">brain-dynamics</option>
      <option value="viz">viz</option>
      <option value="config">config</option>
    </select>
  </div>
  <div class="ctrl-config-table-wrap">
    <table class="ctrl-config-table">
      <thead>
        <tr>
          <th>KNOB</th>
          <th>TYPE</th>
          <th>CURRENT</th>
          <th>DEFAULT</th>
          <th>SOURCE</th>
          <th>RELOAD</th>
          <th>EDIT</th>
          <th>REF</th>
        </tr>
      </thead>
      <tbody class="ctrl-config-tbody"></tbody>
    </table>
  </div>
</div>
<div class="ctrl-restart-section ctrl-section">
  <div class="ctrl-section-title">RESTART</div>
  <div class="ctrl-restart-inner"></div>
</div>
`;
}

function _showBanner(banner, msg, level) {
  banner.textContent = msg;
  banner.className = `ctrl-banner ctrl-banner--${level}`;
  banner.style.display = '';
}

function _disableSection(el) {
  if (!el) return;
  el.style.opacity = '0.4';
  el.style.pointerEvents = 'none';
}

// ── Actions row ──────────────────────────────────────────────────────────────

/**
 * Fire a control action. vacuum carries 2-5 min of daemon downtime, so it
 * requires an explicit confirm (browser confirm dialog) and sends a
 * {"confirm":"vacuum"} body the server re-validates (ADR-0013). consolidate
 * (mode=light, ~30s) and re-embed (idempotent) fire one-click with an empty body.
 *
 * @param {HTMLButtonElement} btn
 * @param {string} label
 * @param {string} action - "consolidate" | "vacuum" | "reembed"
 */
async function _fireAction(btn, label, action) {
  let body = '{}';
  if (action === 'vacuum') {
    const ok = (typeof window !== 'undefined' && typeof window.confirm === 'function')
      ? window.confirm('Vacuum causes 2-5 min of daemon downtime. Proceed?')
      : false;
    if (!ok) return;
    body = JSON.stringify({ confirm: 'vacuum' });
  }
  btn.disabled = true;
  btn.textContent = `${label} …`;
  try {
    const r = await _apiFetch(`${_BASE}/api/control/action/${action}`, { method: 'POST', body });
    const respBody = await r.json().catch(() => ({}));
    if (r.ok) {
      _flash(btn, `${label} ✓`, 1500, label);
    } else {
      _flash(btn, `error: ${respBody.error || r.status}`, 2000, label);
    }
  } catch (err) {
    _flash(btn, `net error`, 2000, label);
  } finally {
    btn.disabled = false;
  }
}

function _renderActions(container, root) {
  const btnWrap = container.querySelector('.ctrl-buttons');
  if (!btnWrap) return;

  const actions = [
    { label: '↻ consolidate', action: 'consolidate' },
    { label: '⚒ vacuum',       action: 'vacuum'      },
    { label: '⟳ re-embed',    action: 'reembed'     },
  ];

  for (const { label, action } of actions) {
    const btn = _el('button', { class: 'ctrl-btn', 'data-action': action }, label);
    btn.addEventListener('click', () => _fireAction(btn, label, action));
    btnWrap.appendChild(btn);
  }

  // Update button (detects availability via POST /api/control/update)
  const updateBtn = _el('button', { class: 'ctrl-btn ctrl-btn--update', title: 'checking…' }, '⬆ update');
  updateBtn.disabled = true;
  btnWrap.appendChild(updateBtn);

  _apiFetch(`${_BASE}/api/control/update`, { method: 'POST', body: JSON.stringify({ action: 'check' }) })
    .then(async r => {
      if (r.status === 404 || r.status === 501) {
        updateBtn.disabled = true;
        updateBtn.title = 'requires v5.47 update endpoint';
        updateBtn.classList.add('ctrl-btn--disabled');
      } else if (r.ok) {
        const body = await r.json().catch(() => ({}));
        updateBtn.disabled = false;
        updateBtn.title = body.update_available
          ? `Update available: ${body.available_version}`
          : `Up to date: ${body.current_version}`;
        updateBtn.classList.remove('ctrl-btn--disabled');
        updateBtn.addEventListener('click', () => {
          root.querySelector('.ctrl-banner').textContent = `Run: ${body.upgrade_command}`;
          root.querySelector('.ctrl-banner').className = 'ctrl-banner ctrl-banner--info';
          root.querySelector('.ctrl-banner').style.display = '';
        });
      } else {
        updateBtn.title = `update check failed (${r.status})`;
      }
    })
    .catch(() => {
      updateBtn.title = 'update check unavailable';
    });
}

// ── Config editor ────────────────────────────────────────────────────────────

function _renderConfigEditor(container, knobs) {
  const tbody     = container.querySelector('.ctrl-config-tbody');
  const filterEl  = container.querySelector('.ctrl-filter');
  const groupEl   = container.querySelector('.ctrl-group');
  const countEl   = container.querySelector('.ctrl-knob-count');
  if (!tbody || !filterEl || !groupEl) return;

  function _refresh() {
    const filtered = filterKnobs(knobs, filterEl.value, groupEl.value);
    tbody.innerHTML = '';
    if (countEl) countEl.textContent = `(${filtered.length} knobs)`;

    // B1.2: Group by capability category (deterministic order, alpha-sorted within group)
    const groups = groupKnobsByCategory(filtered);
    for (const group of groups) {
      const sectionRow = document.createElement('tr');
      sectionRow.className = 'ctrl-section-header-row';
      const sectionTd = document.createElement('td');
      sectionTd.colSpan = 8;
      sectionTd.className = 'ctrl-section-header';
      sectionTd.textContent = group.label;
      sectionRow.appendChild(sectionTd);
      tbody.appendChild(sectionRow);
      for (const knob of group.knobs) {
        tbody.appendChild(_buildKnobRow(knob, knobs, _refresh));
      }
    }
  }

  filterEl.addEventListener('input', _refresh);
  groupEl.addEventListener('change', _refresh);
  _refresh();
}

function _buildKnobRow(knob, allKnobs, refreshFn) {
  const tr = document.createElement('tr');
  tr.dataset.name = knob.name;
  if (knob.locked) tr.classList.add('ctrl-row--locked');

  const reloadLabel = classifyReload(knob.reload);
  const reloadClass = knob.reload === 'hot_reload' ? 'ctrl-pill--hot' : 'ctrl-pill--restart';

  // Build cells via DOM methods — knob data is server-provided; never inject raw as HTML.
  // Short name: strip YADGAR_ prefix; full name in title for hover.
  const shortName = knob.name.replace('YADGAR_', '');
  const nameTitle = knob.description
    ? `${knob.name}\n\n${knob.description}`
    : knob.name;
  const tdName = _el('td', { class: 'ctrl-knob-name', title: nameTitle });
  tdName.textContent = shortName;

  const tdType = _el('td', { class: 'ctrl-knob-type' });
  tdType.textContent = knob.kind;

  const tdCurrent = _el('td', { class: 'ctrl-knob-current' });
  const currentDisplay = displayKnobValue(knob.current, knob.kind);
  tdCurrent.dataset.original = currentDisplay;
  tdCurrent.textContent = currentDisplay;

  const tdDefault = _el('td', { class: 'ctrl-knob-default' });
  tdDefault.textContent = displayKnobValue(knob.default, knob.kind);

  // SOURCE badge: ENV (locked) / YAML / DEFAULT
  const tdSource = _el('td', { class: 'ctrl-knob-source' });
  const sourceLabel = (knob.source || 'default').toUpperCase();
  const sourceClass = knob.locked
    ? 'ctrl-pill ctrl-pill--env'
    : sourceLabel === 'YAML'
      ? 'ctrl-pill ctrl-pill--yaml'
      : 'ctrl-pill ctrl-pill--default';
  const sourcePill = _el('span', { class: sourceClass });
  sourcePill.textContent = knob.locked ? '🔒 ENV' : sourceLabel;
  tdSource.appendChild(sourcePill);

  const tdReload = _el('td');
  const pill = _el('span', { class: `ctrl-pill ${reloadClass}` });
  pill.textContent = reloadLabel;
  tdReload.appendChild(pill);

  const editCell = _el('td', { class: 'ctrl-knob-edit-cell' });

  // B2a: Help icon — deep-links to Config Reference page anchor
  const tdHelp = _el('td', { class: 'ctrl-knob-help-cell' });
  const helpLink = _el('a', {
    class: 'ctrl-knob-help',
    href: `#cfgref-${knob.name}`,
    title: `${knob.description || knob.name} — open full reference`,
    'aria-label': `config reference for ${knob.name}`,
  }, 'ⓘ');
  helpLink.addEventListener('click', (e) => {
    e.preventDefault();
    // Switch to config-ref tab first, then scroll to anchor
    window.YadgarTabs?.switchTab?.('config-ref');
    const target = document.getElementById(`cfgref-${knob.name}`);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  tdHelp.appendChild(helpLink);

  tr.appendChild(tdName);
  tr.appendChild(tdType);
  tr.appendChild(tdCurrent);
  tr.appendChild(tdDefault);
  tr.appendChild(tdSource);
  tr.appendChild(tdReload);
  tr.appendChild(editCell);
  tr.appendChild(tdHelp);

  if (knob.locked) {
    // Locked knobs are read-only — show a lock indicator instead of edit button
    const lockSpan = _el('span', { class: 'ctrl-knob-locked-hint', title: 'Set via env var — unset env to allow yaml edit' });
    lockSpan.textContent = 'env-only';
    editCell.appendChild(lockSpan);
  } else {
    const editBtn = _el('button', { class: 'ctrl-btn ctrl-btn--sm', 'aria-label': `edit ${knob.name}` }, '✎');
    editCell.appendChild(editBtn);
    editBtn.addEventListener('click', () => {
      _openInlineEdit(tr, knob, allKnobs, refreshFn);
    });
  }

  return tr;
}

function _openInlineEdit(tr, knob, allKnobs, refreshFn) {
  const currentCell = tr.querySelector('.ctrl-knob-current');
  const originalVal = currentCell.dataset.original;

  // Replace current-value cell with an input
  const input = _el('input', {
    type: 'text',
    class: 'ctrl-edit-input',
    value: originalVal,
    'aria-label': `value for ${knob.name}`,
  });
  const hint = _el('span', { class: 'ctrl-edit-hint' }, '');
  const saveBtn   = _el('button', { class: 'ctrl-btn ctrl-btn--sm ctrl-btn--save' }, '✓');
  const cancelBtn = _el('button', { class: 'ctrl-btn ctrl-btn--sm ctrl-btn--cancel' }, '✕');

  currentCell.innerHTML = '';
  currentCell.appendChild(input);
  currentCell.appendChild(hint);
  currentCell.appendChild(saveBtn);
  currentCell.appendChild(cancelBtn);
  input.focus();
  input.select();

  // Live validation
  input.addEventListener('input', () => {
    const { error } = parseEditValue(input.value, knob.kind);
    hint.textContent = error || '';
    hint.className = `ctrl-edit-hint ${error ? 'ctrl-edit-hint--error' : ''}`;
    saveBtn.disabled = !!error;
  });

  cancelBtn.addEventListener('click', () => {
    currentCell.textContent = originalVal;
    currentCell.dataset.original = originalVal;
  });

  saveBtn.addEventListener('click', async () => {
    const { value, error } = parseEditValue(input.value, knob.kind);
    if (error) {
      hint.textContent = error;
      hint.className = 'ctrl-edit-hint ctrl-edit-hint--error';
      return;
    }
    saveBtn.disabled = true;
    try {
      const r = await _apiFetch(`${_BASE}/api/control/config`, {
        method:  'POST',
        body:    JSON.stringify({ name: knob.name, value }),
      });
      const body = await r.json().catch(() => ({}));
      if (r.ok) {
        // Update the knob in allKnobs array so filter refresh shows new value.
        // Normalize bool casing (server bool POST is already lowercase post-ADR-0013,
        // but older daemons may echo capitalized — collapse defensively).
        const idx = allKnobs.findIndex(k => k.name === knob.name);
        const newCurrent = displayKnobValue(body.value ?? value, knob.kind);
        if (idx !== -1) allKnobs[idx] = Object.assign({}, allKnobs[idx], { current: newCurrent });
        refreshFn();
      } else if (r.status === 409) {
        // Env-locked: the knob is set via env var — yaml write would be shadowed
        hint.textContent = body.error || 'env-locked: unset the env var to allow yaml edit';
        hint.className   = 'ctrl-edit-hint ctrl-edit-hint--error';
        saveBtn.disabled = false;
      } else {
        hint.textContent = body.error || `error ${r.status}`;
        hint.className   = 'ctrl-edit-hint ctrl-edit-hint--error';
        saveBtn.disabled = false;
      }
    } catch (err) {
      hint.textContent = `network error: ${err.message}`;
      hint.className   = 'ctrl-edit-hint ctrl-edit-hint--error';
      saveBtn.disabled = false;
    }
  });
}

// ── Restart section ──────────────────────────────────────────────────────────

function _renderRestartSection(container) {
  const inner = container.querySelector('.ctrl-restart-inner');
  if (!inner) return;

  for (const [segment, displayName] of [['yadgar', 'yadgar'], ['backend', 'yadgar-backend']]) {
    const expected = buildRestartConfirmMsg(segment);
    const wrap = _el('div', { class: 'ctrl-restart-item' });

    const btn = _el('button', { class: 'ctrl-btn ctrl-btn--restart', disabled: 'true' }, `⟲ restart ${displayName}`);
    const label = _el('label', { class: 'ctrl-restart-label' }, `type "${expected}" to confirm: `);
    const confirmInput = _el('input', {
      type:        'text',
      class:       'ctrl-restart-confirm-input',
      placeholder: expected,
      'aria-label': `confirm restart of ${displayName}`,
      'data-segment': segment,
    });

    label.appendChild(confirmInput);
    wrap.appendChild(btn);
    wrap.appendChild(label);
    inner.appendChild(wrap);

    confirmInput.addEventListener('input', () => {
      btn.disabled = !isRestartEnabled(confirmInput.value, segment);
    });

    btn.addEventListener('click', async () => {
      if (!isRestartEnabled(confirmInput.value, segment)) return;
      btn.disabled = true;
      confirmInput.value = '';
      try {
        const r = await _apiFetch(`${_BASE}/api/control/restart/${segment}`, {
          method: 'POST',
          body:   JSON.stringify({ confirm: expected }),
        });
        const body = await r.json().catch(() => ({}));
        if (r.status === 202) {
          _flash(btn, `⟲ restart ${displayName} ✓ (sentinel written)`, 3000, `⟲ restart ${displayName}`);
        } else {
          _flash(btn, `error: ${body.error || r.status}`, 3000, `⟲ restart ${displayName}`);
          btn.disabled = false;
        }
      } catch (err) {
        _flash(btn, `net error`, 2000, `⟲ restart ${displayName}`);
        btn.disabled = false;
      }
    });
  }
}

// ---------------------------------------------------------------------------
// Micro-utilities
// ---------------------------------------------------------------------------

function _el(tag, attrs = {}, text = '') {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'disabled' && v) el.disabled = true;
    else el.setAttribute(k, v);
  }
  if (text) el.textContent = text;
  return el;
}

function _esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function _flash(btn, msg, ms, originalText) {
  btn.textContent = msg;
  setTimeout(() => { btn.textContent = originalText; }, ms);
}
