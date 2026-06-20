/**
 * graph-detail.js — v5.50.12
 *
 * Detail-panel logic extracted from index.html for unit testing.
 * Fixes the stale-state bug: wiki header/title bleeding over memory body
 * when showDetail() did not reset the panel between selections.
 *
 * Fixes:
 *   1. Full panel reset before branching — every shared element cleared.
 *   2. Monotonic selectionId guard — late _fetchWikiContent cannot write to
 *      a panel that has already moved to a newer selection.
 *   3. nodeType() helper — single normalised type string used everywhere
 *      so header label and branch selection can never disagree.
 *   4. SSE ingestion helpers — memory_added, wiki_added/updated, wiki_deleted.
 *
 * index.html wires this module via the <script type="module"> block at the
 * bottom of the file; the plain <script> block delegates to it via
 * window._graphDetail.showDetail(node).
 */

'use strict';

// ── nodeType ─────────────────────────────────────────────────────────────────

/**
 * Return a normalised (lowercase, trimmed) type string for a graph node.
 * @param {object} node
 * @returns {string}  e.g. 'wiki', 'memory', 'entity', '' for unknown/missing
 */
export function nodeType(node) {
  const t = node && node.type;
  // Only treat truthy string/non-zero values as a type; 0, null, undefined → ''
  if (!t) return '';
  return String(t).toLowerCase().trim();
}

// ── Header label map ──────────────────────────────────────────────────────────

const _TYPE_LABELS = { wiki: 'WIKI', memory: 'MEMORY', entity: 'ENTITY' };

function _typeLabel(nt) {
  return _TYPE_LABELS[nt] || (nt ? nt.toUpperCase() : 'UNKNOWN');
}

// ── createDetailPanel ────────────────────────────────────────────────────────

/**
 * Factory that creates showDetail() and _fetchWikiContent() with injected deps.
 *
 * All DOM IDs are fixed per index.html conventions.
 *
 * @param {object} deps
 * @param {function(): object}  deps.wikiCatColor  - () → { category: cssColor }
 * @param {function(number): string} deps.heatColorFn - (heat) → cssColor
 * @param {function(): Array}   deps.allLinksFn    - () → allLinks array snapshot
 * @param {function(string): Promise} deps.fetchImpl - fetch(url) implementation
 * @returns {{ showDetail: function, fetchWikiContent: function }}
 */
