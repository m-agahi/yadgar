/**
 * node-popup.js — pure field-model + positioning helpers for the galaxy node
 * popup (Car D, #4). The popup replaces the #right sidebar in galaxy mode with a
 * floating, click-away card anchored near the clicked node (mockup:
 * docs/plans/viz-node-popup.mockup.html).
 *
 * These functions are DOM-free + THREE-free (vitest-covered). index.html renders
 * the np-* markup from popupFieldModel() and positions the card with
 * clampPopupPosition(); the WebGL selection halo is a THREE object parented to
 * the galaxy diskPoints (galaxy-view.js), NOT a CSS ::after (the mockup's halo
 * is on a faux DOM node — the real galaxy has no per-node DOM).
 */

'use strict';

const _TYPE_BADGE = { memory: 'Memory', wiki: 'Wiki', entity: 'Entity' };

/** Wiki nodes auto-widen 340→500 for reading; memory/entity stay compact. */
export function isWideType(type) {
  return type === 'wiki';
}

function _normType(node) {
  const t = node && node.type;
  if (!t) return '';
  return String(t).toLowerCase().trim();
}

function _endId(end) {
  return end && end.id != null ? end.id : end;
}

/**
 * Group a node's incident edges by type (single source of truth with the
 * rendered edge list — mirrors graph-detail.js's dynamic grouping).
 * @returns {Array<{type: string, count: number}>} insertion-ordered
 */
function _connectionsByType(nodeId, allLinks) {
  const byType = new Map();
  for (const l of allLinks || []) {
    const s = _endId(l.source);
    const t = _endId(l.target);
    if (s !== nodeId && t !== nodeId) continue;
    const et = l.type || 'unknown';
    byType.set(et, (byType.get(et) || 0) + 1);
  }
  return Array.from(byType.entries()).map(([type, count]) => ({ type, count }));
}

function _memTitle(node) {
  const t = (node.content || node.label || node.id || '').toString();
  return t.length > 100 ? t.slice(0, 100) : t;
}

/**
 * Build a structured, render-ready field model for the node popup. Type-aware:
 *   - memory: heat bar + content + tags + project + timestamps + connections
 *   - wiki:   category accent + slug + tags + updated + async content + connections; wide
 *   - entity: label + type + connections (no heat, no content)
 *
 * @param {object} node      the raw /api/graph node
 * @param {Array}  allLinks  the rendered edge set (allLinks)
 * @returns {object} { type, badge, title, showHeat, heat, wide, slug, category,
 *                     tags, fields:[{label,value}], connections:[{type,count}], nodeId }
 */
export function popupFieldModel(node, allLinks) {
  const type = _normType(node) || 'memory';
  const badge = _TYPE_BADGE[type] || (type ? type[0].toUpperCase() + type.slice(1) : 'Unknown');
  const connections = _connectionsByType(node && node.id, allLinks);
  const tags = (node && Array.isArray(node.tags) && node.tags) || [];
  const nodeId = (node && node.id) || '';

  if (type === 'wiki') {
    const fields = [];
    fields.push({ label: 'Category', value: (node && node.category) || 'uncategorized' });
    if (node && node.slug) fields.push({ label: 'Slug', value: node.slug, mono: true });
    if (node && node.updated_at) {
      fields.push({ label: 'Updated', value: String(node.updated_at).slice(0, 19), dim: true });
    }
    return {
      type,
      badge,
      title: (node && (node.label || node.slug)) || nodeId,
      showHeat: false,
      heat: 0,
      wide: true,
      slug: (node && node.slug) || null,
      category: (node && node.category) || null,
      tags,
      fields,
      connections,
      nodeId,
      hasContent: true, // async-fetched by slug
    };
  }

  if (type === 'entity') {
    const fields = [];
    if (node && node.entity_type) fields.push({ label: 'Type', value: node.entity_type });
    fields.push({ label: 'Label', value: (node && node.label) || nodeId });
    return {
      type,
      badge,
      title: (node && node.label) || nodeId,
      showHeat: false,
      heat: 0,
      wide: false,
      slug: null,
      category: null,
      tags,
      fields,
      connections,
      nodeId,
      hasContent: false,
    };
  }

  // memory (+ any unknown fallback)
  const heat = (node && Number(node.heat)) || 0;
  const fields = [];
  if (node && node.content) fields.push({ label: 'Content', value: node.content, content: true });
  if (node && node.directory) fields.push({ label: 'Project', value: node.directory, mono: true, dim: true });
  if (node && node.created_at) fields.push({ label: 'Created', value: String(node.created_at).slice(0, 19), dim: true });
  if (node && node.last_accessed) fields.push({ label: 'Last accessed', value: String(node.last_accessed).slice(0, 19), dim: true });
  return {
    type,
    badge,
    title: node ? _memTitle(node) : nodeId,
    showHeat: true,
    heat,
    wide: false,
    slug: null,
    category: null,
    tags,
    fields,
    connections,
    nodeId,
    hasContent: false,
  };
}

/**
 * Position the popup near the clicked node's screen coords, offset +16/+16, and
 * clamp to the viewport (8px margin) so it never clips. Positioned ONCE at click
 * (the halo tracks the node per-frame, the popup does not).
 *
 * @param {{x:number,y:number}} anchor   node screen coords (graphToScreenCoords)
 * @param {{width:number,height:number}} popupSize
 * @param {{width:number,height:number}} viewport
 * @returns {{left:number, top:number}}
 */
export function clampPopupPosition(anchor, popupSize, viewport) {
  const OFFSET = 16;
  const MARGIN = 8;
  const maxLeft = Math.max(MARGIN, viewport.width - popupSize.width - MARGIN);
  const maxTop = Math.max(MARGIN, viewport.height - popupSize.height - MARGIN);
  const left = Math.min(Math.max((anchor.x || 0) + OFFSET, MARGIN), maxLeft);
  const top = Math.min(Math.max((anchor.y || 0) + OFFSET, MARGIN), maxTop);
  return { left, top };
}

/**
 * Clamp a drag-target position so a w×h popup stays fully inside a vw×vh viewport
 * (8px margin). Distinct from clampPopupPosition: no click-anchor offset — the
 * caller supplies the raw desired top-left (x,y) mid-drag, we keep it on-screen.
 *
 * @param {number} x   desired left
 * @param {number} y   desired top
 * @param {number} w   popup width
 * @param {number} h   popup height
 * @param {number} vw  viewport width
 * @param {number} vh  viewport height
 * @returns {{left:number, top:number}}
 */
export function clampToViewport(x, y, w, h, vw, vh) {
  const MARGIN = 8;
  const maxLeft = Math.max(MARGIN, vw - w - MARGIN);
  const maxTop = Math.max(MARGIN, vh - h - MARGIN);
  return {
    left: Math.min(Math.max(x || 0, MARGIN), maxLeft),
    top: Math.min(Math.max(y || 0, MARGIN), maxTop),
  };
}
