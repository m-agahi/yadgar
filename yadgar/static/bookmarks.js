/* yadgar bookmarks page JS — v5.24.2
 *
 * Consumes:
 *   GET  /api/bookmarks                    → [{slug, label_override, position, added_at}]
 *   POST /api/bookmarks                    ← {slug, label_override?}
 *   DELETE /api/bookmarks/{slug}
 *   PUT  /api/bookmarks/{slug}/position    ← {position}
 *   GET  /api/wiki/search?q=<q>[&limit=N]  → [{slug, title, score, ...}]
 *   GET  /api/wiki/list[?slug_prefix=<p>]  → [{slug, title, category}]
 *   GET  /api/wiki/read?slug=<slug>        → {slug, title, content, ...}
 *   GET  /api/stats                        → {queue_depth?, ...}  (for queue badge)
 *
 * Libraries (vendored in lib/):
 *   marked.min.js    — markdown → HTML
 *   highlight.min.js — syntax highlighting
 *   dompurify.min.js — XSS sanitization
 */

/* ---------------------------------------------------------------------------
 * State
 * ----------------------------------------------------------------------- */
let _bookmarks = [];          // ordered list of bookmark objects
let _selectedSlug = null;     // currently displayed bookmark slug
let _fetchedAt = {};          // slug → Date of last fetch
let _dragSrcIdx = null;       // drag-and-drop source index
let _queuePollTimer = null;   // interval id for queue-depth badge

/* ---------------------------------------------------------------------------
 * Marked renderer: wire highlight.js + custom [[slug]] links
 * ----------------------------------------------------------------------- */
function _configureMarked() {
  if (typeof marked === 'undefined') return;

  // Syntax highlighting via highlight.js
  if (typeof hljs !== 'undefined') {
    marked.setOptions({
      highlight: (code, lang) => {
        if (lang && hljs.getLanguage(lang)) {
          return hljs.highlight(code, { language: lang }).value;
        }
        return hljs.highlightAuto(code).value;
      },
    });
  }

  // Custom renderer: [[slug]] → clickable links that navigate bookmarks
  // marked v15 passes a token object to renderer.text, not a raw string.
  // v5.24.1 extracted token.text correctly but then called _origText(replaced),
  // which is v15's default text renderer and expects a token object — it does
  // `'tokens' in arg` on the returned string and throws.  Fix: return the
  // replaced HTML string directly; DOMPurify downstream handles XSS.
  const renderer = new marked.Renderer();
  renderer.text = (token) => {
    // marked v15: token is an object {type:"text", text:"...", tokens:[...]}
    // marked v14 and below: token is already the string.
    const raw = (typeof token === "object" && token !== null && typeof token.text === "string")
      ? token.text
      : (typeof token === "string" ? token : "");
    // Replace [[slug]] patterns with anchor that calls selectBookmark(slug)
    return raw.replace(
      /\[\[([^\]]+)\]\]/g,
      (_, slug) =>
        `<a href="#" class="wiki-xref" onclick="selectBookmarkBySlug('${slug.replace(/'/g, "\\'")}');return false;">${slug}</a>`
    );
  };
  marked.setOptions({ renderer });
}

/* ---------------------------------------------------------------------------
 * Utilities
 * ----------------------------------------------------------------------- */
function _relTime(d) {
  if (!d) return '';
  const secs = Math.floor((Date.now() - d) / 1000);
  if (secs < 60)   return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  return `${Math.floor(secs / 3600)}h ago`;
}

function _debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/* ---------------------------------------------------------------------------
 * Render markdown
 * ----------------------------------------------------------------------- */
function _renderMarkdown(content) {
  // v5.24.1: guard — if API returns non-string (null, object, etc.) fall back
  // to empty string rather than passing a non-string to marked.parse().
  if (typeof content !== "string") {
    console.warn("_renderMarkdown: expected string, got", typeof content, content);
    content = "";
  }
  if (typeof marked === 'undefined') {
    // Fallback: plain text in <pre>
    const pre = document.createElement('pre');
    pre.textContent = content;
    return pre.outerHTML;
  }
  let html = marked.parse(content);
  if (typeof DOMPurify !== 'undefined') {
    html = DOMPurify.sanitize(html, {
      ADD_ATTR: ['onclick'],  // allow [[slug]] onclick links
    });
  }
  return html;
}

