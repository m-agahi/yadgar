/**
 * bookmark-spine.js — v5.50.1 Bookmarks Tab BookmarkSpine / shelf
 *
 * Bookmark shelf landing state: 2-col spine grid with HTML5 drag-reorder.
 * j/k keyboard navigation.
 *
 * Exported for tests:
 *   reorderArray(items, fromIdx, toIdx) → new array with item moved
 *   truncateSlug(slug, maxLen) → string
 */

'use strict';

// ── Pure logic (exported for tests) ────────────────────────────────────────

/**
 * Move item at fromIdx to toIdx in a new array (non-mutating).
 * @param {any[]} items
 * @param {number} fromIdx
 * @param {number} toIdx
 * @returns {any[]}
 */
export function reorderArray(items, fromIdx, toIdx) {
  if (fromIdx === toIdx) return items.slice();
  const arr = items.slice();
  const [item] = arr.splice(fromIdx, 1);
  arr.splice(toIdx, 0, item);
  return arr;
}

/**
 * Truncate a slug for display in the spine tile.
 * Keeps the first maxLen chars; adds ellipsis if truncated.
 * @param {string} slug
 * @param {number} [maxLen=12]
 * @returns {string}
 */
export function truncateSlug(slug, maxLen = 12) {
  if (!slug) return '';
  if (slug.length <= maxLen) return slug;
  return slug.slice(0, maxLen) + '…';
}

// ── Component ───────────────────────────────────────────────────────────────

export class BookmarkShelf {
  /**
   * @param {Object} opts
   * @param {HTMLElement} opts.container
   * @param {function(string): void} opts.onSpineClick - (slug) open preview
   * @param {function(string[]): void} opts.onReorder  - (newSlugOrder) persisted
   */
  constructor({ container, onSpineClick, onReorder }) {
    this._container = container;
    this._onSpineClick = onSpineClick;
    this._onReorder = onReorder;
    this._bookmarks = [];
    this._navIdx = -1;
    this._dragSrcIdx = null;
    this._build();
  }

  _build() {
    this._container.className = 'bm-shelf';

    const titleRow = document.createElement('div');
    titleRow.className = 'bm-shelf-title';

    const titleText = document.createElement('span');
    titleText.textContent = 'BOOKMARKED';
    titleRow.appendChild(titleText);

    const dragHint = document.createElement('span');
    // Unicode braille pattern as drag indicator (decorative)
    dragHint.textContent = '⋮⋮⋮';
    dragHint.style.fontSize = '10px';
    dragHint.style.opacity = '0.4';
    titleRow.appendChild(dragHint);

    this._grid = document.createElement('div');
    this._grid.className = 'bm-spine-grid';

    this._emptyEl = document.createElement('div');
    this._emptyEl.className = 'bm-shelf-empty';
    this._emptyEl.textContent = 'No bookmarks yet. Search and star a page to bookmark it.';

    this._container.appendChild(titleRow);
    this._container.appendChild(this._grid);
    this._container.appendChild(this._emptyEl);
  }

  /**
   * Set the bookmarks to display.
   * @param {object[]} bookmarks - [{slug, label_override?, position, ...}]
   */
  setBookmarks(bookmarks) {
    this._bookmarks = bookmarks.slice();
    this._navIdx = -1;
    this._renderGrid();
  }

  _renderGrid() {
    // Clear grid using DOM removal (not innerHTML = '')
    while (this._grid.firstChild) this._grid.removeChild(this._grid.firstChild);

    if (this._bookmarks.length === 0) {
      this._emptyEl.style.display = '';
      return;
    }
    this._emptyEl.style.display = 'none';

    this._bookmarks.forEach((bm, idx) => {
      this._grid.appendChild(this._buildSpine(bm, idx));
    });
  }

  _buildSpine(bm, idx) {
    const tile = document.createElement('div');
    tile.className = 'bm-spine';
    tile.draggable = true;
    tile.dataset.idx = String(idx);
    tile.setAttribute('tabindex', '0');
    tile.setAttribute('role', 'button');
    tile.setAttribute('aria-label', bm.label_override || bm.slug);

    const slugEl = document.createElement('span');
    slugEl.className = 'bm-spine-slug';
    // Display as truncated slug text — safe, no HTML
    slugEl.textContent = truncateSlug(bm.label_override || bm.slug);
    slugEl.title = bm.slug;

    const star = document.createElement('span');
    star.className = 'bm-spine-star';
    star.textContent = '★';
    star.setAttribute('aria-hidden', 'true');

    tile.appendChild(slugEl);
    tile.appendChild(star);

    // Click
    tile.addEventListener('click', () => this._onSpineClick(bm.slug));
    tile.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        this._onSpineClick(bm.slug);
      }
    });

    // Drag-and-drop reorder
    tile.addEventListener('dragstart', (e) => {
      this._dragSrcIdx = idx;
      tile.classList.add('dragging');
      if (e.dataTransfer) {
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', String(idx));
      }
    });
    tile.addEventListener('dragend', () => {
      tile.classList.remove('dragging');
      this._dragSrcIdx = null;
      this._grid.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
    });
    tile.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
      tile.classList.add('drag-over');
    });
    tile.addEventListener('dragleave', () => {
      tile.classList.remove('drag-over');
    });
    tile.addEventListener('drop', (e) => {
      e.preventDefault();
      tile.classList.remove('drag-over');
      const fromIdx = this._dragSrcIdx;
      if (fromIdx === null || fromIdx === idx) return;
      const newOrder = reorderArray(this._bookmarks, fromIdx, idx);
      this._bookmarks = newOrder;
      this._renderGrid();
      this._onReorder(newOrder.map(b => b.slug));
    });

    return tile;
  }

  /**
   * Move keyboard nav focus by delta (+1 = next, -1 = prev).
   * @param {number} delta
   */
  navigate(delta) {
    const n = this._bookmarks.length;
    if (n === 0) return;
    this._navIdx = Math.max(0, Math.min(n - 1, this._navIdx + delta));
    const tiles = this._grid.querySelectorAll('.bm-spine');
    tiles.forEach((t, i) => t.classList.toggle('hover', i === this._navIdx));
    const active = tiles[this._navIdx];
    if (active) active.focus();
  }

  /** Activate the currently nav-focused spine (Enter). */
  activateNav() {
    if (this._navIdx >= 0 && this._bookmarks[this._navIdx]) {
      this._onSpineClick(this._bookmarks[this._navIdx].slug);
    }
  }

  get bookmarks() { return this._bookmarks.slice(); }
}
