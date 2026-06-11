/**
 * versions-rail.js — v5.50.1 Bookmarks Tab VersionsRail component
 *
 * Vertical timeline of wiki page versions.
 * Click to preview historical version; shift-click multi-select for compare.
 *
 * Exported for tests:
 *   applySelection(versions, clickedIdx, shiftKey, currentSelection) → number[]
 *   relTime(isoString, nowMs) → string
 *   computeSparklineValues(versions) → number[]
 */

'use strict';

// ── Pure logic (exported for tests) ────────────────────────────────────────

/**
 * Compute new selection array after a lozenge click.
 *
 * Rules:
 *   - Plain click: select only this version.
 *   - Shift+click: if one already selected, select range [min, max].
 *     If none selected, select only this version.
 *   - Result is always sorted ascending by index.
 *
 * @param {object[]} versions - ordered array (newest-first as returned by API)
 * @param {number} clickedIdx - index into versions array that was clicked
 * @param {boolean} shiftKey
 * @param {number[]} currentSelection - currently selected indices
 * @returns {number[]} new selection (sorted ascending)
 */
export function applySelection(versions, clickedIdx, shiftKey, currentSelection) {
  if (!shiftKey || currentSelection.length === 0) {
    return [clickedIdx];
  }
  const anchor = currentSelection[0];
  const lo = Math.min(anchor, clickedIdx);
  const hi = Math.max(anchor, clickedIdx);
  const result = [];
  for (let i = lo; i <= hi; i++) result.push(i);
  return result;
}

/**
 * Format an ISO timestamp as a human-readable relative time string.
 * @param {string|null} isoString
 * @param {number} [nowMs] - override for testing (Date.now())
 * @returns {string}
 */