/* ---------------------------------------------------------------------------
 * API helpers
 * ----------------------------------------------------------------------- */
async function _apiFetch(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  return resp;
}

/* ---------------------------------------------------------------------------
 * Bookmark list load + render
 * ----------------------------------------------------------------------- */
async function loadBookmarks() {
  try {
    const resp = await _apiFetch('/api/bookmarks');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    _bookmarks = await resp.json();
  } catch (e) {
    console.warn('loadBookmarks error:', e);
    _bookmarks = [];
  }
  renderSidebar();
}

function renderSidebar() {
  const list = document.getElementById('bookmark-list');
  const hint = document.getElementById('empty-hint');
  if (!list) return;

  list.innerHTML = '';

  const countEl = document.getElementById('bm-count');
  if (countEl) countEl.textContent = _bookmarks.length ? `${_bookmarks.length}` : '';

  if (_bookmarks.length === 0) {
    if (hint) hint.style.display = 'block';
    return;
  }
  if (hint) hint.style.display = 'none';

  _bookmarks.forEach((bm, idx) => {
    const row = document.createElement('div');
    row.className = 'bm-row' + (bm.slug === _selectedSlug ? ' selected' : '');
    row.dataset.idx = idx;
    row.draggable = true;

    const handle = document.createElement('span');
    handle.className = 'bm-drag-handle';
    handle.textContent = '⠿';
    handle.title = 'Drag to reorder';

    const label = document.createElement('span');
    label.className = 'bm-label';
    label.title = bm.slug;

    const labelInner = document.createElement('span');
    labelInner.textContent = bm.label_override || bm.slug;
    label.appendChild(labelInner);

    const fetched = document.createElement('span');
    fetched.className = 'bm-fetched';
    const when = _fetchedAt[bm.slug];
    fetched.textContent = when ? _relTime(when) : '';
    label.appendChild(fetched);

    const slugSpan = document.createElement('span');
    slugSpan.className = 'bm-slug';
    slugSpan.textContent = bm.label_override ? bm.slug : '';

    const refreshBtn = document.createElement('button');
    refreshBtn.className = 'bm-refresh';
    refreshBtn.textContent = '↺';
    refreshBtn.title = 'Refresh (r)';
    refreshBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      loadWikiContent(bm.slug);
    });

    const removeBtn = document.createElement('button');
    removeBtn.className = 'bm-remove';
    removeBtn.textContent = '✕';
    removeBtn.title = 'Remove bookmark';
    removeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      removeBookmark(bm.slug);
    });

    row.appendChild(handle);
    row.appendChild(label);
    row.appendChild(slugSpan);
    row.appendChild(refreshBtn);
    row.appendChild(removeBtn);

    // Click → select
    row.addEventListener('click', () => selectBookmark(idx));

    // Keyboard: 'r' to refresh focused row
    row.tabIndex = 0;
    row.addEventListener('keydown', (e) => {
      if (e.key === 'r') { e.preventDefault(); loadWikiContent(bm.slug); }
      if (e.key === 'Enter') selectBookmark(idx);
    });

    // Drag-and-drop
    row.addEventListener('dragstart', (e) => { _dragSrcIdx = idx; row.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; });
    row.addEventListener('dragend', () => { row.classList.remove('dragging'); _clearDragOver(); });
    row.addEventListener('dragover', (e) => { e.preventDefault(); row.classList.add('drag-over'); });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', (e) => { e.preventDefault(); row.classList.remove('drag-over'); _onDrop(idx); });

    list.appendChild(row);
  });

  // Refresh fetched-at labels every 30s
  clearTimeout(_relTimeTimer);
  _relTimeTimer = setTimeout(_refreshRelTimes, 30_000);
}

let _relTimeTimer = null;
function _refreshRelTimes() {
  const rows = document.querySelectorAll('.bm-fetched');
  rows.forEach((el, i) => {
    if (i < _bookmarks.length) {
      const when = _fetchedAt[_bookmarks[i].slug];
      el.textContent = when ? _relTime(when) : '';
    }
  });
  _relTimeTimer = setTimeout(_refreshRelTimes, 30_000);
}

