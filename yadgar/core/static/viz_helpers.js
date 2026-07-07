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

/**
 * v5.87.1 — decide whether onEngineStop still owes a catch-up zoom-to-fit.
 *
 * Root cause of the "graph blank on initial load until tab-away/back + Reset"
 * bug: warm-start (v5.87 C2) caps cooldownTicks(60) so the engine stops at ~60
 * internal ticks, but the inline auto-fit in onEngineTick fires only at
 * engineTickCount === auto_zoom_fit_tick_threshold (80). 60 < 80 → the camera is
 * never fitted to the warm-started node bounds → nodes sit off-screen → blank
 * canvas. (Reset works because _engineTickCount is reset only in initGraph, so a
 * post-load reheat continues 61→…→80 and finally trips the inline fit.)
 *
 * Returns true whenever the engine settled enough to have a real layout
 * (>= minTicks) but no fit has happened yet this load. We deliberately do NOT
 * gate on the inline fit threshold: stomp protection against an in-flight 800ms
 * pan is already covered by zoomFitDone (the inline fit sets it true in the same
 * block before starting its transition), and the catch-up fit is instant (0ms) so
 * it never creates an in-flight transition. Gating on the threshold instead would
 * wrongly suppress the catch-up on the Reload-button path, where _engineTickCount
 * is NOT reset (only initGraph resets it) so the counter is already past the
 * threshold and the inline `=== threshold` fit can never fire → blank canvas.
 *
 * Pure function — no DOM/global dependencies (the actual zoomToFit + deferred
 * pause stay in index.html; this only encodes the timing decision for unit tests).
 * Keep in sync with the inline copy in index.html onEngineStop handlers.
 *
 * @param {number} engineTickCount - ticks the engine ran this settle (positions valid >= minTicks)
 * @param {boolean} zoomFitDone - whether a fit has already fired this load
 * @param {number} minTicks - settle guard; below this the layout never ran (typ. 50)
 * @returns {boolean} true if a catch-up fit is owed before pausing the render loop
 */
export function shouldFitOnStop(engineTickCount, zoomFitDone, minTicks) {
  return !zoomFitDone && engineTickCount >= minTicks;
}
