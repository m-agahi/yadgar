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