/* ---------------------------------------------------------------------------
 * Select bookmark + load wiki content
 * ----------------------------------------------------------------------- */
function selectBookmark(idx) {
  const bm = _bookmarks[idx];
  if (!bm) return;
  _selectedSlug = bm.slug;
  renderSidebar();
  loadWikiContent(bm.slug);
}

function selectBookmarkBySlug(slug) {
  const idx = _bookmarks.findIndex(b => b.slug === slug);
  if (idx >= 0) {
    selectBookmark(idx);
  } else {
    // Not bookmarked — load anyway as a read-only preview
    _selectedSlug = slug;
    loadWikiContent(slug);
  }
}

async function loadWikiContent(slug) {
  const spinner = document.getElementById('spinner');
  const mdEl = document.getElementById('md-render');
  const errEl = document.getElementById('pane-error');
  const titleEl = document.getElementById('pane-title');
  const slugEl = document.getElementById('pane-slug');

  if (spinner) spinner.classList.add('visible');
  if (mdEl) mdEl.style.display = 'none';
  if (errEl) errEl.classList.remove('visible');

  try {
    const resp = await _apiFetch(`/api/wiki/read?slug=${encodeURIComponent(slug)}`);
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.error || `HTTP ${resp.status}`);
    }
    const page = await resp.json();
    _fetchedAt[slug] = new Date();

    if (titleEl) titleEl.textContent = page.title || slug;
    if (slugEl) slugEl.textContent = slug;

    const html = _renderMarkdown(page.content || '_No content._');
    if (mdEl) {
      mdEl.innerHTML = html;
      mdEl.style.display = 'block';
      // Run hljs on any un-highlighted code blocks
      if (typeof hljs !== 'undefined') {
        mdEl.querySelectorAll('pre code:not(.hljs)').forEach(el => hljs.highlightElement(el));
      }
    }
    // Update fetched-at in sidebar
    renderSidebar();
  } catch (e) {
    if (errEl) {
      errEl.textContent = `Error loading "${slug}": ${e.message}`;
      errEl.classList.add('visible');
    }
    if (titleEl) titleEl.textContent = slug;
  } finally {
    if (spinner) spinner.classList.remove('visible');
  }
}

/* ---------------------------------------------------------------------------
 * Remove bookmark
 * ----------------------------------------------------------------------- */
async function removeBookmark(slug) {
  try {
    await _apiFetch(`/api/bookmarks/${encodeURIComponent(slug)}`, { method: 'DELETE' });
  } catch (e) {
    console.warn('removeBookmark error:', e);
  }
  if (_selectedSlug === slug) {
    _selectedSlug = null;
    const paneTitle = document.getElementById('pane-title');
    const mdEl = document.getElementById('md-render');
    if (paneTitle) paneTitle.textContent = 'Select a bookmark';
    if (mdEl) mdEl.innerHTML = '';
  }
  await loadBookmarks();
}

/* ---------------------------------------------------------------------------
 * Drag-and-drop reorder
 * ----------------------------------------------------------------------- */
function _clearDragOver() {
  document.querySelectorAll('.bm-row.drag-over').forEach(el => el.classList.remove('drag-over'));
}

async function _onDrop(targetIdx) {
  if (_dragSrcIdx === null || _dragSrcIdx === targetIdx) { _dragSrcIdx = null; return; }
  const slug = _bookmarks[_dragSrcIdx]?.slug;
  if (!slug) { _dragSrcIdx = null; return; }
  _dragSrcIdx = null;

  // Optimistic reorder in _bookmarks
  const moved = _bookmarks.splice(
    _bookmarks.findIndex(b => b.slug === slug), 1
  )[0];
  _bookmarks.splice(targetIdx, 0, moved);
  renderSidebar();

  // Persist
  try {
    await _apiFetch(`/api/bookmarks/${encodeURIComponent(slug)}/position`, {
      method: 'PUT',
      body: JSON.stringify({ position: targetIdx }),
    });
  } catch (e) {
    console.warn('reorder error:', e);
  }
  await loadBookmarks();  // reconcile with server
}