export function relTime(isoString, nowMs = Date.now()) {
  if (!isoString) return '';
  const d = new Date(isoString);
  if (isNaN(d.getTime())) return '';
  const secs = Math.floor((nowMs - d.getTime()) / 1000);
  if (secs < 0) return 'just now';
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

/**
 * Compute sparkline height values (0–8px range) from version size_bytes.
 * Returns normalized values for canvas drawing.
 *
 * @param {object[]} versions - each has .size_bytes (may be null)
 * @returns {number[]} heights in [0, 8]
 */
export function computeSparklineValues(versions) {
  const sizes = versions.map(v => (typeof v.size_bytes === 'number' ? v.size_bytes : 0));
  const max = Math.max(...sizes, 1);
  return sizes.map(s => Math.round((s / max) * 8));
}

// ── Component ───────────────────────────────────────────────────────────────

export class VersionsRail {
  /**
   * @param {Object} opts
   * @param {HTMLElement} opts.container
   * @param {function(object, number): void} opts.onVersionClick - (version, idx)
   * @param {function(number[]): void} opts.onSelectionChange  - (selectedIndices)
   * @param {function(number[]): void} opts.onCompare          - (selectedIndices, versions[])
   * @param {function(object): void} opts.onRestore            - (version)
   */
  constructor({ container, onVersionClick, onSelectionChange, onCompare, onRestore }) {
    this._container = container;
    this._onVersionClick = onVersionClick;
    this._onSelectionChange = onSelectionChange;
    this._onCompare = onCompare;
    this._onRestore = onRestore;
    this._versions = [];
    this._selected = []; // indices into _versions
    this._build();
  }

  _build() {
    this._container.className = 'bm-versions-rail';

    const header = document.createElement('div');
    header.className = 'bm-versions-header';
    header.textContent = 'HISTORY';

    this._list = document.createElement('div');
    this._list.className = 'bm-versions-list';

    this._actions = document.createElement('div');
    this._actions.className = 'bm-versions-actions';

    this._compareBtn = document.createElement('button');
    this._compareBtn.className = 'bm-versions-btn';
    this._compareBtn.textContent = '⇄ compare';
    this._compareBtn.disabled = true;
    this._compareBtn.addEventListener('click', () => {
      if (this._selected.length >= 2) {
        this._onCompare(this._selected, this._versions);
      }
    });

    this._restoreBtn = document.createElement('button');
    this._restoreBtn.className = 'bm-versions-btn';
    this._restoreBtn.textContent = '↶ restore';
    this._restoreBtn.disabled = true;
    this._restoreBtn.addEventListener('click', () => {
      if (this._selected.length === 1) {
        this._onRestore(this._versions[this._selected[0]]);
      }
    });

    this._actions.appendChild(this._compareBtn);
    this._actions.appendChild(this._restoreBtn);

    this._container.appendChild(header);
    this._container.appendChild(this._list);
    this._container.appendChild(this._actions);
  }

  /**
   * Populate the rail with versions (newest-first from /api/wiki_history).
   * @param {object[]} versions
   * @param {number} currentVersionNum - version number of currently shown page
   */
  setVersions(versions, currentVersionNum) {
    this._versions = versions;
    this._selected = [];
    this._currentVersionNum = currentVersionNum;
    this._renderList();
    this._updateButtons();
  }

  _renderList() {
    this._list.innerHTML = '';
    for (let i = 0; i < this._versions.length; i++) {
      this._list.appendChild(this._buildLozenge(i));
    }
  }

  _buildLozenge(idx) {
    const v = this._versions[idx];
    const isCurrent = v.version === this._currentVersionNum;

    const loz = document.createElement('div');
    loz.className = 'bm-version-lozenge' + (isCurrent ? ' current' : '');
    loz.dataset.idx = String(idx);

    // Meta row
    const meta = document.createElement('div');
    meta.className = 'bm-version-lozenge-meta';

    const marker = document.createElement('span');
    marker.className = 'bm-version-marker' + (isCurrent ? ' current' : '');
    marker.textContent = isCurrent ? '◉' : '◯';

    const label = document.createElement('span');
    label.className = 'bm-version-label';
    label.textContent = `v${v.version}`;

    const time = document.createElement('span');
    time.className = 'bm-version-time';
    time.textContent = relTime(v.created_at);

    meta.appendChild(marker);
    meta.appendChild(label);
    meta.appendChild(time);
    loz.appendChild(meta);

    // Sparkline
    const sparkWrap = document.createElement('div');
    sparkWrap.className = 'bm-version-sparkline-wrap';
    const canvas = document.createElement('canvas');
    canvas.width = 40;
    canvas.height = 8;
    canvas.style.display = 'block';
    sparkWrap.appendChild(canvas);
    loz.appendChild(sparkWrap);

    // Draw sparkline after append (needs context)
    requestAnimationFrame(() => this._drawSparkline(canvas, idx));

    // Change summary
    if (v.change_summary) {
      const summary = document.createElement('div');
      summary.className = 'bm-version-summary';
      summary.textContent = v.change_summary;
      loz.appendChild(summary);
    }

    // Click / shift-click handler
    loz.addEventListener('click', (e) => {
      const newSel = applySelection(this._versions, idx, e.shiftKey, this._selected);
      this._selected = newSel;
      this._renderSelectionState();
      this._updateButtons();
      this._onSelectionChange(newSel);
      // Preview click (single or first of range)
      this._onVersionClick(this._versions[idx], idx);
    });

    return loz;
  }

  _drawSparkline(canvas, idx) {
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const heights = computeSparklineValues(this._versions);
    const barW = Math.max(1, Math.floor(canvas.width / this._versions.length));
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#58a6ff55';
    heights.forEach((h, i) => {
      ctx.fillRect(i * barW, canvas.height - h, barW - 1, h);
    });
    // Highlight current bar
    if (typeof this._currentVersionNum === 'number') {
      const curIdx = this._versions.findIndex(v => v.version === this._currentVersionNum);
      if (curIdx >= 0) {
        ctx.fillStyle = '#58a6ffb3';
        ctx.fillRect(curIdx * barW, canvas.height - heights[curIdx], barW - 1, heights[curIdx]);
      }
    }
  }

  _renderSelectionState() {
    const lozenges = this._list.querySelectorAll('.bm-version-lozenge');
    lozenges.forEach((loz, i) => {
      loz.classList.toggle('selected', this._selected.includes(i));
    });
  }

  _updateButtons() {
    this._compareBtn.disabled = this._selected.length < 2;
    // Restore available when single non-current version selected
    const canRestore = this._selected.length === 1 &&
      this._versions[this._selected[0]]?.version !== this._currentVersionNum;
    this._restoreBtn.disabled = !canRestore;
  }

  /**
   * Cycle to the next version (] key).
   * @param {number} direction - +1 or -1
   */
  cycleVersion(direction) {
    if (this._versions.length === 0) return;
    const cur = this._selected.length > 0 ? this._selected[0] : 0;
    const next = Math.max(0, Math.min(this._versions.length - 1, cur + direction));
    if (next !== cur) {
      this._selected = [next];
      this._renderSelectionState();
      this._updateButtons();
      this._onSelectionChange(this._selected);
      this._onVersionClick(this._versions[next], next);
    }
  }

  get selection() { return this._selected.slice(); }
  get versions() { return this._versions.slice(); }
}
