/**
 * bookmarks-tab.js — v5.50.1 Bookmarks Tab orchestrator
 *
 * Wires together:
 *   SearchBar + ModeToggle
 *   BookmarkShelf (empty-search landing)
 *   ResultCards   (active search)
 *   PreviewPane   (wiki content)
 *   VersionsRail  (version history)
 *   DiffView      (compare two versions)
 *   ConfirmModal  (restore confirmation)
 *
 * API consumed:
 *   GET  /api/wiki_query?q=&mode=semantic|keyword|slug
 *   GET  /api/wiki/read?slug=
 *   GET  /api/wiki_history?slug=
 *   GET  /api/wiki_read_version?slug=&version=N
 *   GET  /api/wiki_diff?slug=&v1=A&v2=B
 *   POST /api/wiki_restore   {slug, version}
 *   GET  /api/bookmarks
 *   POST /api/bookmarks      {slug}
 *   DELETE /api/bookmarks/{slug}
 *   PUT  /api/bookmarks/{slug}/position  {position}
 *
 * Keyboard:
 *   /        → focus search
 *   j/k      → nav results / shelf spines
 *   Enter    → open preview
 *   Esc      → close preview / diff
 *   Ctrl+B / ⌘B → toggle star on current preview
 *   [ / ]    → cycle versions in rail
 *   Tab      → cycle search modes (handled in SearchBar)
 */

import { SearchBar } from './components/search-bar.js';
import { PreviewPane } from './components/preview-pane.js';
import { VersionsRail } from './components/versions-rail.js';
import { DiffView } from './components/diff-view.js';
import { BookmarkShelf } from './components/bookmark-spine.js';

// ── State ───────────────────────────────────────────────────────────────────

const DAEMON = (typeof window !== 'undefined' && window.DAEMON) ? window.DAEMON : '';

let _bookmarks = [];        // [{slug, label_override, position, added_at}]
let _bookmarkSlugs = new Set(); // for fast O(1) lookup
let _resultCards = [];      // current search results [{slug, title, score, ...}]
let _activeSlug = null;     // slug currently in preview
let _navIdx = -1;           // keyboard nav index into results
let _mode = 'shelf';        // 'shelf' | 'results' | 'preview' | 'diff'

// Component instances
let _searchBar = null;
let _shelf = null;
let _preview = null;
let _versionsRail = null;
let _diffView = null;

// DOM refs
let _resultsPanel = null;
let _previewContainer = null;
let _versionsContainer = null;
let _diffContainer = null;
let _bodyEl = null;

// ── Init ────────────────────────────────────────────────────────────────────

/**
 * Initialize the Bookmarks tab within the given container.
 * Called from index.html <script type="module">.
 * @param {HTMLElement} tabContainer - #tab-bookmarks
 */
