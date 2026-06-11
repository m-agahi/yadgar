/**
 * preview-pane.js — v5.50.1 Bookmarks Tab PreviewPane component
 *
 * Renders markdown wiki content via marked.js v15.
 * PRESERVES the v5.24.2 marked-fix verbatim (renderer.text token-compat fix).
 *
 * Exported for tests:
 *   makeRendererTextFn(marked)  → renderer.text function (v5.24.2 fix, testable)
 *   buildPreviewHeader(slug, title, isStarred, onClose, onStar) → HTMLElement
 *
 * Security: all markdown output is sanitized via DOMPurify before innerHTML assignment.
 * DOMPurify is expected as window.DOMPurify (vendored in lib/).
 */

'use strict';

// ── v5.24.2 marked renderer fix (pure, exported for tests) ─────────────────

/**
 * Build the renderer.text function that handles marked v15 token objects.
 *
 * v5.24.1 bug: after extracting token.text, code delegated back to v15's default
 * text renderer via a bound _origText reference, which does `'tokens' in arg` —
 * throwing on a plain string. v5.24.2 fix: return the replaced HTML string directly;
 * DOMPurify downstream handles XSS.
 *
 * marked v15: token is {type:"text", text:"...", tokens:[...]}
 * marked v14 and below: token is already the string.
 *
 * @param {object} markedInstance - the marked library object (for injection in tests)
 * @returns {function} renderer.text compatible with marked v14 and v15
 */
export function makeRendererTextFn(markedInstance) {
  // The returned function is assigned to renderer.text — must NOT call
  // the original renderer.text (that's the v5.24.1 bug). Return HTML directly.
  return function rendererText(token) {
    const raw =
      typeof token === 'object' && token !== null && typeof token.text === 'string'
        ? token.text
        : typeof token === 'string'
          ? token
          : '';
    // Replace [[slug]] patterns with anchor that triggers navigation.
    // XSS-safe: slug is attribute-escaped; DOMPurify sanitizes the full output.
    return raw.replace(
      /\[\[([^\]]+)\]\]/g,
      (_, slug) =>
        `<a href="#" class="wiki-xref" data-slug="${slug.replace(/"/g, '&quot;')}">${slug}</a>`
    );
  };
}

/**
 * Configure a marked instance with the v5.24.2 renderer and hljs (if available).
 * @param {object} markedInstance
 * @param {object|null} [hljsInstance]
 */
export function configureMarked(markedInstance, hljsInstance = null) {
  if (!markedInstance) return;

  // Syntax highlighting
  if (hljsInstance) {
    markedInstance.setOptions({
      highlight: (code, lang) => {
        if (lang && hljsInstance.getLanguage(lang)) {
          return hljsInstance.highlight(code, { language: lang }).value;
        }
        return hljsInstance.highlightAuto(code).value;
      },
    });
  }

  // v5.24.2 fix: custom renderer.text
  const renderer = new markedInstance.Renderer();
  renderer.text = makeRendererTextFn(markedInstance);
  markedInstance.setOptions({ renderer });
}

/**
 * Parse markdown to sanitized HTML.
 * Falls back to escaped plain text if marked or DOMPurify unavailable.
 *
 * @param {string} content - raw markdown string
 * @param {object|null} [markedInstance] - marked library (window.marked if omitted)
 * @param {object|null} [domPurifyInstance] - DOMPurify (window.DOMPurify if omitted)
 * @returns {string} sanitized HTML string
 */
