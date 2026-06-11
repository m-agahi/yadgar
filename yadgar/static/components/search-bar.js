/**
 * search-bar.js — v5.50.1 Bookmarks Tab SearchBar component
 *
 * Renders a sticky search bar with mode toggle (semantic / keyword / slug).
 * Exports pure logic functions for testability.
 *
 * Exported for tests:
 *   buildQueryParams(query, mode) → URLSearchParams-compatible object
 *   getModeFromStorage()          → string ('semantic'|'keyword'|'slug')
 *   setModeToStorage(mode)        → void
 */

'use strict';

/** @typedef {'semantic'|'keyword'|'slug'} SearchMode */

const MODES = /** @type {SearchMode[]} */ (['semantic', 'keyword', 'slug']);
const STORAGE_KEY = 'yadgar.bm.searchMode';

// ── Pure logic (exported for tests) ────────────────────────────────────────

/**
 * Build query params object for the search API.
 * @param {string} query
 * @param {SearchMode} mode
 * @returns {{ q: string, mode: string }}
 */
export function buildQueryParams(query, mode) {
  return { q: query.trim(), mode };
}

/**
 * Read persisted search mode from localStorage.
 * Falls back to 'semantic' on any error.
 * @returns {SearchMode}
 */
export function getModeFromStorage() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && MODES.includes(/** @type {SearchMode} */ (stored))) {
      return /** @type {SearchMode} */ (stored);
    }
  } catch (_) { /* localStorage unavailable */ }
  return 'semantic';
}

/**
 * Persist search mode to localStorage.
 * @param {SearchMode} mode
 */
export function setModeToStorage(mode) {
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch (_) { /* localStorage unavailable */ }
}

/**
 * Cycle to the next search mode.
 * @param {SearchMode} current
 * @returns {SearchMode}
 */
export function cycleMode(current) {
  const idx = MODES.indexOf(current);
  return MODES[(idx + 1) % MODES.length];
}

// ── Component ───────────────────────────────────────────────────────────────

export class SearchBar {
  /**
   * @param {Object} opts
   * @param {HTMLElement} opts.container - DOM element to render into
   * @param {function(string, SearchMode): void} opts.onSearch - called on input change (debounced)
   * @param {number} [opts.debounceMs=200]
   */
  constructor({ container, onSearch, debounceMs = 200 }) {
    this._container = container;
    this._onSearch = onSearch;
    this._debounceMs = debounceMs;
    this._mode = getModeFromStorage();
    this._timer = null;
    this._query = '';
    this._render();
  }

  _render() {
    this._container.innerHTML = '';
    this._container.className = 'bm-search-bar';

    const caret = document.createElement('span');
    caret.className = 'bm-search-caret';
    caret.textContent = '╱';
    this._container.appendChild(caret);

    this._input = document.createElement('input');
    this._input.type = 'text';
    this._input.className = 'bm-search-input';
    this._input.placeholder = 'search wiki…';
    this._input.setAttribute('aria-label', 'Search wiki');
    this._container.appendChild(this._input);

    this._countEl = document.createElement('span');
    this._countEl.className = 'bm-search-count';
    this._container.appendChild(this._countEl);

    this._modeToggle = document.createElement('div');
    this._modeToggle.className = 'bm-mode-toggle';
    this._modeToggle.setAttribute('role', 'group');
    this._modeToggle.setAttribute('aria-label', 'Search mode');

    for (const m of MODES) {
      const chip = document.createElement('button');
      chip.className = 'bm-mode-chip' + (m === this._mode ? ' active' : '');
      chip.textContent = m;
      chip.dataset.mode = m;
      chip.type = 'button';
      chip.addEventListener('click', () => this._setMode(m));
      this._modeToggle.appendChild(chip);
    }
    this._container.appendChild(this._modeToggle);

    this._input.addEventListener('input', () => this._onInput());

    // Keyboard: Tab cycles mode while input focused
    this._input.addEventListener('keydown', (e) => {
      if (e.key === 'Tab') {
        e.preventDefault();
        this._setMode(cycleMode(this._mode));
      }
    });
  }

  _onInput() {
    this._query = this._input.value;
    clearTimeout(this._timer);
    this._timer = setTimeout(() => {
      this._onSearch(this._query, this._mode);
    }, this._debounceMs);
  }

  _setMode(mode) {
    this._mode = mode;
    setModeToStorage(mode);
    // Update chip active states
    this._modeToggle.querySelectorAll('.bm-mode-chip').forEach(chip => {
      chip.classList.toggle('active', chip.dataset.mode === mode);
    });
    // Re-fire search with new mode if query present
    if (this._query) {
      clearTimeout(this._timer);
      this._onSearch(this._query, this._mode);
    }
  }

  /** Focus the search input. */
  focus() {
    this._input && this._input.focus();
  }

  /** Update result count badge. */
  setCount(n) {
    if (this._countEl) {
      this._countEl.textContent = n != null ? `${n} result${n === 1 ? '' : 's'}` : '';
    }
  }

  /** Clear the input and reset count. */
  clear() {
    if (this._input) this._input.value = '';
    this._query = '';
    this.setCount(null);
  }

  get mode() { return this._mode; }
  get query() { return this._query; }
}
