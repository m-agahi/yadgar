/**
 * viz-search.js — pure helpers for the dedicated Search tab (Car D, #10).
 *
 * The Search tab consumes GET /api/viz/search (returns {node_ids: [...]}) and
 * renders a type-aware results list. These pure functions do the routing logic:
 *   - parseNodeRef:      split a "mem:N" / "wiki:N" / "entity:X" id into {type, rawId}
 *   - buildSearchResults: resolve node_ids against the loaded graph (allNodes),
 *                         degrading gracefully for out-of-graph hits (500-node cap)
 *   - routeSearchResult: decide where a clicked result goes (graph focus vs wiki page)
 *
 * DOM-free + THREE-free so vitest can cover them; the inline copy in index.html
 * (plain <script> can't import) is read-verified equivalent.
 */

'use strict';

/** node id prefix → normalized node type. */
const _PREFIX_TYPE = { mem: 'memory', wiki: 'wiki', entity: 'entity' };

/**
 * Split a graph node id into its type + raw id.
 * Ids look like "mem:530093", "wiki:6782", "entity:galaxy-view.js" (raw id may
 * itself contain ':'), so we split on the FIRST colon only.
 * @param {string} id
 * @returns {{type: string, rawId: string}} type '' when unprefixed/unknown
 */
export function parseNodeRef(id) {
  const s = id == null ? '' : String(id);
  const i = s.indexOf(':');
  if (i < 0) return { type: '', rawId: s };
  const prefix = s.slice(0, i);
  const rest = s.slice(i + 1);
  const type = _PREFIX_TYPE[prefix];
  if (!type) return { type: '', rawId: s };
  return { type, rawId: rest };
}

function _memTitle(node) {
  const t = (node.content || node.label || node.id || '').toString();
  return t.length > 120 ? t.slice(0, 120) + '…' : t;
}

/**
 * Build type-aware result rows from search node_ids + the loaded graph nodes.
 * In-graph hits carry the resolved node (rich title/slug); out-of-graph hits
 * (beyond the 500-node render cap) are surfaced with type parsed from the id
 * prefix and node=null. Order preserved, duplicates dropped.
 *
 * @param {string[]} nodeIds  from /api/viz/search
 * @param {Map<string, object>} nodesById  id → node object (allNodes indexed)
 * @returns {Array<{id, type, title, subtitle, slug, node, inGraph}>}
 */
export function buildSearchResults(nodeIds, nodesById) {
  if (!Array.isArray(nodeIds) || nodeIds.length === 0) return [];
  const map = nodesById instanceof Map ? nodesById : new Map();
  const seen = new Set();
  const rows = [];
  for (const id of nodeIds) {
    if (id == null || seen.has(id)) continue;
    seen.add(id);
    const node = map.get(id) || null;
    const { type: parsedType } = parseNodeRef(id);
    const type = (node && node.type) || parsedType || '';
    let title;
    let subtitle = '';
    let slug = null;
    if (type === 'wiki') {
      title = (node && (node.label || node.slug)) || id;
      slug = (node && node.slug) || null;
      subtitle = (node && node.category) || 'wiki';
    } else if (type === 'entity') {
      title = (node && node.label) || id;
      subtitle = (node && node.entity_type) || 'entity';
    } else {
      // memory (and any unknown fallback)
      title = node ? _memTitle(node) : id;
      subtitle = 'memory';
    }
    rows.push({ id, type, title, subtitle, slug, node, inGraph: node != null });
  }
  return rows;
}

/**
 * Decide where a clicked search result navigates.
 *   - wiki with a slug   → open the wiki page ('open-wiki')
 *   - memory / entity in graph, or wiki without slug but in graph → 'focus-graph'
 *   - not in graph and no slug → 'none' (can't focus a node the graph didn't load)
 *
 * @param {{id, type, slug, node, inGraph}} row
 * @returns {{action: 'open-wiki'|'focus-graph'|'none', nodeId: string, slug?: string}}
 */
export function routeSearchResult(row) {
  if (!row) return { action: 'none', nodeId: '' };
  if (row.type === 'wiki' && row.slug) {
    return { action: 'open-wiki', slug: row.slug, nodeId: row.id };
  }
  if (row.inGraph && row.node) {
    return { action: 'focus-graph', nodeId: row.id };
  }
  return { action: 'none', nodeId: row.id };
}