/* ---------------------------------------------------------------------------
 * Global refresh (re-fetch bookmark list)
 * ----------------------------------------------------------------------- */
async function globalRefresh() {
  await loadBookmarks();
  if (_selectedSlug) await loadWikiContent(_selectedSlug);
}

/* ---------------------------------------------------------------------------
 * Queue-depth badge
 * ----------------------------------------------------------------------- */
async function _pollQueueDepth() {
  try {
    const resp = await _apiFetch('/api/stats');
    if (!resp.ok) return;
    const data = await resp.json();
    const depth = data.queue_depth ?? 0;
    const badge = document.getElementById('queue-badge');
    if (badge) {
      if (depth > 0) {
        badge.textContent = `${depth} write${depth === 1 ? '' : 's'} pending`;
        badge.classList.add('visible');
      } else {
        badge.classList.remove('visible');
      }
    }
  } catch (_) { /* silent — non-critical */ }
}

/* ---------------------------------------------------------------------------
 * Add Bookmark Modal
 * ----------------------------------------------------------------------- */
let _modalMode = 'slug';  // 'slug' | 'search'

function openModal() {
  const overlay = document.getElementById('modal-overlay');
  if (overlay) overlay.classList.add('open');
  const input = document.getElementById('modal-slug-input');
  if (input) { input.value = ''; input.focus(); }
  const label = document.getElementById('modal-label-input');
  if (label) label.value = '';
  _setModalMode('slug');
  _clearAutocomplete();
  _clearSearchResults();
}

function closeModal() {
  const overlay = document.getElementById('modal-overlay');
  if (overlay) overlay.classList.remove('open');
}

function _setModalMode(mode) {
  _modalMode = mode;
  const slugField = document.getElementById('slug-field');
  const searchField = document.getElementById('search-field');
  const radioSlug = document.getElementById('radio-slug');
  const radioSearch = document.getElementById('radio-search');
  if (mode === 'slug') {
    if (slugField) slugField.style.display = 'block';
    if (searchField) searchField.style.display = 'none';
    if (radioSlug) radioSlug.checked = true;
  } else {
    if (slugField) slugField.style.display = 'none';
    if (searchField) searchField.style.display = 'block';
    if (radioSearch) radioSearch.checked = true;
  }
}

function _clearAutocomplete() {
  const ac = document.getElementById('slug-autocomplete');
  if (ac) { ac.innerHTML = ''; ac.classList.remove('visible'); }
}

function _clearSearchResults() {
  const sr = document.getElementById('search-results');
  if (sr) { sr.innerHTML = ''; sr.classList.remove('visible'); }
}

// Slug autocomplete
const _fetchSlugAC = _debounce(async (prefix) => {
  if (!prefix) { _clearAutocomplete(); return; }
  try {
    const resp = await _apiFetch(`/api/wiki/list?slug_prefix=${encodeURIComponent(prefix)}`);
    const pages = resp.ok ? await resp.json() : [];
    const ac = document.getElementById('slug-autocomplete');
    if (!ac) return;
    ac.innerHTML = '';
    if (!pages.length) { ac.classList.remove('visible'); return; }
    pages.slice(0, 12).forEach(p => {
      const item = document.createElement('div');
      item.className = 'ac-item';
      item.textContent = p.slug;
      if (p.title) item.title = p.title;
      item.addEventListener('click', () => {
        const input = document.getElementById('modal-slug-input');
        if (input) input.value = p.slug;
        _clearAutocomplete();
      });
      ac.appendChild(item);
    });
    ac.classList.add('visible');
  } catch (_) { _clearAutocomplete(); }
}, 150);