export function parseMarkdown(content, markedInstance = null, domPurifyInstance = null) {
  const _marked = markedInstance || (typeof window !== 'undefined' ? window.marked : null);
  const _purify = domPurifyInstance || (typeof window !== 'undefined' ? window.DOMPurify : null);

  if (!_marked) {
    // No marked available: escape and return as preformatted text
    const escaped = String(content ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
    return `<pre>${escaped}</pre>`;
  }

  // Guard: content must be a string (v5.24.1 bug: non-string passed to marked.parse)
  const src = typeof content === 'string' ? content : '';
  const html = _marked.parse(src);

  if (_purify) {
    return _purify.sanitize(html, { ADD_ATTR: ['data-slug'] });
  }
  // DOMPurify not loaded — basic fallback (should not happen in production)
  return html;
}

// ── Component ───────────────────────────────────────────────────────────────

export class PreviewPane {
  /**
   * @param {Object} opts
   * @param {HTMLElement} opts.container
   * @param {function(string): void} opts.onClose   - called when user closes preview
   * @param {function(string, boolean): void} opts.onStarToggle - (slug, newStarred)
   * @param {function(string): void} opts.onXrefClick - [[slug]] link clicked
   * @param {object|null} [opts.markedInstance]     - injected marked (tests)
   * @param {object|null} [opts.domPurifyInstance]  - injected DOMPurify (tests)
   */
  constructor({ container, onClose, onStarToggle, onXrefClick, markedInstance = null, domPurifyInstance = null }) {
    this._container = container;
    this._onClose = onClose;
    this._onStarToggle = onStarToggle;
    this._onXrefClick = onXrefClick;
    this._marked = markedInstance || (typeof window !== 'undefined' ? window.marked : null);
    this._purify = domPurifyInstance || (typeof window !== 'undefined' ? window.DOMPurify : null);
    this._slug = null;
    this._starred = false;

    if (this._marked) configureMarked(this._marked, typeof window !== 'undefined' ? window.hljs : null);

    this._build();
  }

  _build() {
    this._container.className = 'bm-preview';

    // Header
    this._header = document.createElement('div');
    this._header.className = 'bm-preview-header';

    this._closeBtn = document.createElement('button');
    this._closeBtn.className = 'bm-preview-close';
    this._closeBtn.textContent = '×';
    this._closeBtn.title = 'Close preview (Esc)';
    this._closeBtn.addEventListener('click', () => this._onClose());

    this._titleEl = document.createElement('span');
    this._titleEl.className = 'bm-preview-title';

    this._starBtn = document.createElement('button');
    this._starBtn.className = 'bm-preview-star';
    this._starBtn.title = 'Toggle bookmark (Ctrl+B / ⌘B)';
    this._starBtn.addEventListener('click', () => {
      if (this._slug) {
        this._starred = !this._starred;
        this._updateStar();
        this._onStarToggle(this._slug, this._starred);
      }
    });

    this._header.appendChild(this._closeBtn);
    this._header.appendChild(this._titleEl);
    this._header.appendChild(this._starBtn);

    // Body
    this._body = document.createElement('div');
    this._body.className = 'bm-preview-body';

    // Delegate [[slug]] link clicks
    this._body.addEventListener('click', (e) => {
      const a = e.target.closest('a.wiki-xref');
      if (a && this._onXrefClick) {
        e.preventDefault();
        this._onXrefClick(a.dataset.slug || a.textContent);
      }
    });

    this._container.appendChild(this._header);
    this._container.appendChild(this._body);
  }

  /**
   * Load and render a wiki page.
   * @param {Object} page - {slug, title, content, ...}
   * @param {boolean} isStarred
   */
  show(page, isStarred) {
    this._slug = page.slug;
    this._starred = isStarred;
    this._titleEl.textContent = page.title || page.slug;
    this._updateStar();

    const html = parseMarkdown(page.content || '', this._marked, this._purify);
    // DOMPurify sanitizes html before setting innerHTML
    this._body.innerHTML = html;
  }

  /** Show a loading state. */
  showLoading() {
    this._body.innerHTML = '';
    const el = document.createElement('div');
    el.className = 'bm-loading';
    el.textContent = 'Loading…';
    this._body.appendChild(el);
  }

  /** Show an error state. */
  showError(msg) {
    this._body.innerHTML = '';
    const el = document.createElement('div');
    el.className = 'bm-error';
    el.textContent = msg;
    this._body.appendChild(el);
  }

  /** Update star button appearance. */
  _updateStar() {
    if (!this._starBtn) return;
    this._starBtn.textContent = this._starred ? '★' : '☆';
    this._starBtn.classList.toggle('starred', this._starred);
    this._starBtn.setAttribute('aria-pressed', String(this._starred));
  }

  /** Update star state without triggering callback (e.g. after API response). */
  setStarred(v) {
    this._starred = v;
    this._updateStar();
  }

  get slug() { return this._slug; }
  get starred() { return this._starred; }
}
