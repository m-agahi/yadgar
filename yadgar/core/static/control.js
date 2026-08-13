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

// Chrome-style settings redesign helpers (v5.89) — pure, vitest-covered in
// control_helpers.test.js. The DOM layer below renders over them.
import {
  alphabeticalCategories,
  groupKnobsAlphabetical,
  searchKnobs,
  highlightSegments,
  knobCategory,
  deriveBadgeState,
  computePending,
  controlKind,
  isDestructive,
  toggleArmed,
  classify428,
  formatConfigStatus,
  categoryPendingCounts,
  pendingDiffs,
  armCountdown,
} from './control_helpers.js';

// viz-rest #29: daemon version captured from the update-check, surfaced in the
// config header status line. '' until the check resolves.
let _daemonVersion = '';

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

// Surface 2 (neural-console restyle): the config editor is a 3-column shell —
// rail (categories + per-category pending badges) | content | commit tray. The
// former bottom pending-BAR is now the tray FOOT (.cfg-pending-bar preserved as a
// class so control.js's queries + test 19 still resolve). The tray is always
// visible with an empty-state; refreshPending() renders diffs into .cfg-tray-body
// rather than hiding the whole column.
function _buildShell() {
  return `
<div class="ctrl-banner" style="display:none;"></div>
<div class="ctrl-actions-row ctrl-section">
  <div class="ctrl-section-title">ACTIONS</div>
  <div class="ctrl-buttons"></div>
</div>
<div class="ctrl-config-section ctrl-section">
  <div class="ctrl-section-title">CONFIG EDITOR <span class="ctrl-knob-count"></span><span class="ctrl-cfg-status"></span></div>
  <div class="cfg-shell">
    <nav class="cfg-rail">
      <div class="cfg-rail-search-wrap">
        <input class="cfg-search" type="search" placeholder="Search settings" autocomplete="off" spellcheck="false" aria-label="search all settings" />
      </div>
      <div class="cfg-rail-section-label">Categories</div>
      <div class="cfg-rail-items"></div>
    </nav>
    <div class="cfg-content-wrap">
      <div class="cfg-content">
        <div class="cfg-search-header" style="display:none"></div>
        <div class="cfg-search-pane" style="display:none"></div>
        <div class="cfg-search-empty" style="display:none">No settings match the search.</div>
        <div class="cfg-category-pane"></div>
      </div>
    </div>
    <aside class="cfg-tray">
      <div class="cfg-tray-head">
        <h3 class="cfg-tray-title">Commit tray</h3>
        <div class="cfg-tray-sub"></div>
      </div>
      <div class="cfg-tray-body"></div>
      <div class="cfg-pending-bar">
        <span class="cfg-pending-label">0 unsaved changes</span>
        <button class="ctrl-btn cfg-btn-restart" style="display:none">↻ Restart daemon</button>
        <button class="ctrl-btn cfg-btn-discard">Discard</button>
        <button class="ctrl-btn ctrl-btn--save cfg-btn-apply">Apply</button>
      </div>
    </aside>
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
 * Fire a control action. vacuum pauses DB access for ~2-5 min while it runs —
 * the core daemon itself stays up (no restart, no MCP session drop; DB-backed
 * tools fast-fail during the window instead) — so it requires an explicit
 * confirm (browser confirm dialog) and sends a {"confirm":"vacuum"} body the
 * server re-validates (ADR-0013). consolidate (mode=light, ~30s) and re-embed
 * (idempotent) fire one-click with an empty body.
 *
 * @param {HTMLButtonElement} btn
 * @param {string} label
 * @param {string} action - "consolidate" | "vacuum" | "reembed"
 */
async function _fireAction(btn, label, action) {
  let body = '{}';
  if (action === 'vacuum') {
    const ok = (typeof window !== 'undefined' && typeof window.confirm === 'function')
      ? window.confirm('Vacuum pauses DB access for ~2-5 min (the daemon itself stays up). Proceed?')
      : false;
    if (!ok) return;
    body = JSON.stringify({ confirm: 'vacuum' });
  }
  btn.disabled = true;
  btn.textContent = `${label} …`;
  try {
    const r = await _apiFetch(`${_BASE}/api/control/action/${action}`, { method: 'POST', body });
    const respBody = await r.json().catch(() => ({}));
    if (r.ok && respBody.result && respBody.result.started === false) {
      // HTTP 200 does NOT mean the action ran — vacuum_now() can accept the
      // request and still skip (e.g. skipped_reason: "db_below_threshold").
      // Surface the real outcome instead of a false "✓" (Car 10).
      _flash(btn, `${label}: ${respBody.result.skipped_reason || 'skipped'}`, 3000, label);
    } else if (r.ok) {
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
        // viz-rest #29: capture the version for the config header status line.
        if (body.current_version) {
          _daemonVersion = body.current_version;
          const _statusEl = root.querySelector('.ctrl-cfg-status');
          if (_statusEl && !_statusEl.textContent) {
            _statusEl.textContent = ' — ' + formatConfigStatus(_daemonVersion, 0, false);
          }
        }
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

// ── Config editor (chrome://settings-style redesign, v5.89) ───────────────────
//
// Left rail of ALPHABETICAL categories (+ counts) + a content pane of grouped
// rows with 3-way source badges, typed controls, a cross-category live search,
// per-row reset-to-default, and a sticky pending-changes bar (Apply/Discard +
// optional Restart). All decision logic lives in control_helpers.js (vitest);
// this is the thin DOM layer over it.

function _renderConfigEditor(container, knobs) {
  const railItems = container.querySelector('.cfg-rail-items');
  const searchEl  = container.querySelector('.cfg-search');
  const catPane   = container.querySelector('.cfg-category-pane');
  const searchPane = container.querySelector('.cfg-search-pane');
  const searchHdr = container.querySelector('.cfg-search-header');
  const searchEmpty = container.querySelector('.cfg-search-empty');
  const countEl   = container.querySelector('.ctrl-knob-count');
  const pendingBar = container.querySelector('.cfg-pending-bar');
  const pendingLabel = container.querySelector('.cfg-pending-label');
  const applyBtn  = container.querySelector('.cfg-btn-apply');
  const discardBtn = container.querySelector('.cfg-btn-discard');
  const restartBtn = container.querySelector('.cfg-btn-restart');
  // Surface 2: commit-tray body + subtitle (new 3rd column).
  const trayBody  = container.querySelector('.cfg-tray-body');
  const traySub   = container.querySelector('.cfg-tray-sub');
  if (!railItems || !searchEl || !catPane) return;

  // ── Edit state ────────────────────────────────────────────────────────────
  // originalValues = server-resolved value; currentValues = live edits (display
  // strings). source overrides flip default/yaml→yaml after a successful Apply.
  const originalValues = {};
  const currentValues = {};
  const sourceOverride = {};
  // Car D: destructive rows the user has armed (drives applyOne's {armed:true}
  // POST + enables the row's edit control).
  let armedRows = new Set();
  // Surface 2: per-row arm expiry timestamps (ms) + countdown interval handles.
  // The arm auto-expires after _ARM_TTL_MS, re-disabling the control.
  const _ARM_TTL_MS = 15000;
  const _armExpiry = new Map();
  const _armTimers = new Map();
  function _clearArmTimer(name) {
    const t = _armTimers.get(name);
    if (t) { clearInterval(t); _armTimers.delete(name); }
  }
  for (const k of knobs) {
    const disp = displayKnobValue(k.current, k.kind);
    originalValues[k.name] = disp;
    currentValues[k.name] = disp;
    sourceOverride[k.name] = k.source || 'default';
  }

  const cats = alphabeticalCategories(knobs);
  let activeCategory = cats.length ? cats[0].category : null;

  // ── Pending / commit tray ─────────────────────────────────────────────────
  // Surface 2: the tray is ALWAYS the 3rd column. refreshPending drives its
  // subtitle + body (diff cards) + foot (Apply/Discard/Restart) rather than
  // hiding a bottom bar. The rail's per-category pending badges + the header
  // status line are re-synced here too, all off the same computePending diff so
  // rail, tray, and header can never diverge.
  function refreshPending() {
    // Carry `destructive` through so computePending can count dirty destructive
    // knobs (Car D — surfaced in red on the tray + header).
    const knobsView = knobs.map(k => ({ name: k.name, reload: k.reload, destructive: k.destructive }));
    const p = computePending(knobsView, originalValues, currentValues);
    // viz-rest #29 + Surface 2: config header status line (version · pending ·
    // destructive · restart).
    const statusEl = container.querySelector('.ctrl-cfg-status');
    if (statusEl) statusEl.textContent = ' — ' + formatConfigStatus(_daemonVersion, p.count, p.restartRequired, p.destructiveCount);

    // Foot label + destructive tint + restart button visibility.
    let label = p.count > 0
      ? `${p.count} unsaved change${p.count === 1 ? '' : 's'}`
      : 'No pending changes';
    if (p.destructiveCount > 0) label += ` — ${p.destructiveCount} destructive`;
    pendingLabel.textContent = label;
    pendingLabel.classList.toggle('cfg-pending-destructive', p.destructiveCount > 0);
    restartBtn.style.display = p.restartRequired ? '' : 'none';
    applyBtn.disabled = p.count === 0;
    discardBtn.disabled = p.count === 0;
    // The tray/foot stays mounted (mockup: permanent 3rd column). A .cfg-empty
    // class dims the foot when nothing is staged.
    pendingBar.style.display = '';
    pendingBar.classList.toggle('cfg-empty', p.count === 0);

    renderTray(p);
    renderRailBadges();
    return p;
  }

  // Render the commit-tray body (one diff card per dirty knob) + subtitle.
  function renderTray(p) {
    if (!trayBody) return;
    const diffs = pendingDiffs({ knobs, originalValues, currentValues });
    if (traySub) {
      const restartN = diffs.filter(d => d.restart).length;
      traySub.textContent = diffs.length === 0
        ? 'Nothing staged'
        : `${diffs.length} pending change${diffs.length === 1 ? '' : 's'}` +
          (restartN ? ` · ${restartN} need restart` : '');
    }
    trayBody.innerHTML = '';
    if (diffs.length === 0) {
      trayBody.appendChild(_el('div', { class: 'cfg-tray-empty' }, 'No changes yet'));
      return;
    }
    for (const d of diffs) {
      const card = _el('div', { class: 'cfg-chg' + (d.destructive ? ' destructive' : '') });
      card.appendChild(_el('div', { class: 'cfg-chg-name' }, d.name));
      const diffLine = _el('div', { class: 'cfg-chg-diff' });
      diffLine.appendChild(_el('span', { class: 'cfg-chg-old' }, d.oldValue));
      diffLine.appendChild(_el('span', { class: 'cfg-chg-arr' }, '→'));
      diffLine.appendChild(_el('span', { class: 'cfg-chg-new' + (d.destructive ? ' destructive' : '') }, d.newValue));
      card.appendChild(diffLine);
      if (d.restart || d.destructive) {
        const meta = _el('div', { class: 'cfg-chg-meta' });
        if (d.destructive) meta.appendChild(_el('span', { class: 'danger-tag' }, 'destructive'));
        if (d.restart) meta.appendChild(_el('span', { class: 'restart-pill' }, '↻ restart'));
        card.appendChild(meta);
      }
      trayBody.appendChild(card);
    }
  }

  // Update the per-category pending badges on the rail without a full re-render
  // (keeps rail-item click listeners intact). Adds/updates/removes a .rail-pending
  // pill per category off categoryPendingCounts.
  function renderRailBadges() {
    const counts = categoryPendingCounts({ knobs, originalValues, currentValues });
    railItems.querySelectorAll('.rail-item').forEach(item => {
      const cat = item.getAttribute('data-cat');
      const n = counts[cat] || 0;
      let pill = item.querySelector('.rail-pending');
      if (n > 0) {
        if (!pill) {
          pill = _el('span', { class: 'rail-pending' });
          item.appendChild(pill);
        }
        pill.textContent = String(n);
      } else if (pill) {
        pill.remove();
      }
    });
  }

  function setCurrent(name, value) {
    currentValues[name] = String(value);
    refreshPending();
    // toggle the row .cfg-changed marker (search + category panes both use
    // data-name). Knob names are [A-Z0-9_] so the attribute selector is safe.
    const dirty = String(currentValues[name]) !== String(originalValues[name]);
    container.querySelectorAll(`.setting-row[data-name="${name}"]`)
      .forEach(row => row.classList.toggle('cfg-changed', dirty));
  }

  // ── Rail ────────────────────────────────────────────────────────────────────
  function renderRail() {
    railItems.innerHTML = '';
    if (countEl) countEl.textContent = `(${knobs.length} knobs)`;
    for (const c of cats) {
      const item = _el('div', { class: 'rail-item' + (c.category === activeCategory && !searchEl.value.trim() ? ' active' : ''), 'data-cat': c.category });
      const name = _el('span', {}, c.label);
      const count = _el('span', { class: 'rail-count' }, String(c.count));
      item.append(name, count);
      item.addEventListener('click', () => {
        if (searchEl.value.trim()) return; // ignore rail clicks during search
        activeCategory = c.category;
        renderRail();
        renderCategoryPane();
      });
      railItems.appendChild(item);
    }
    // Surface 2: re-apply per-category pending badges after a rail rebuild.
    renderRailBadges();
  }

  // ── A single setting row (shared by category + search panes) ──────────────────
  function buildRow(knob, query) {
    const badge = deriveBadgeState({ source: sourceOverride[knob.name], locked: knob.locked });
    const destructive = isDestructive(knob);
    const row = _el('div', { class: 'setting-row', 'data-name': knob.name });
    if (badge.locked) row.classList.add('env-locked');
    if (destructive) row.classList.add('destructive');  // Car D: red border via CSS
    if (String(currentValues[knob.name]) !== String(originalValues[knob.name])) row.classList.add('cfg-changed');

    // LEFT
    const left = _el('div', { class: 'row-left' });
    const labelLine = _el('div', { class: 'row-label' });
    if (destructive) {
      // ⚠ marker before the label (Car D). Title spells out the consequence.
      const warn = _el('span', { class: 'destructive-marker', title: 'Destructive — permanently deletes data. Arm before saving.' }, '⚠ ');
      labelLine.appendChild(warn);
    }
    _appendHighlighted(labelLine, knob.description || knob.name.replace('YADGAR_', ''), query);
    if (query) {
      const chip = _el('span', { class: 'cat-chip' }, knobCategory(knob));
      labelLine.appendChild(chip);
    }
    if (knob.reload === 'restart_required') {
      labelLine.appendChild(_el('span', { class: 'restart-pill' }, '↻ restart required'));
    }
    const knobName = _el('div', { class: 'row-knob' });
    _appendHighlighted(knobName, knob.name, query);
    left.append(labelLine, knobName);

    // RIGHT
    const right = _el('div', { class: 'row-right' });
    right.appendChild(_buildBadgeEl(badge));

    if (badge.resettable || (badge.editable && String(currentValues[knob.name]) !== String(originalValues[knob.name]))) {
      const reset = _el('button', { class: 'cfg-reset-btn', title: 'Reset to built-in default', 'aria-label': `reset ${knob.name}` }, '⟲ Reset');
      reset.addEventListener('click', () => handleReset(knob));
      right.appendChild(reset);
    }

    // Car D + Surface 2: a destructive row must be ARMED before its control is
    // editable. The typed-name confirm is replaced by an arm BUTTON + a live
    // "expires in Ns" countdown (mockup fidelity). Arming toggles inline WITHOUT a
    // rerender so focus/countdown are not torn down. The armed Set gates control
    // editability; applyOne sends armed:true off isDestructive() — the countdown is
    // PRESENTATION ONLY and never feeds the POST flag (an expired arm re-disables
    // the control, so a dirty destructive knob is always still-armed at Apply).
    const disableControlFor = destructive && badge.editable && !armedRows.has(knob.name);
    const controlEl = _buildControl(knob, badge.editable, setCurrent, () => currentValues[knob.name]);

    if (destructive && badge.editable) {
      const armBtn = _el('button', {
        class: 'cfg-arm-btn',
        'aria-label': `arm ${knob.name}`,
        'data-arm': knob.name,
      }, '⚠ Arm');
      const countdownEl = _el('span', { class: 'cfg-arm-countdown' });

      const setControlDisabled = (disabled) => {
        controlEl.querySelectorAll('input, select, textarea')
          .forEach(el => { el.disabled = disabled; });
      };

      const disarm = () => {
        _clearArmTimer(knob.name);
        armedRows = toggleArmed(armedRows, knob.name, false);
        armBtn.classList.remove('armed');
        armBtn.textContent = '⚠ Arm';
        countdownEl.textContent = '';
        setControlDisabled(true);
      };

      const tickCountdown = () => {
        const expiry = _armExpiry.get(knob.name);
        const { seconds, expired } = armCountdown(expiry, Date.now());
        if (expired) { disarm(); return; }
        countdownEl.textContent = `armed — expires in ${seconds}s`;
      };

      const arm = () => {
        armedRows = toggleArmed(armedRows, knob.name, true);
        armBtn.classList.add('armed');
        armBtn.textContent = '⚠ Armed';
        setControlDisabled(false);
        _armExpiry.set(knob.name, Date.now() + _ARM_TTL_MS);
        tickCountdown();
        _clearArmTimer(knob.name);
        _armTimers.set(knob.name, setInterval(tickCountdown, 1000));
      };

      // Reflect a pre-existing armed state (e.g. after a category re-render while
      // the arm is still live).
      if (armedRows.has(knob.name)) {
        armBtn.classList.add('armed');
        armBtn.textContent = '⚠ Armed';
        tickCountdown();
        _clearArmTimer(knob.name);
        _armTimers.set(knob.name, setInterval(tickCountdown, 1000));
      }

      armBtn.addEventListener('click', () => {
        if (armedRows.has(knob.name)) disarm();
        else arm();
      });

      right.append(armBtn, countdownEl);
    }

    // Disable the destructive control's inputs until armed (build-time state).
    if (disableControlFor) {
      controlEl.querySelectorAll('input, select, textarea')
        .forEach(el => { el.disabled = true; });
    }
    right.appendChild(controlEl);
    row.append(left, right);
    return row;
  }

  // ── Category pane ─────────────────────────────────────────────────────────────
  function renderCategoryPane() {
    catPane.style.display = '';
    searchPane.style.display = 'none';
    searchHdr.style.display = 'none';
    searchEmpty.style.display = 'none';
    catPane.innerHTML = '';
    if (!activeCategory) return;
    const grouped = groupKnobsAlphabetical(knobs).find(g => g.category === activeCategory);
    if (!grouped) return;

    catPane.appendChild(_el('h2', { class: 'cfg-cat-heading' }, grouped.label));
    for (const sec of grouped.sections) {
      const group = _el('div', { class: 'cfg-section-group' });
      if (grouped.sections.length > 1) {
        group.appendChild(_el('div', { class: 'cfg-section-label' }, sec.section));
      }
      for (const knob of sec.knobs) group.appendChild(buildRow(knob, ''));
      catPane.appendChild(group);
    }
  }

  // ── Search pane (cross-category) ──────────────────────────────────────────────
  function renderSearch(query) {
    catPane.style.display = 'none';
    searchPane.style.display = '';
    railItems.querySelectorAll('.rail-item').forEach(el => el.classList.remove('active'));

    const matches = searchKnobs(knobs, query);
    if (matches.length === 0) {
      searchPane.innerHTML = '';
      searchHdr.style.display = 'none';
      searchEmpty.style.display = '';
      return;
    }
    searchEmpty.style.display = 'none';
    searchHdr.style.display = '';
    searchHdr.textContent = `${matches.length} result${matches.length === 1 ? '' : 's'} for "${query}"`;
    searchPane.innerHTML = '';
    for (const knob of matches) searchPane.appendChild(buildRow(knob, query));
  }

  // ── Reset / Apply / Discard / Restart ─────────────────────────────────────────
  function handleReset(knob) {
    const def = displayKnobValue(knob.default, knob.kind);
    setCurrent(knob.name, def);
    rerender();
  }

  function rerender() {
    if (searchEl.value.trim()) renderSearch(searchEl.value.trim());
    else renderCategoryPane();
    renderRail();
  }

  async function applyOne(knob) {
    const { value, error } = parseEditValue(String(currentValues[knob.name]), knob.kind);
    if (error) return { name: knob.name, ok: false, error };
    // Car D: a destructive knob's write must carry armed:true. The row control is
    // only editable once armed, so an armed row is guaranteed here — but send the
    // flag explicitly so the 428 gate passes.
    const payload = { name: knob.name, value };
    if (isDestructive(knob)) payload.armed = true;
    try {
      const r = await _apiFetch(`${_BASE}/api/control/config`, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      const body = await r.json().catch(() => ({}));
      if (r.ok) {
        const newCurrent = displayKnobValue(body.value ?? value, knob.kind);
        originalValues[knob.name] = newCurrent;
        currentValues[knob.name] = newCurrent;
        sourceOverride[knob.name] = 'yaml'; // a saved knob is now yaml-sourced
        armedRows = toggleArmed(armedRows, knob.name, false); // disarm after a save
        _clearArmTimer(knob.name); _armExpiry.delete(knob.name);
        const idx = knobs.findIndex(k => k.name === knob.name);
        if (idx !== -1) knobs[idx] = Object.assign({}, knobs[idx], { current: newCurrent, source: 'yaml' });
        return { name: knob.name, ok: true };
      }
      // Defensive: a 428 means the write needs arming (the gate refused it). Surface
      // it as an actionable "arm this row" error rather than a raw status.
      const cls = classify428({ status: r.status, body });
      if (cls.needsArming) {
        armedRows = toggleArmed(armedRows, knob.name, false);
        _clearArmTimer(knob.name); _armExpiry.delete(knob.name);
        return { name: knob.name, ok: false, error: `needs arming: ${cls.hint}`, status: r.status };
      }
      return { name: knob.name, ok: false, error: body.error || `error ${r.status}`, status: r.status };
    } catch (err) {
      return { name: knob.name, ok: false, error: `network error: ${err.message}` };
    }
  }

  async function handleApply() {
    const p = computePending(knobs, originalValues, currentValues);
    const dirtyKnobs = knobs.filter(k => p.dirty.has(k.name));
    applyBtn.disabled = true;
    const results = await Promise.all(dirtyKnobs.map(applyOne));
    applyBtn.disabled = false;
    const failed = results.filter(r => !r.ok);
    rerender();
    refreshPending();
    if (failed.length) {
      searchHdr.style.display = '';
      searchHdr.textContent = `Apply failed for ${failed.length} knob(s): ${failed.map(f => `${f.name} (${f.error})`).join('; ')}`;
    }
  }

  function handleDiscard() {
    for (const name of Object.keys(currentValues)) currentValues[name] = originalValues[name];
    // Surface 2: discarding also disarms every armed destructive row + stops its
    // countdown (the edits it guarded are gone).
    for (const name of [...armedRows]) { armedRows = toggleArmed(armedRows, name, false); }
    for (const name of [..._armTimers.keys()]) _clearArmTimer(name);
    _armExpiry.clear();
    rerender();
    refreshPending();
  }

  function handleRestart() {
    const ok = (typeof window !== 'undefined' && typeof window.confirm === 'function')
      ? window.confirm('Restarting drops the live connection. Continue?')
      : false;
    if (!ok) return;
    _apiFetch(`${_BASE}/api/control/restart/yadgar`, {
      method: 'POST',
      body: JSON.stringify({ confirm: 'yadgar' }),
    }).catch(() => {});
  }

  applyBtn.addEventListener('click', handleApply);
  discardBtn.addEventListener('click', handleDiscard);
  restartBtn.addEventListener('click', handleRestart);

  searchEl.addEventListener('input', () => {
    const q = searchEl.value.trim();
    if (q) renderSearch(q);
    else { renderCategoryPane(); renderRail(); }
  });

  renderRail();
  renderCategoryPane();
  refreshPending();
}

/**
 * Append highlighted text segments (pure helper output) to a parent element.
 * Each marked segment becomes a <mark>; the rest are text nodes. Never sets
 * innerHTML from data — segments carry only text.
 */
function _appendHighlighted(parent, text, query) {
  for (const seg of highlightSegments(text, query)) {
    if (seg.mark) {
      const m = document.createElement('mark');
      m.textContent = seg.text;
      parent.appendChild(m);
    } else {
      parent.appendChild(document.createTextNode(seg.text));
    }
  }
}

/** Build the 3-way source badge element from a derived badge state. */
function _buildBadgeEl(badge) {
  const el = _el('span', { class: `badge badge-${badge.state}` }, badge.label);
  if (badge.state === 'env') {
    el.setAttribute('data-tooltip', 'Set via environment / nix — edit there, not here.');
    el.title = 'Set via environment / nix — edit there, not here.';
  }
  return el;
}

/**
 * Build the typed control for a knob. editable=false (env-locked) → disabled.
 * setFn(name, value) records the edit; getFn() reads the live value.
 */
function _buildControl(knob, editable, setFn, getFn) {
  const kind = controlKind(knob);
  const cur = getFn();

  if (kind === 'toggle') {
    const wrap = _el('div', { class: 'toggle-wrap' });
    const inp = _el('input', { type: 'checkbox', 'aria-label': knob.name });
    inp.checked = displayKnobValue(cur, 'bool') === 'true';
    inp.disabled = !editable;
    if (editable) inp.addEventListener('change', () => setFn(knob.name, inp.checked ? 'true' : 'false'));
    const track = _el('div', { class: 'toggle-track' });
    const thumb = _el('div', { class: 'toggle-thumb' });
    wrap.append(inp, track, thumb);
    return wrap;
  }

  if (kind === 'slider') {
    const wrap = _el('div', { class: 'slider-wrap' });
    const num = _el('input', { type: 'number', class: 'num-input', 'aria-label': knob.name });
    num.value = cur;
    num.disabled = !editable;
    const range = _el('input', { type: 'range' });
    range.disabled = !editable;
    // bounds: best-effort from current/default magnitude when the API gives none
    const base = parseFloat(cur) || parseFloat(displayKnobValue(knob.default, knob.kind)) || 1;
    range.min = String(Math.min(0, base));
    range.max = String(base === 0 ? 100 : Math.max(base * 4, base + 10));
    range.step = knob.kind === 'float' ? 'any' : '1';
    range.value = cur;
    if (editable) {
      range.addEventListener('input', () => { num.value = range.value; setFn(knob.name, range.value); });
      num.addEventListener('input', () => { range.value = num.value; setFn(knob.name, num.value); });
    }
    wrap.append(range, num);
    return wrap;
  }

  if (kind === 'select') {
    const sel = _el('select', { class: 'sel-input', 'aria-label': knob.name });
    sel.disabled = !editable;
    for (const choice of knob.enum_choices) {
      const opt = _el('option', { value: choice }, choice);
      if (choice === String(cur)) opt.selected = true;
      sel.appendChild(opt);
    }
    if (editable) sel.addEventListener('change', () => setFn(knob.name, sel.value));
    return sel;
  }

  // text
  const inp = _el('input', { type: 'text', class: 'text-input', 'aria-label': knob.name });
  inp.value = cur;
  inp.disabled = !editable;
  if (editable) inp.addEventListener('input', () => setFn(knob.name, inp.value));
  return inp;
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