// Semantic search
const _fetchSearch = _debounce(async (q) => {
  if (q.length < 2) { _clearSearchResults(); return; }
  try {
    const resp = await _apiFetch(`/api/wiki/search?q=${encodeURIComponent(q)}&limit=8`);
    const results = resp.ok ? await resp.json() : [];
    const sr = document.getElementById('search-results');
    if (!sr) return;
    sr.innerHTML = '';
    if (!results.length) { sr.classList.remove('visible'); return; }
    results.forEach(r => {
      const item = document.createElement('div');
      item.className = 'sr-item';
      const score = typeof r.score === 'number' ? r.score.toFixed(2) : '';
      item.innerHTML = `<span>${r.title || r.slug}</span><span class="sr-score">${score}</span>`;
      item.title = r.slug;
      item.addEventListener('click', () => {
        // Fill slug field and switch to slug mode for confirmation
        _setModalMode('slug');
        const input = document.getElementById('modal-slug-input');
        if (input) input.value = r.slug;
        const lbl = document.getElementById('modal-label-input');
        if (lbl && !lbl.value) lbl.value = r.title || '';
        _clearSearchResults();
      });
      sr.appendChild(item);
    });
    sr.classList.add('visible');
  } catch (_) { _clearSearchResults(); }
}, 300);

async function submitModal() {
  const slugInput = document.getElementById('modal-slug-input');
  const labelInput = document.getElementById('modal-label-input');
  const addBtn = document.getElementById('modal-add-btn');

  const slug = (slugInput?.value || '').trim();
  if (!slug) return;

  if (addBtn) addBtn.disabled = true;
  try {
    const resp = await _apiFetch('/api/bookmarks', {
      method: 'POST',
      body: JSON.stringify({
        slug,
        label_override: (labelInput?.value || '').trim(),
      }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      alert(`Could not add bookmark: ${body.reason || resp.status}`);
      return;
    }
    closeModal();
    await loadBookmarks();
    // Auto-select the newly added bookmark
    const idx = _bookmarks.findIndex(b => b.slug === slug);
    if (idx >= 0) selectBookmark(idx);
  } catch (e) {
    alert(`Error: ${e.message}`);
  } finally {
    if (addBtn) addBtn.disabled = false;
  }
}

/* ---------------------------------------------------------------------------
 * Keyboard shortcuts (global)
 * ----------------------------------------------------------------------- */
document.addEventListener('keydown', (e) => {
  const overlay = document.getElementById('modal-overlay');
  const modalOpen = overlay?.classList.contains('open');

  if (e.key === 'Escape') {
    if (modalOpen) closeModal();
    return;
  }

  if (modalOpen) return;  // don't intercept keys while modal open

  // j/k to navigate bookmark list
  if (e.key === 'j' || e.key === 'k') {
    if (_bookmarks.length === 0) return;
    const cur = _bookmarks.findIndex(b => b.slug === _selectedSlug);
    let next = cur < 0 ? 0 : (e.key === 'j' ? cur + 1 : cur - 1);
    next = Math.max(0, Math.min(_bookmarks.length - 1, next));
    selectBookmark(next);
  }
});

/* ---------------------------------------------------------------------------
 * Boot
 * ----------------------------------------------------------------------- */
document.addEventListener('DOMContentLoaded', () => {
  _configureMarked();
  loadBookmarks();

  // Poll queue depth every 5s
  _pollQueueDepth();
  _queuePollTimer = setInterval(_pollQueueDepth, 5_000);

  // Wire up modal radio buttons
  const radioSlug = document.getElementById('radio-slug');
  const radioSearch = document.getElementById('radio-search');
  if (radioSlug) radioSlug.addEventListener('change', () => _setModalMode('slug'));
  if (radioSearch) radioSearch.addEventListener('change', () => _setModalMode('search'));

  // Slug input → autocomplete
  const slugInput = document.getElementById('modal-slug-input');
  if (slugInput) {
    slugInput.addEventListener('input', () => _fetchSlugAC(slugInput.value.trim()));
    slugInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { _clearAutocomplete(); submitModal(); }
      if (e.key === 'Tab') {
        // Accept top autocomplete suggestion
        const first = document.querySelector('#slug-autocomplete .ac-item');
        if (first) { slugInput.value = first.textContent; _clearAutocomplete(); e.preventDefault(); }
      }
    });
  }

  // Semantic search input
  const searchInput = document.getElementById('modal-search-input');
  if (searchInput) {
    searchInput.addEventListener('input', () => _fetchSearch(searchInput.value.trim()));
  }

  // Close modal when clicking overlay backdrop
  const overlay = document.getElementById('modal-overlay');
  if (overlay) {
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
  }
});
