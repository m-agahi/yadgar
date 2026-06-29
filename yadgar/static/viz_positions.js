/**
 * viz_positions.js — v5.87 C2 — Pure warm-start position helpers for the viz graph.
 *
 * Extracted from index.html so logic is unit-testable via vitest.
 * No DOM/global dependencies — pure functions only.
 *
 * Problem (C2): on a FRESH page reload the in-memory `allNodes` is empty, so the
 * existing in-session posMap (index.html loadGraph) restores nothing and the d3
 * force layout runs its full cold settle from a phyllotaxis spiral — the graph
 * "flies around" for the cooldown window on every reload. Persisting settled
 * positions to localStorage and seeding them back on the next load makes first
 * paint show the already-laid-out graph and lets the sim settle in far fewer ticks.
 *
 * Exports:
 *   serializeNodePositions(nodes)            — {id → {x,y,z?}} of finite-position nodes
 *   restoreNodePositions(nodes, saved)       — seed x/y/z onto nodes in place; returns count
 *   pruneStalePositions(saved, currentIds)   — drop saved ids absent from the payload
 *   parsePositionStore(json)                 — safe JSON parse → map, {} on malformed
 *   serializePositionStore(map)              — JSON.stringify wrapper (symmetry/testability)
 */

'use strict';

/**
 * True only for a real, finite number (rejects NaN, Infinity, null, undefined).
 * @param {*} v
 * @returns {boolean}
 */
function _isFiniteNum(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

/**
 * Build a {id → {x,y,z?}} map from the current node objects, keeping only nodes
 * that have finite x AND y. z is included only when finite (2D nodes have no z).
 *
 * Mirrors what the d3-force layout writes onto each node (n.x / n.y / n.z). We
 * deliberately do NOT persist fx/fy/fz (hard pins) — seeding hard pins on reload
 * would freeze the layout so newly-added nodes could never integrate.
 *
 * @param {Array<{id, x, y, z?}>} nodes
 * @returns {Object} map id → {x, y, z?}
 */
export function serializeNodePositions(nodes) {
  const out = {};
  for (const n of (nodes || [])) {
    if (!n || n.id == null) continue;
    if (!_isFiniteNum(n.x) || !_isFiniteNum(n.y)) continue;
    const rec = { x: n.x, y: n.y };
    if (_isFiniteNum(n.z)) rec.z = n.z;
    out[n.id] = rec;
  }
  return out;
}

/**
 * Seed saved positions onto node objects in place. Only assigns x/y (and z when
 * both the saved record and the assignment make sense). Nodes with no saved entry
 * are left untouched so the force sim positions them (new nodes integrate near
 * the saved scaffold). Returns the number of nodes that received a position.
 *
 * Restores x/y/z ONLY — never fx/fy/fz — so the layout stays free to relax.
 *
 * @param {Array<{id}>} nodes
 * @param {Object} saved - {id → {x,y,z?}}
 * @returns {number} count of nodes seeded
 */
export function restoreNodePositions(nodes, saved) {
  if (!saved) return 0;
  let count = 0;
  for (const n of (nodes || [])) {
    if (!n || n.id == null) continue;
    const p = saved[n.id];
    if (!p) continue;
    if (!_isFiniteNum(p.x) || !_isFiniteNum(p.y)) continue;
    n.x = p.x;
    n.y = p.y;
    if (_isFiniteNum(p.z)) n.z = p.z;
    count++;
  }
  return count;
}

/**
 * Drop saved positions whose ids are no longer present in the current payload,
 * so the persisted store cannot grow unbounded as the live graph churns.
 *
 * @param {Object} saved - {id → {x,y,z?}}
 * @param {Iterable<string>} currentIds - ids present in the fresh payload
 * @returns {Object} pruned copy (saved is not mutated)
 */
export function pruneStalePositions(saved, currentIds) {
  const keep = currentIds instanceof Set ? currentIds : new Set(currentIds || []);
  const out = {};
  for (const id of Object.keys(saved || {})) {
    if (keep.has(id)) out[id] = saved[id];
  }
  return out;
}

/**
 * Safely parse a persisted position store. Returns {} on null/empty/malformed
 * input or if the parsed value is not a plain object — never throws.
 *
 * @param {string|null|undefined} json
 * @returns {Object} map id → {x,y,z?} (possibly empty)
 */
export function parsePositionStore(json) {
  if (!json || typeof json !== 'string') return {};
  try {
    const v = JSON.parse(json);
    if (!v || typeof v !== 'object' || Array.isArray(v)) return {};
    return v;
  } catch {
    return {};
  }
}

/**
 * Serialize a position map to a JSON string for persistence.
 * Thin wrapper for symmetry with parsePositionStore + testability.
 *
 * @param {Object} map
 * @returns {string}
 */
export function serializePositionStore(map) {
  return JSON.stringify(map || {});
}

/**
 * Count nodes that arrive already carrying a finite (x, y) position (v5.88).
 *
 * The server-side precomputed-layout feature (VIZ_PRECOMPUTED_LAYOUT_ENABLED)
 * attaches x/y/z to nodes in the /api/graph payload. Those nodes are already
 * seeded — so the localStorage warm-start restore (which only touches nodes
 * WITHOUT finite x) leaves them alone and server positions take priority. This
 * counter lets the caller know the payload was server-seeded so it can cap
 * cooldownTicks for a near-instant render (same path as the localStorage
 * warm-start), instead of running a full cold force settle.
 *
 * @param {Array<{x?, y?}>} nodes
 * @returns {number} count of nodes with finite x AND y
 */
export function countSeededPositions(nodes) {
  let count = 0;
  for (const n of (nodes || [])) {
    if (n && _isFiniteNum(n.x) && _isFiniteNum(n.y)) count++;
  }
  return count;
}
