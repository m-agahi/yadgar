/**
 * diff-view.js — v5.50.1 Bookmarks Tab DiffView component
 *
 * Split-pane synced scroll showing two wiki versions side by side.
 * Add lines use --diff-add, del lines use --diff-del, context uses --diff-ctx.
 *
 * Exported for tests:
 *   classifyDiffLines(diffText) → {left: LineEntry[], right: LineEntry[]}
 *   LineEntry: {text: string, type: 'add'|'del'|'ctx'|'hdr'}
 */

'use strict';

// ── Pure logic (exported for tests) ────────────────────────────────────────

/**
 * @typedef {{ text: string, type: 'add'|'del'|'ctx'|'hdr' }} LineEntry
 */

/**
 * Parse a unified diff string into left (old) and right (new) line arrays.
 *
 * Left pane shows: context + deleted lines (del) + hunk headers.
 * Right pane shows: context + added lines (add) + hunk headers.
 *
 * @param {string} diffText - unified diff text (--- / +++ / @@ ... lines)
 * @returns {{ left: LineEntry[], right: LineEntry[] }}
 */
export function classifyDiffLines(diffText) {
  if (!diffText || typeof diffText !== 'string') {
    return { left: [], right: [] };
  }

  const left = /** @type {LineEntry[]} */ ([]);
  const right = /** @type {LineEntry[]} */ ([]);

  for (const line of diffText.split('\n')) {
    if (line.startsWith('---') || line.startsWith('+++')) {
      // File header lines: show on both sides as header
      left.push({ text: line, type: 'hdr' });
      right.push({ text: line, type: 'hdr' });
    } else if (line.startsWith('@@')) {
      // Hunk header: show on both sides
      left.push({ text: line, type: 'hdr' });
      right.push({ text: line, type: 'hdr' });
    } else if (line.startsWith('-')) {
      // Deleted line: left only (old side), add placeholder on right
      left.push({ text: line, type: 'del' });
      right.push({ text: '', type: 'ctx' });
    } else if (line.startsWith('+')) {
      // Added line: right only (new side), placeholder on left
      left.push({ text: '', type: 'ctx' });
      right.push({ text: line, type: 'add' });
    } else {
      // Context line: both sides
      left.push({ text: line, type: 'ctx' });
      right.push({ text: line, type: 'ctx' });
    }
  }

  return { left, right };
}

// ── Component ───────────────────────────────────────────────────────────────

export class DiffView {
  /**
   * @param {Object} opts
   * @param {HTMLElement} opts.container
   * @param {function(): void} opts.onClose - called when diff view is dismissed
   */
  constructor({ container, onClose }) {
    this._container = container;
    this._onClose = onClose;
    this._syncingScroll = false;
    this._build();
  }

  _build() {
    this._container.className = 'bm-diff';

    // Header row with version labels
    this._headers = document.createElement('div');
    this._headers.className = 'bm-diff-headers';

    this._leftHeader = document.createElement('div');
    this._leftHeader.className = 'bm-diff-col-header';

    this._rightHeader = document.createElement('div');
    this._rightHeader.className = 'bm-diff-col-header';

    this._headers.appendChild(this._leftHeader);
    this._headers.appendChild(this._rightHeader);

    // Body: two synced-scroll panes
    this._body = document.createElement('div');
    this._body.className = 'bm-diff-body';

    this._leftPane = document.createElement('div');
    this._leftPane.className = 'bm-diff-pane';

    this._rightPane = document.createElement('div');
    this._rightPane.className = 'bm-diff-pane';

    // Synced scroll
    this._leftPane.addEventListener('scroll', () => this._syncScroll(this._leftPane, this._rightPane));
    this._rightPane.addEventListener('scroll', () => this._syncScroll(this._rightPane, this._leftPane));

    this._body.appendChild(this._leftPane);
    this._body.appendChild(this._rightPane);

    this._container.appendChild(this._headers);
    this._container.appendChild(this._body);
  }

  _syncScroll(source, target) {
    if (this._syncingScroll) return;
    this._syncingScroll = true;
    target.scrollTop = source.scrollTop;
    target.scrollLeft = source.scrollLeft;
    this._syncingScroll = false;
  }

  /**
   * Render a diff from API response.
   *
   * @param {object} diffData - {diff: string, v1: number, v2: number, slug: string}
   * @param {object[]} versions - full versions list (for timestamps)
   */
  show(diffData, versions) {
    const v1 = diffData.v1;
    const v2 = diffData.v2;

    // Find timestamps for version labels
    const findVer = (n) => versions.find(v => v.version === n);
    const v1meta = findVer(v1);
    const v2meta = findVer(v2);
    const v1label = `v${v1}${v1meta ? ' · ' + _relTimeShort(v1meta.created_at) : ''}`;
    const v2label = `v${v2}${v2meta ? ' · ' + _relTimeShort(v2meta.created_at) : ''}`;

    this._leftHeader.textContent = v1label;
    this._rightHeader.textContent = v2label;

    const { left, right } = classifyDiffLines(diffData.diff || '');

    this._renderPane(this._leftPane, left);
    this._renderPane(this._rightPane, right);
  }

  _renderPane(pane, lines) {
    // Clear using DOM method (no untrusted content)
    while (pane.firstChild) pane.removeChild(pane.firstChild);

    for (const entry of lines) {
      const div = document.createElement('div');
      div.className = 'bm-diff-line ' + entry.type;
      div.textContent = entry.text; // textContent — safe, no HTML injection
      pane.appendChild(div);
    }
  }

  /** Clear the diff panes. */
  clear() {
    while (this._leftPane.firstChild) this._leftPane.removeChild(this._leftPane.firstChild);
    while (this._rightPane.firstChild) this._rightPane.removeChild(this._rightPane.firstChild);
    this._leftHeader.textContent = '';
    this._rightHeader.textContent = '';
  }
}

// ── Internal helpers ────────────────────────────────────────────────────────

function _relTimeShort(iso) {
  if (!iso) return '';
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}
