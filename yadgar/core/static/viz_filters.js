/**
 * viz_filters.js — v5.54.3 — Pure filter + role helpers for the viz edge system.
 *
 * Extracted from index.html so logic is unit-testable via vitest.
 * No DOM/global dependencies — pure functions only.
 *
 * Exports:
 *   buildEdgeTypeMap(legendEdges)   — {type → {role, default_on, color, lazy}}
 *   edgeVisible(link, toggleState)  — boolean: is this edge visible?
 *   edgeRole(type, edgeTypeMap)     — "retrieval" | "display" | null
 *   linksChanged(before, after)     — boolean: did graphData().links change size?
 *   edgeCbKey(type)                 — "fo-show-" + type.replace(/_/g, '-')
 */

'use strict';

/**
 * Build a lookup map from legend.edges (config from /api/viz/config).
 *
 * @param {Array<{key, role, default_on, color, lazy}>} legendEdges
 * @returns {Object} map: type → {role, default_on, color, lazy}
 */
export function buildEdgeTypeMap(legendEdges) {
  const map = {};
  for (const edge of (legendEdges || [])) {
    map[edge.key] = {
      role: edge.role || 'display',
      default_on: edge.default_on !== false,  // true unless explicitly false
      color: edge.color || '#484f58',
      lazy: edge.lazy === true,
    };
  }
  return map;
}

/**
 * Filter a links array down to those whose edge TYPE is toggled on — the set
 * that must be fed to the d3 force simulation (graph.graphData().links).
 *
 * v5.87 C1: the force-link layer must exclude hidden edge types, otherwise the
 * invisible link force keeps formerly-linked nodes clumped after a toggle-off.
 * Keys off edge-TYPE only (via edgeVisible) — deliberately NOT node-visibility
 * (`__visible`) or search/focus state, which belong on the visual linkVisibility
 * layer. Folding node visibility in here would rebuild+reheat the sim on every
 * search keystroke (graph jumps while typing) and scatter hidden nodes on re-show.
 *
 * @param {Array<{type}>} links - all links (allLinks)
 * @param {Object} toggleState - {type: boolean} per-type checkbox state
 * @returns {Array} links whose type is toggled on
 */
export function visibleForceLinks(links, toggleState) {
  return (links || []).filter(l => edgeVisible(l, toggleState || {}));
}

/**
 * Return the checkbox element ID for a given edge type key.
 * Convention: underscores → hyphens, prefixed with "fo-show-".
 *
 * @param {string} type - edge type key (e.g. "co_occurrence")
 * @returns {string} checkbox ID (e.g. "fo-show-co-occurrence")
 */
export function edgeCbKey(type) {
  return 'fo-show-' + String(type).replace(/_/g, '-');
}

/**
 * Decide if a link is visible given the current toggle state map.
 *
 * @param {Object} link  - edge object with {type}
 * @param {Object} toggleState - {type: boolean} checked state per type
 * @returns {boolean}
 */
export function edgeVisible(link, toggleState) {
  const t = link && link.type;
  if (!t) return true;  // unknown type: show by default
  if (t in toggleState) return !!toggleState[t];
  return true;  // type not in state: show by default
}

/**
 * viz-rest #70: return an edge's numeric weight for the threshold filter.
 * `count` (transition co-recall strength) takes precedence over `weight`
 * (entity-relation / similarity-link strength). Returns null for edges that
 * carry no weight metric — those are never pruned by the weight filter.
 *
 * @param {Object} link - edge object, may carry {count} and/or {weight}
 * @returns {number|null}
 */
export function edgeWeightOf(link) {
  if (!link) return null;
  if (typeof link.count === 'number') return link.count;
  if (typeof link.weight === 'number') return link.weight;
  return null;
}

/**
 * viz-rest #70: does an edge pass the min-weight threshold?
 * Threshold 0 (default) passes everything. An edge with no weight metric always
 * passes (the filter only prunes weighted edges). A weighted edge passes only
 * when its weight is >= minWeight.
 *
 * @param {Object} link - edge object
 * @param {number} minWeight - slider threshold (>= 0)
 * @returns {boolean}
 */
