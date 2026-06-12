/**
 * help.js — v5.50.13 — Help tab renderer for yadgar viz.
 *
 * Pure function: given a config object (from /api/viz/config), renders
 * the Help tab content into a container element.
 *
 * No hardcoded colors, labels, or descriptions — all content flows from
 * the config.legend block, which is built server-side from canonical sets
 * (WikiStore.CATEGORIES, EDGE_TYPES, NODE_TYPES in viz_meta.py).
 *
 * Exported:
 *   renderHelp(config, container)  — idempotent, clears container first
 */

'use strict';

/**
 * Render the Help tab content.
 *
 * @param {object} config  - Full /api/viz/config response
 * @param {Element} container - DOM element to render into (cleared first)
 */
export function renderHelp(config, container) {
  container.innerHTML = '';

  const legend = config && config.legend;
  if (!legend) {
    const p = document.createElement('p');
    p.className = 'help-error';
    p.textContent = 'Help data unavailable — /api/viz/config did not return a legend block.';
    container.appendChild(p);
    return;
  }

  // ── Intro ──────────────────────────────────────────────────────────────────
  const intro = document.createElement('p');
  intro.className = 'help-intro';
  intro.textContent =
    'This graph visualises your memory store. ' +
    'Nodes are memories, wiki pages, or entities. ' +
    'Edges show relationships between them.';
  container.appendChild(intro);

  // ── Section builder helpers ────────────────────────────────────────────────
  function _section(title) {
    const h = document.createElement('h3');
    h.className = 'help-section-title';
    h.textContent = title;
    container.appendChild(h);
  }

  function _row(swatchColor, label, description, extraClass) {
    const row = document.createElement('div');
    row.className = 'help-row' + (extraClass ? ' ' + extraClass : '');

    const swatch = document.createElement('span');
    swatch.className = 'help-swatch';
    swatch.style.background = swatchColor || '#484f58';
    row.appendChild(swatch);

    const text = document.createElement('span');
    text.className = 'help-label';
    text.textContent = label || '';
    row.appendChild(text);

    if (description) {
      const desc = document.createElement('span');
      desc.className = 'help-desc';
      desc.textContent = description;
      row.appendChild(desc);
    }

    container.appendChild(row);
    return row;
  }

  // ── Node Types & Shapes ────────────────────────────────────────────────────
  _section('Node types & shapes');
  const nodeTypes = (legend.node_types || []);
  for (const nt of nodeTypes) {
    const shapeNote = nt.shape ? ` [${nt.shape}]` : '';
    _row(null, nt.key + shapeNote, nt.description, 'help-row-node');
  }
  if (!nodeTypes.length) {
    const p = document.createElement('p');
    p.className = 'help-empty';
    p.textContent = 'No node type data.';
    container.appendChild(p);
  }

  // ── Wiki Categories ────────────────────────────────────────────────────────
  _section('Wiki categories');
  const note = document.createElement('p');
  note.className = 'help-note';
  note.textContent =
    'Pages with non-canonical categories (legacy or auto-generated) render grey (#8b949e).';
  container.appendChild(note);

  const cats = (legend.categories || []);
  for (const cat of cats) {
    _row(cat.color, cat.label || cat.key, cat.description, 'help-row-category');
  }
  if (!cats.length) {
    const p = document.createElement('p');
    p.className = 'help-empty';
    p.textContent = 'No category data.';
    container.appendChild(p);
  }

  // ── Edge Types ─────────────────────────────────────────────────────────────
  _section('Edge types');
  const edges = (legend.edges || []);
  for (const edge of edges) {
    _row(edge.color, edge.label || edge.key, edge.description, 'help-row-edge');
  }
  if (!edges.length) {
    const p = document.createElement('p');
    p.className = 'help-empty';
    p.textContent = 'No edge type data.';
    container.appendChild(p);
  }

  // ── Heat ──────────────────────────────────────────────────────────────────
  _section('Heat');
  if (legend.heat) {
    const hRow = document.createElement('div');
    hRow.className = 'help-row help-row-heat';

    const grad = document.createElement('span');
    grad.className = 'help-swatch help-swatch-gradient';
    grad.style.background = 'linear-gradient(to right, hsl(240,60%,40%), hsl(0,90%,60%))';
    hRow.appendChild(grad);

    const hText = document.createElement('span');
    hText.className = 'help-label';
    hText.textContent = legend.heat.gradient || '';
    hRow.appendChild(hText);

    container.appendChild(hRow);

    const hDesc = document.createElement('p');
    hDesc.className = 'help-desc help-heat-desc';
    hDesc.textContent = legend.heat.description || '';
    container.appendChild(hDesc);
  }
}