export function initBookmarksTab(tabContainer) {
  if (!tabContainer) return;

  tabContainer.innerHTML = '';

  // Inject CSS link if not already present
  if (!document.querySelector('link[href*="bookmarks-tab.css"]')) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = './bookmarks-tab.css';
    document.head.appendChild(link);
  }

  // ── Search bar (sticky top) ────────────────────────────────────────────────
  const searchBarEl = document.createElement('div');
  _searchBar = new SearchBar({
    container: searchBarEl,
    onSearch: _onSearch,
    debounceMs: 200,
  });
  tabContainer.appendChild(searchBarEl);

  // ── Body area ──────────────────────────────────────────────────────────────
  _bodyEl = document.createElement('div');
  _bodyEl.className = 'bm-body';
  tabContainer.appendChild(_bodyEl);

  // ── Shelf (hidden when search active) ─────────────────────────────────────
  const shelfEl = document.createElement('div');
  _shelf = new BookmarkShelf({
    container: shelfEl,
    onSpineClick: _loadPreview,
    onReorder: _onReorder,
  });
  _bodyEl.appendChild(shelfEl);

  // ── Results panel (hidden when shelf visible) ──────────────────────────────
  _resultsPanel = document.createElement('div');
  _resultsPanel.className = 'bm-results';
  _resultsPanel.style.display = 'none';
  _bodyEl.appendChild(_resultsPanel);

  // ── Preview container ──────────────────────────────────────────────────────
  _previewContainer = document.createElement('div');
  _previewContainer.style.display = 'none';
  _previewContainer.style.flex = '1';
  _previewContainer.style.display = 'none';
  _preview = new PreviewPane({
    container: _previewContainer,
    onClose: _closePreview,
    onStarToggle: _onStarToggle,
    onXrefClick: _loadPreview,
  });
  _bodyEl.appendChild(_previewContainer);

  // ── Versions rail ──────────────────────────────────────────────────────────
  _versionsContainer = document.createElement('div');
  _versionsContainer.style.display = 'none';
  _versionsRail = new VersionsRail({
    container: _versionsContainer,
    onVersionClick: _onVersionClick,
    onSelectionChange: _onVersionSelectionChange,
    onCompare: _onCompare,
    onRestore: _onRestoreRequest,
  });
  _bodyEl.appendChild(_versionsContainer);

  // ── Diff view ──────────────────────────────────────────────────────────────
  _diffContainer = document.createElement('div');
  _diffContainer.style.display = 'none';
  _diffView = new DiffView({
    container: _diffContainer,
    onClose: _closeDiff,
  });
  _bodyEl.appendChild(_diffContainer);

  // ── Keyboard handler ───────────────────────────────────────────────────────
  document.addEventListener('keydown', _onKeyDown);

  // ── Initial load ───────────────────────────────────────────────────────────
  _setMode('shelf');
  _fetchBookmarks();
}

// ── Mode management ─────────────────────────────────────────────────────────

function _setMode(m) {
  _mode = m;

  // Visibility rules:
  //   shelf:   shelf visible, results hidden, preview hidden, diff hidden
  //   results: shelf hidden, results visible, preview hidden, diff hidden
  //   preview: shelf hidden, results visible, preview+versions visible, diff hidden
  //   diff:    shelf hidden, results visible, preview hidden, diff visible

  const shelfEl = _shelf && _shelf._container;
  _show(shelfEl, m === 'shelf');
  _show(_resultsPanel, m === 'results' || m === 'preview' || m === 'diff');
  _show(_previewContainer, m === 'preview');
  _show(_versionsContainer, m === 'preview');
  _show(_diffContainer, m === 'diff');
}

function _show(el, visible) {
  if (!el) return;
  el.style.display = visible ? '' : 'none';
}

// ── Search ───────────────────────────────────────────────────────────────────

