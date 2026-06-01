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

/**
 * Compute link/edge width based on edge type and count.
 * Pure function — no DOM or global config dependencies.
 *
 * @param {Object} l - Edge object with {type, count?}
 * @returns {number} Link width in pixels
 */
export function _linkWidth(l) {
  if (l.type === 'transition') return Math.min(4, 1 + Math.log2(l.count || 1));
  if (l.type === 'semantic') return 1;
  if (l.type === 'wiki_crossref') return 1;
  return 1.5;
}

/**
 * Check whether a graph payload has orphan edges.
 * Returns the set of endpoint IDs not present in the node set.
 *
 * This is the algebraic check that would have caught the v5.10.9 root cause:
 * entity:* edge endpoints not in the node list crashing force-graph.min.js.
 *
 * @param {{nodes: Array<{id: string}>, edges: Array<{source: string, target: string}>}} payload
 * @returns {Set<string>} Set of orphan endpoint IDs (empty = no orphans)
 */
export function findOrphanEdgeEndpoints(payload) {
  const nodeIds = new Set((payload.nodes || []).map((n) => String(n.id)));
  const orphans = new Set();
  for (const e of payload.edges || []) {
    const src = String(e.source);
    const tgt = String(e.target);
    if (!nodeIds.has(src)) orphans.add(src);
    if (!nodeIds.has(tgt)) orphans.add(tgt);
  }
  return orphans;
}