export function edgePassesWeight(link, minWeight) {
  if (!minWeight || minWeight <= 0) return true;
  const w = edgeWeightOf(link);
  if (w === null) return true;  // unweighted edge — not subject to the filter
  return w >= minWeight;
}

/**
 * Return the role ("retrieval" | "display") for an edge type.
 *
 * @param {string} type - edge type key
 * @param {Object} edgeTypeMap - from buildEdgeTypeMap()
 * @returns {"retrieval" | "display" | null}
 */
export function edgeRole(type, edgeTypeMap) {
  if (!type || !edgeTypeMap) return null;
  return (edgeTypeMap[type] && edgeTypeMap[type].role) || null;
}

/**
 * Return true if the number of links changed (i.e. a reheat is warranted).
 *
 * Used to gate d3ReheatSimulation calls: only reheat when links actually
 * change count (e.g. lazy semantic edges appended), not on visibility toggles.
 *
 * @param {number} beforeCount - link count before the operation
 * @param {number} afterCount  - link count after the operation
 * @returns {boolean}
 */
export function linksChanged(beforeCount, afterCount) {
  return beforeCount !== afterCount;
}

/**
 * Compute link color based on role + edge type map.
 * Retrieval-role edges: brighter/solid (full opacity).
 * Display-role edges: dimmer (reduced opacity).
 *
 * @param {Object} link - edge object with {type}
 * @param {Object} edgeTypeMap - from buildEdgeTypeMap()
 * @returns {string} CSS color string
 */
export function edgeLinkColor(link, edgeTypeMap) {
  const t = link && link.type;
  const meta = t && edgeTypeMap && edgeTypeMap[t];
  if (!meta) return 'rgba(130,130,130,0.3)';
  const color = meta.color || '#484f58';
  // Retrieval-active: full opacity (solid, load-bearing)
  // Display-only: 45% opacity (dimmer, decorative)
  if (meta.role === 'retrieval') {
    return color;
  }
  // Parse hex to rgba for dimming
  return _hexToRgba(color, 0.45);
}

/**
 * Compute link width based on role + edge metadata.
 * Retrieval-active edges: thicker (default 1.5 or transition-scaled).
 * Display-only edges: thinner (0.8 for semantic, 1.0 for others).
 *
 * @param {Object} link - edge object with {type, count?, role?}
 * @param {Object} edgeTypeMap - from buildEdgeTypeMap()
 * @returns {number} width in pixels
 */
export function edgeLinkWidth(link, edgeTypeMap) {
  const t = link && link.type;
  const meta = t && edgeTypeMap && edgeTypeMap[t];

  // Transition edges: width scales with co-recall count
  if (t === 'transition') return Math.min(4, 1 + Math.log2(link.count || 1));

  if (meta && meta.role === 'retrieval') return 1.5;
  if (t === 'semantic') return 0.8;
  return 1.0;
}

// ── Internal helpers ──────────────────────────────────────────────────────────

/**
 * Convert a hex color string to rgba() with the given alpha.
 * Falls back to rgba(130,130,130,alpha) on parse error.
 *
 * @param {string} hex - e.g. "#1f6feb"
 * @param {number} alpha - 0–1
 * @returns {string} e.g. "rgba(31,111,235,0.45)"
 */
export function _hexToRgba(hex, alpha) {
  try {
    const h = hex.replace('#', '');
    const r = parseInt(h.substring(0, 2), 16);
    const g = parseInt(h.substring(2, 4), 16);
    const b = parseInt(h.substring(4, 6), 16);
    if (isNaN(r) || isNaN(g) || isNaN(b)) throw new Error('bad hex');
    return `rgba(${r},${g},${b},${alpha})`;
  } catch {
    return `rgba(130,130,130,${alpha})`;
  }
}