async function _onSearch(query, mode) {
  if (!query.trim()) {
    // Empty search: return to shelf
    _resultCards = [];
    _navIdx = -1;
    _searchBar && _searchBar.setCount(null);
    _setMode('shelf');
    return;
  }

  _renderResultsLoading();
  _setMode('results');

  try {
    const params = new URLSearchParams({ q: query.trim(), mode });
    const resp = await fetch(`${DAEMON}/api/wiki_query?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const results = await resp.json();
    _resultCards = Array.isArray(results) ? results : [];
  } catch (err) {
    _resultCards = [];
    _renderResultsError(String(err));
    return;
  }

  _navIdx = -1;
  _searchBar && _searchBar.setCount(_resultCards.length);
  _renderResultCards(_resultCards);
}

// ── Result rendering ────────────────────────────────────────────────────────

function _renderResultsLoading() {
  while (_resultsPanel.firstChild) _resultsPanel.removeChild(_resultsPanel.firstChild);
  const hdr = _makeResultsHeader();
  _resultsPanel.appendChild(hdr);
  const el = document.createElement('div');
  el.className = 'bm-loading';
  el.textContent = 'Searching…';
  _resultsPanel.appendChild(el);
}

function _renderResultsError(msg) {
  while (_resultsPanel.firstChild) _resultsPanel.removeChild(_resultsPanel.firstChild);
  _resultsPanel.appendChild(_makeResultsHeader());
  const el = document.createElement('div');
  el.className = 'bm-error';
  el.textContent = msg;
  _resultsPanel.appendChild(el);
}

function _makeResultsHeader() {
  const hdr = document.createElement('div');
  hdr.className = 'bm-results-header';
  hdr.textContent = 'RESULTS';
  return hdr;
}

function _renderResultCards(results) {
  while (_resultsPanel.firstChild) _resultsPanel.removeChild(_resultsPanel.firstChild);
  _resultsPanel.appendChild(_makeResultsHeader());

  if (results.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'bm-loading';
    empty.textContent = 'No results.';
    _resultsPanel.appendChild(empty);
    return;
  }

  results.forEach((r, idx) => {
    const card = _buildResultCard(r, idx);
    _resultsPanel.appendChild(card);
  });
}

function _buildResultCard(r, idx) {
  const card = document.createElement('div');
  card.className = 'bm-result-card';
  card.dataset.idx = String(idx);

  const title = document.createElement('div');
  title.className = 'bm-result-title';
  title.textContent = r.title || r.slug;

  const slug = document.createElement('div');
  slug.className = 'bm-result-slug';
  slug.textContent = r.slug;

  // Tags
  const tags = document.createElement('div');
  tags.className = 'bm-result-tags';
  if (Array.isArray(r.tags)) {
    r.tags.slice(0, 4).forEach(t => {
      const chip = document.createElement('span');
      chip.className = 'bm-result-tag';
      chip.textContent = t;
      tags.appendChild(chip);
    });
  }

  // Snippet (2-line clamp via CSS)
  const snippet = document.createElement('div');
  snippet.className = 'bm-result-snippet';
  const rawContent = typeof r.content === 'string' ? r.content : '';
  // Strip markdown: headings, table rows (|...|), separator lines (|---|), bold/italic, code
  const plainText = rawContent
    .split('\n')
    .filter(line => !/^\s*\|/.test(line))  // drop table rows and separator lines
    .join('\n')
    .replace(/#+\s*/g, '')          // headings
    .replace(/\*\*(.+?)\*\*/g, '$1') // bold
    .replace(/\*(.+?)\*/g, '$1')     // italic
    .replace(/`(.+?)`/g, '$1')       // inline code
    .replace(/\n{2,}/g, ' ')         // collapse blank lines
    .replace(/\s+/g, ' ')
    .trim();
  snippet.textContent = plainText.slice(0, 200);

  card.appendChild(title);
  card.appendChild(slug);
  card.appendChild(tags);
  card.appendChild(snippet);

  // Score chip (floated right via CSS)
  if (r.score != null) {
    const score = document.createElement('div');
    score.className = 'bm-result-score';
    score.textContent = Number(r.score).toFixed(2);
    card.appendChild(score);
  }

  card.addEventListener('click', () => {
    _navIdx = idx;
    _updateNavHighlight();
    _loadPreview(r.slug);
  });

  return card;
}

function _updateNavHighlight() {
  const cards = _resultsPanel.querySelectorAll('.bm-result-card');
  cards.forEach((c, i) => {
    c.classList.toggle('bm-nav-active', i === _navIdx);
    c.classList.toggle('active', i === _navIdx);
  });
}

// ── Preview loading ──────────────────────────────────────────────────────────

async function _loadPreview(slug) {
  if (!slug) return;
  _activeSlug = slug;

  // Show results + preview layout
  if (_mode === 'shelf' || _mode === 'results' || _mode === 'diff') {
    _setMode('preview');
  } else {
    _setMode('preview');
  }

  _preview.showLoading();

  try {
    const resp = await fetch(`${DAEMON}/api/wiki/read?slug=${encodeURIComponent(slug)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const page = await resp.json();
    if (page.error) throw new Error(page.error);

    const isStarred = _bookmarkSlugs.has(slug);
    _preview.show(page, isStarred);

    // Load version history alongside
    _loadHistory(slug);
  } catch (err) {
    _preview.showError(`Failed to load: ${err.message}`);
  }
}

async function _loadHistory(slug) {
  try {
    const resp = await fetch(`${DAEMON}/api/wiki_history?slug=${encodeURIComponent(slug)}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.error || !Array.isArray(data.versions)) return;
    const currentVersion = data.versions.length > 0 ? data.versions[0].version : 1;
    _versionsRail.setVersions(data.versions, currentVersion);
  } catch (_) { /* non-fatal */ }
}

// ── Version interaction ──────────────────────────────────────────────────────

async function _onVersionClick(version) {
  if (!_activeSlug) return;

  try {
    const params = new URLSearchParams({
      slug: _activeSlug,
      version: String(version.version),
    });
    const resp = await fetch(`${DAEMON}/api/wiki_read_version?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    const isStarred = _bookmarkSlugs.has(_activeSlug);
    _preview.show({
      slug: _activeSlug,
      title: data.title || _activeSlug,
      content: data.content || '',
    }, isStarred);
  } catch (err) {
    _preview.showError(`Failed to load v${version.version}: ${err.message}`);
  }
}

function _onVersionSelectionChange(_selectedIndices) {
  // Rail manages button state; nothing extra needed here
}

async function _onCompare(selectedIndices, versions) {
  if (selectedIndices.length < 2 || !_activeSlug) return;

  // Use first two selected; sort by version number ascending
  const sel = selectedIndices
    .slice(0, 2)
    .map(i => versions[i])
    .sort((a, b) => a.version - b.version);
  const v1 = sel[0].version;
  const v2 = sel[1].version;

  _setMode('diff');

  try {
    const params = new URLSearchParams({
      slug: _activeSlug,
      v1: String(v1),
      v2: String(v2),
    });
    const resp = await fetch(`${DAEMON}/api/wiki_diff?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    _diffView.show(data, versions);
  } catch (err) {
    _diffView.clear();
    // Revert to preview on error
    _setMode('preview');
  }
}

function _closeDiff() {
  _setMode('preview');
}

// ── Restore ──────────────────────────────────────────────────────────────────

function _onRestoreRequest(version) {
  if (!_activeSlug) return;
  _showConfirmModal(version);
}

function _showConfirmModal(version) {
  const backdrop = document.createElement('div');
  backdrop.className = 'bm-modal-backdrop';

  const modal = document.createElement('div');
  modal.className = 'bm-modal';

  const title = document.createElement('div');
  title.className = 'bm-modal-title';
  title.textContent = `Restore v${version.version}?`;

  const body = document.createElement('div');
  body.className = 'bm-modal-body';
  body.textContent = `Restore v${version.version} as new v${(version.version + 1)}? Intervening versions are preserved.`;

  const actions = document.createElement('div');
  actions.className = 'bm-modal-actions';

  const cancelBtn = document.createElement('button');
  cancelBtn.className = 'bm-modal-btn';
  cancelBtn.textContent = 'Cancel';
  cancelBtn.addEventListener('click', () => document.body.removeChild(backdrop));

  const confirmBtn = document.createElement('button');
  confirmBtn.className = 'bm-modal-btn confirm';
  confirmBtn.textContent = 'Restore';
  confirmBtn.addEventListener('click', async () => {
    document.body.removeChild(backdrop);
    await _doRestore(version.version);
  });

  // Also close on backdrop click
  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) document.body.removeChild(backdrop);
  });

  actions.appendChild(cancelBtn);
  actions.appendChild(confirmBtn);
  modal.appendChild(title);
  modal.appendChild(body);
  modal.appendChild(actions);
  backdrop.appendChild(modal);
  document.body.appendChild(backdrop);

  confirmBtn.focus();
}

async function _doRestore(versionNum) {
  if (!_activeSlug) return;
  try {
    const resp = await fetch(`${DAEMON}/api/wiki_restore`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug: _activeSlug, version: versionNum }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    // Reload the preview with the latest (restored) content
    await _loadPreview(_activeSlug);
  } catch (err) {
    console.error('Restore failed:', err);
  }
}

// ── Star / bookmark ──────────────────────────────────────────────────────────

async function _onStarToggle(slug, newStarred) {
  try {
    if (newStarred) {
      await fetch(`${DAEMON}/api/bookmarks`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ slug }),
      });
      _bookmarkSlugs.add(slug);
    } else {
      await fetch(`${DAEMON}/api/bookmarks/${encodeURIComponent(slug)}`, {
        method: 'DELETE',
      });
      _bookmarkSlugs.delete(slug);
    }
    await _fetchBookmarks();
  } catch (err) {
    console.error('Star toggle failed:', err);
    // Revert UI
    _preview.setStarred(!newStarred);
  }
}

// ── Bookmarks CRUD ───────────────────────────────────────────────────────────

async function _fetchBookmarks() {
  try {
    const resp = await fetch(`${DAEMON}/api/bookmarks`);
    if (!resp.ok) return;
    _bookmarks = await resp.json();
    _bookmarkSlugs = new Set(_bookmarks.map(b => b.slug));
    _shelf.setBookmarks(_bookmarks);
    // Update star state in preview if open
    if (_activeSlug && _mode === 'preview') {
      _preview.setStarred(_bookmarkSlugs.has(_activeSlug));
    }
  } catch (_) { /* non-fatal */ }
}

async function _onReorder(newSlugOrder) {
  // Send updated positions to API
  for (let i = 0; i < newSlugOrder.length; i++) {
    const slug = newSlugOrder[i];
    fetch(`${DAEMON}/api/bookmarks/${encodeURIComponent(slug)}/position`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ position: i }),
    }).catch(() => {});
  }
  _bookmarks = _bookmarks
    .slice()
    .sort((a, b) => newSlugOrder.indexOf(a.slug) - newSlugOrder.indexOf(b.slug));
  _bookmarkSlugs = new Set(_bookmarks.map(b => b.slug));
}

// ── Close ────────────────────────────────────────────────────────────────────

function _closePreview() {
  _activeSlug = null;
  if (_searchBar && _searchBar.query) {
    _setMode('results');
  } else {
    _setMode('shelf');
  }
}

// ── Keyboard ─────────────────────────────────────────────────────────────────

function _onKeyDown(e) {
  // Only handle when bookmarks tab is active
  const tab = document.getElementById('tab-bookmarks');
  if (!tab || !tab.classList.contains('active')) return;

  const target = e.target;
  const isInput = target.tagName === 'INPUT' || target.tagName === 'TEXTAREA';

  // / → focus search
  if (e.key === '/' && !isInput) {
    e.preventDefault();
    _searchBar && _searchBar.focus();
    return;
  }

  // Esc → close diff or preview
  if (e.key === 'Escape') {
    if (_mode === 'diff') { _closeDiff(); return; }
    if (_mode === 'preview') { _closePreview(); return; }
    return;
  }

  // Ctrl+B / ⌘B → toggle star
  if (e.key === 'b' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    if (_activeSlug && _mode === 'preview') {
      const next = !_bookmarkSlugs.has(_activeSlug);
      _preview.setStarred(next);
      _onStarToggle(_activeSlug, next);
    }
    return;
  }

  if (!isInput) {
    // j/k navigation
    if (e.key === 'j') {
      e.preventDefault();
      if (_mode === 'shelf') {
        _shelf && _shelf.navigate(1);
      } else if (_mode === 'results' || _mode === 'preview') {
        _navIdx = Math.min(_resultCards.length - 1, _navIdx + 1);
        _updateNavHighlight();
      }
      return;
    }
    if (e.key === 'k') {
      e.preventDefault();
      if (_mode === 'shelf') {
        _shelf && _shelf.navigate(-1);
      } else if (_mode === 'results' || _mode === 'preview') {
        _navIdx = Math.max(0, _navIdx - 1);
        _updateNavHighlight();
      }
      return;
    }
    // Enter → open preview for nav-active result
    if (e.key === 'Enter') {
      if (_mode === 'shelf') {
        _shelf && _shelf.activateNav();
      } else if (_mode === 'results' && _navIdx >= 0 && _resultCards[_navIdx]) {
        _loadPreview(_resultCards[_navIdx].slug);
      }
      return;
    }
    // [ / ] → cycle versions
    if (e.key === ']' && _mode === 'preview') {
      _versionsRail && _versionsRail.cycleVersion(1);
      return;
    }
    if (e.key === '[' && _mode === 'preview') {
      _versionsRail && _versionsRail.cycleVersion(-1);
      return;
    }
  }
}