export function createDetailPanel({ wikiCatColor, heatColorFn, allLinksFn, fetchImpl }) {
  // Monotonic id incremented on each showDetail call.
  // _fetchWikiContent captures it and only writes DOM if still current.
  let _selectionId = 0;

  function _esc(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  /**
   * Reset every shared panel element unconditionally.
   * Called at the TOP of showDetail before any branch executes.
   */
  function _resetPanel() {
    const detType = document.getElementById('det-type');
    const detTitle = document.getElementById('det-title');
    const detBody = document.getElementById('det-body');
    const fill = document.getElementById('det-heat-fill');

    if (detType)  detType.textContent = '';
    if (detTitle) detTitle.textContent = '';
    if (detBody)  detBody.innerHTML = '';
    if (fill) {
      fill.style.width = '0%';
      fill.style.background = '';
    }
  }

  async function _fetchWikiContent(slug, capturedId) {
    const findEl = () => document.getElementById('wiki-content-body');
    try {
      const r = await fetchImpl(`/api/wiki/read?slug=${encodeURIComponent(slug)}`);
      // Guard: if a newer selection happened since this fetch was launched, discard.
      if (capturedId !== _selectionId) return;
      const el = findEl();
      if (!el) return;
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      // Guard again after the second await
      if (capturedId !== _selectionId) return;
      const elAfter = findEl();
      if (!elAfter) return;
      const content = (d && (d.content || (d.result && d.result.content))) || '';
      if (content) {
        elAfter.style.fontStyle = '';
        elAfter.style.color = '';
        elAfter.textContent = content.length > 800 ? content.slice(0, 800) + '…' : content;
      } else {
        elAfter.textContent = '(no content)';
      }
    } catch (err) {
      if (capturedId !== _selectionId) return;
      const el = findEl();
      if (el) {
        el.style.fontStyle = '';
        el.textContent = `(fetch failed: ${err.message})`;
      }
    }
  }

  function showDetail(node) {
    const panel = document.getElementById('right');
    if (panel) panel.classList.add('open');

    // Step 1: reset all shared elements unconditionally
    _resetPanel();

    // Step 1: increment selectionId so any in-flight fetch becomes stale
    _selectionId += 1;
    const thisId = _selectionId;

    const nt = nodeType(node);
    const label = _typeLabel(nt);

    const detType  = document.getElementById('det-type');
    const detTitle = document.getElementById('det-title');
    const detBody  = document.getElementById('det-body');
    const fill     = document.getElementById('det-heat-fill');

    if (nt === 'wiki') {
      if (detType)  detType.textContent  = label;
      if (detTitle) detTitle.textContent = node.label || node.slug || '';

      const catColors = wikiCatColor();
      const color = (catColors && catColors[node.category]) || '#8b949e';
      if (fill) {
        fill.style.width = '100%';
        fill.style.background = color;
      }

      const rows = [];

      rows.push(`<div class="det-sec">
        <div class="det-lbl">Category</div>
        <div class="det-val">${_esc(node.category || 'uncategorized')}</div>
      </div>`);

      if (node.slug)
        rows.push(`<div class="det-sec">
          <div class="det-lbl">Slug</div>
          <div class="det-val" style="color:#8b949e">${_esc(node.slug)}</div>
        </div>`);

      if (node.tags && node.tags.length)
        rows.push(`<div class="det-sec">
          <div class="det-lbl">Tags</div>
          <div>${node.tags.map((t) => `<span class="tag">${_esc(t)}</span>`).join('')}</div>
        </div>`);

      if (node.updated_at)
        rows.push(`<div class="det-sec">
          <div class="det-lbl">Updated</div>
          <div class="det-val" style="color:#8b949e">${String(node.updated_at).slice(0, 19)}</div>
        </div>`);

      const allLinks = allLinksFn();
      const conns = allLinks.filter((l) => {
        const s = (l.source && l.source.id != null ? l.source.id : l.source);
        const t = (l.target && l.target.id != null ? l.target.id : l.target);
        return s === node.id || t === node.id;
      });
      const byXref   = conns.filter((l) => l.type === 'wiki_crossref').length;
      const byMemWiki = conns.filter((l) => l.type === 'memory_wiki').length;
      rows.push(`<div class="det-sec">
        <div class="det-lbl">Connections</div>
        <div class="det-val" style="color:#8b949e">${byXref} cross-refs · ${byMemWiki} source memories</div>
      </div>`);

      rows.push(`<div class="det-sec">
        <div class="det-lbl">Node ID</div>
        <div class="det-val" style="color:#8b949e">${_esc(node.id)}</div>
      </div>`);

      // Placeholder for async content fetch
      rows.push(`<div class="det-sec" id="wiki-content-sec">
        <div class="det-lbl">Content</div>
        <div class="det-val" id="wiki-content-body" style="color:#8b949e;font-style:italic">Loading…</div>
      </div>`);

      if (detBody) detBody.innerHTML = rows.join('');

      if (node.slug) {
        _fetchWikiContent(node.slug, thisId);
      } else {
        const el = document.getElementById('wiki-content-body');
        if (el) el.textContent = '(no slug)';
      }
      return;
    }

    // Universal fallback: memory, entity, and any future types
    if (detType)  detType.textContent  = label;
    if (detTitle) detTitle.textContent = (node.content || node.label || node.id || '').slice(0, 100);

    const heat = node.heat || 0;
    if (fill) {
      fill.style.width      = `${heat * 100}%`;
      fill.style.background = heatColorFn(heat);
    }

    const rows = [];

    rows.push(`<div class="det-sec">
      <div class="det-lbl">Heat</div>
      <div class="det-val">${heat.toFixed(4)}</div>
    </div>`);

    if (node.content)
      rows.push(`<div class="det-sec">
        <div class="det-lbl">Content</div>
        <div class="det-val">${_esc(node.content)}</div>
      </div>`);

    if (node.tags && node.tags.length)
      rows.push(`<div class="det-sec">
        <div class="det-lbl">Tags</div>
        <div>${node.tags.map((t) => `<span class="tag">${_esc(t)}</span>`).join('')}</div>
      </div>`);

    if (node.directory)
      rows.push(`<div class="det-sec">
        <div class="det-lbl">Project</div>
        <div class="det-val" style="color:#8b949e;word-break:break-all">${_esc(node.directory)}</div>
      </div>`);

    if (node.created_at)
      rows.push(`<div class="det-sec">
        <div class="det-lbl">Created</div>
        <div class="det-val" style="color:#8b949e">${String(node.created_at).slice(0, 19)}</div>
      </div>`);

    // F1 fidelity fix: derive count from the SAME rendered edge set (_edgeToggleState),
    // not a hardcoded subset of 4 types. Entity nodes wired by co_occurrence/imports/
    // calls/resolved_by/caused_by previously showed "0 connections" while their edges
    // were visibly drawn. Now we group ALL incident edges by type dynamically.
    const allLinks = allLinksFn();
    const conns = allLinks.filter((l) => {
      const s = (l.source && l.source.id != null ? l.source.id : l.source);
      const t = (l.target && l.target.id != null ? l.target.id : l.target);
      return s === node.id || t === node.id;
    });
    // Group by type dynamically — single source of truth with the rendered edge list.
    /** @type {Map<string, number>} */
    const byType = new Map();
    for (const l of conns) {
      const et = l.type || 'unknown';
      byType.set(et, (byType.get(et) || 0) + 1);
    }
    let connText;
    if (byType.size === 0) {
      connText = '0 connections';
    } else {
      connText = Array.from(byType.entries())
        .map(([et, n]) => `${n} ${et}`)
        .join(' · ');
    }
    rows.push(`<div class="det-sec">
      <div class="det-lbl">Connections</div>
      <div class="det-val" style="color:#8b949e">${connText}</div>
    </div>`);

    rows.push(`<div class="det-sec">
      <div class="det-lbl">Node ID</div>
      <div class="det-val" style="color:#8b949e">${_esc(node.id)}</div>
    </div>`);

    if (detBody) detBody.innerHTML = rows.join('');
  }

  return { showDetail, fetchWikiContent: _fetchWikiContent };
}

// ── SSE ingestion helpers ─────────────────────────────────────────────────────

/**
 * Ingest an SSE node event into allNodes.
 * Sets node.type explicitly from the event name (never trusts payload).
 *
 * Handles: memory_added, wiki_added, wiki_updated.
 * Mutates allNodes in place (same pattern as the inline code).
 *
 * @param {object} msg     - { event, node }
 * @param {Array}  allNodes - the live allNodes array
 * @returns {boolean} true if the node was added/updated, false if skipped
 */
export function ingestSseNode(msg, allNodes) {
  const { event, node } = msg;
  if (!node) return false;

  const isWiki   = event === 'wiki_added' || event === 'wiki_updated';
  const isMem    = event === 'memory_added';
  if (!isWiki && !isMem) return false;

  // Always set type from the event name — never trust the payload
  node.type = isWiki ? 'wiki' : 'memory';

  if (isMem) {
    node.label = (node.content || '').slice(0, 60);
    node.__deg = node.__deg != null ? node.__deg : 0;
    node.__match = false;
    const idx = allNodes.findIndex((x) => x.id === node.id);
    if (idx === -1) {
      allNodes.push(node);
    }
    // dedup: already exists, do not add again
    return idx === -1;
  }

  // wiki_added or wiki_updated — upsert by id
  const idx = allNodes.findIndex((x) => x.id === node.id);
  if (idx === -1) {
    node.__deg = node.__deg != null ? node.__deg : 0;
    node.__match = false;
    allNodes.push(node);
  } else {
    // Merge: keep structural props, update payload fields
    Object.assign(allNodes[idx], node);
  }
  return true;
}

/**
 * Remove nodes from allNodes matching the deleted slug.
 * wiki_deleted payload is { event, slug } — no id field.
 *
 * @param {object} msg     - { event: 'wiki_deleted', slug: string }
 * @param {Array}  allNodes - the live allNodes array
 */
export function removeSseNode(msg, allNodes) {
  const { slug } = msg;
  if (!slug) return;
  for (let i = allNodes.length - 1; i >= 0; i--) {
    if (allNodes[i].slug === slug) {
      allNodes.splice(i, 1);
    }
  }
}
