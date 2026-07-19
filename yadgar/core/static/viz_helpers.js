/**
 * viz_helpers.js — Pure helper functions extracted from index.html for unit testing.
 *
 * These functions have no DOM dependencies and no side effects.
 * Imported by index.html via <script src="viz_helpers.js"> and testable with Vitest.
 *
 * Layer 3 of v5.37.0 viz integration testing infrastructure.
 */

/**
 * Format bytes as human-readable string.
 * @param {number|null} b - Bytes to format
 * @returns {string} Formatted string (e.g. "1.5 MB")
 */
export function _fmtBytes(b) {
  if (b == null) return '—';
  if (b >= 1073741824) return (b / 1073741824).toFixed(1) + ' GB';
  if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB';
  if (b >= 1024) return Math.round(b / 1024) + ' KB';
  return b + ' B';
}

/**
 * Format uptime seconds as human-readable string.
 * @param {number|null} s - Seconds of uptime
 * @returns {string} Formatted string (e.g. "2h 30m")
 */
export function _fmtUptime(s) {
  if (s == null) return '—';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return h + 'h ' + m + 'm';
  if (m > 0) return m + 'm ' + Math.floor(s % 60) + 's';
  return Math.round(s) + 's';
}

/**
 * Escape HTML special characters.
 * @param {string} s - Raw string
 * @returns {string} HTML-escaped string
 */
export function esc(s) {
  // Matches the esc() implementation in index.html (line 1430):
  // escapes &, <, > — does NOT escape double quotes.
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

// ADR-0135 (galaxy-only): _linkWidth / particleCount / convexHull /
// findOrphanEdgeEndpoints / shouldFitOnStop were force-directed-renderer helpers
// (edge styling, 3D particles, 2D cluster hulls, force-graph orphan crash guard,
// warm-start engine-settle timing). The force renderer was removed, so these were
// deleted along with their inline index.html copies and their vitest blocks.

// ── #69 unified-panel pure reducers ────────────────────────────────────────────
// The EDGES panel section groups the edge types under Retrieval / Informational
// master toggles + shows a live COUNT per type and per group (folding in the edge
// counts the old GRAPH STATS panel used to show). These reducers are DOM-free so
// the count + toggle logic is vitest-covered; the DOM render is a smoke-check.

/**
 * Aggregate a link list into per-type and per-role-group counts.
 * `legendEdges` (YADGAR_VIZ_CONFIG.legend.edges) supplies each type's role and
 * declares the full type set so a type with zero live edges still reports 0.
 *
 * @param {Array<{type?:string, role?:string}>} links  allLinks
 * @param {Array<{key:string, role:string}>} [legendEdges]  legend edge descriptors
 * @returns {{byType: Object<string,number>,
 *            byGroup: {retrieval:number, informational:number},
 *            roleOf: Object<string,string>}}
 */
export function aggregateEdgeCounts(links, legendEdges) {
  const byType = Object.create(null);
  const roleOf = Object.create(null);
  // Seed from the legend so declared-but-empty types report 0 (not missing).
  for (const e of legendEdges || []) {
    if (!e || e.key == null) continue;
    byType[e.key] = 0;
    roleOf[e.key] = e.role === 'retrieval' ? 'retrieval' : 'informational';
  }
  for (const l of links || []) {
    const t = l && l.type;
    if (t == null) continue;
    byType[t] = (byType[t] || 0) + 1;
    // Prefer the wire role; fall back to the legend role; else informational.
    if (!(t in roleOf)) {
      roleOf[t] = l.role === 'retrieval' ? 'retrieval' : 'informational';
    }
  }
  const byGroup = { retrieval: 0, informational: 0 };
  for (const t of Object.keys(byType)) {
    const g = roleOf[t] === 'retrieval' ? 'retrieval' : 'informational';
    byGroup[g] += byType[t];
  }
  return { byType, byGroup, roleOf };
}

/**
 * Reduce a group-master toggle action into the next per-type toggle state.
 * Flipping a master sets every type in that role group to the master's value;
 * per-type sub-toggles remain independently settable afterwards. Master state is
 * DERIVED (a group is "on" iff ≥1 of its types is on) — see edgeGroupIsOn.
 *
 * @param {Object<string,boolean>} toggleState  current type→shown (missing=shown)
 * @param {string} group  'retrieval' | 'informational'
 * @param {boolean} on    new master value
 * @param {Object<string,string>} roleOf  type→role
 * @returns {Object<string,boolean>} NEW toggle state (input not mutated)
 */
export function edgeGroupToggleReducer(toggleState, group, on, roleOf) {
  const next = { ...(toggleState || {}) };
  for (const t of Object.keys(roleOf || {})) {
    if ((roleOf[t] === 'retrieval' ? 'retrieval' : 'informational') === group) {
      next[t] = !!on;
    }
  }
  return next;
}

/**
 * Derive whether a role group's master toggle reads "on": true iff at least one
 * type in the group is currently shown (missing key = shown, the default).
 *
 * @param {Object<string,boolean>} toggleState  type→shown (missing=shown)
 * @param {string} group  'retrieval' | 'informational'
 * @param {Object<string,string>} roleOf  type→role
 * @returns {boolean}
 */
export function edgeGroupIsOn(toggleState, group, roleOf) {
  const st = toggleState || {};
  let any = false;
  for (const t of Object.keys(roleOf || {})) {
    if ((roleOf[t] === 'retrieval' ? 'retrieval' : 'informational') !== group) continue;
    any = true;
    if (st[t] !== false) return true; // missing or true = shown
  }
  return any ? false : false; // empty group → off
}

/**
 * Toggle a collapsible section's expanded state. Pure — the panel stores a
 * {sectionName: expanded} map and this returns the next map (input untouched).
 *
 * @param {Object<string,boolean>} state  section→expanded
 * @param {string} name  section id
 * @returns {Object<string,boolean>} NEW state
 */
export function sectionToggleReducer(state, name) {
  const next = { ...(state || {}) };
  next[name] = !next[name];
  return next;
}
