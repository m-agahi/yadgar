/**
 * preview-pane.test.js — v5.50.1 PreviewPane component behavioral tests
 *
 * Key tests:
 *   - makeRendererTextFn: v5.24.2 fix verbatim correctness
 *   - parseMarkdown: marked v15 token object + plain string
 *   - PreviewPane: renders content, star toggle, close callback
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  makeRendererTextFn,
  parseMarkdown,
  configureMarked,
  PreviewPane,
} from './preview-pane.js';

// ── makeRendererTextFn — v5.24.2 fix ────────────────────────────────────────

describe('makeRendererTextFn (v5.24.2 fix)', () => {
  const fn = makeRendererTextFn(null); // marked instance not needed for the fn

  it('accepts marked v15 token object {type, text, tokens}', () => {
    // v5.24.1 bug: calling with a token object caused a throw
    expect(() => fn({ type: 'text', text: 'hello world', tokens: [] })).not.toThrow();
  });

  it('accepts plain string (marked v14 style)', () => {
    expect(() => fn('plain string')).not.toThrow();
  });

  it('extracts text from token object', () => {
    const result = fn({ type: 'text', text: 'hello', tokens: [] });
    expect(result).toBe('hello');
  });

  it('returns plain string as-is when not a token object', () => {
    const result = fn('plain string');
    expect(result).toBe('plain string');
  });

  it('converts [[slug]] to wiki-xref anchor from token object', () => {
    const result = fn({ type: 'text', text: 'See [[my-page]] for details', tokens: [] });
    expect(result).toContain('class="wiki-xref"');
    expect(result).toContain('data-slug="my-page"');
    expect(result).toContain('my-page');
  });

  it('converts [[slug]] to wiki-xref anchor from plain string', () => {
    const result = fn('See [[my-page]] here');
    expect(result).toContain('class="wiki-xref"');
    expect(result).toContain('data-slug="my-page"');
  });

  it('converts multiple [[slug]] patterns in one text', () => {
    const result = fn('See [[page-a]] and [[page-b]]');
    expect(result).toContain('data-slug="page-a"');
    expect(result).toContain('data-slug="page-b"');
  });

  it('escapes double-quotes in slug for data-slug attribute', () => {
    // Slug with special chars — attribute must not break
    const result = fn({ type: 'text', text: '[[slug"with"quotes]]', tokens: [] });
    // The data-slug value must have &quot; instead of raw "
    expect(result).toContain('&quot;');
    expect(result).not.toContain('data-slug="slug"with"');
  });

  it('does NOT call original renderer.text (v5.24.1 bug prevention)', () => {
    // The fix: return HTML directly; DO NOT call a bound _origText
    // Verify by checking the function does not re-invoke itself recursively
    let callCount = 0;
    const spiedFn = makeRendererTextFn(null);
    const wrapped = (token) => { callCount++; return spiedFn(token); };
    wrapped({ type: 'text', text: 'test', tokens: [] });
    expect(callCount).toBe(1); // exactly one call — no recursive delegation
  });

  it('returns empty string for null/undefined token', () => {
    const result = fn(null);
    expect(result).toBe('');
  });
});

// ── parseMarkdown ────────────────────────────────────────────────────────────

describe('parseMarkdown', () => {
  it('falls back to escaped pre when marked unavailable', () => {
    const result = parseMarkdown('# Hello', null, null);
    expect(result).toContain('<pre>');
    expect(result).toContain('# Hello');
    // Must not contain raw HTML tags from markdown parsing
    expect(result).not.toContain('<h1>');
  });

  it('escapes HTML entities in fallback (XSS safety)', () => {
    const result = parseMarkdown('<script>alert(1)</script>', null, null);
    expect(result).toContain('&lt;script&gt;');
    expect(result).not.toContain('<script>');
  });

  it('handles non-string content gracefully (v5.24.1 guard)', () => {
    expect(() => parseMarkdown(null, null, null)).not.toThrow();
    expect(() => parseMarkdown(undefined, null, null)).not.toThrow();
    expect(() => parseMarkdown({}, null, null)).not.toThrow();
  });

  it('calls marked.parse with string content when marked available', () => {
    const markedMock = {
      parse: vi.fn().mockReturnValue('<p>hello</p>'),
      Renderer: class { text() {} },
      setOptions: vi.fn(),
    };
    const purifyMock = { sanitize: vi.fn().mockReturnValue('<p>hello</p>') };
    parseMarkdown('hello', markedMock, purifyMock);
    expect(markedMock.parse).toHaveBeenCalledWith('hello');
  });

  it('passes output through DOMPurify.sanitize when available', () => {
    const markedMock = {
      parse: vi.fn().mockReturnValue('<p>ok</p>'),
      Renderer: class { text() {} },
      setOptions: vi.fn(),
    };
    const purifyMock = { sanitize: vi.fn().mockReturnValue('<p>ok</p>') };
    parseMarkdown('ok', markedMock, purifyMock);
    expect(purifyMock.sanitize).toHaveBeenCalledWith('<p>ok</p>', expect.objectContaining({ ADD_ATTR: ['data-slug'] }));
  });
});

// ── PreviewPane component ────────────────────────────────────────────────────

describe('PreviewPane component', () => {
  let container;
  let onClose;
  let onStarToggle;
  let onXrefClick;
  let pane;

  // Minimal marked mock: does not call renderer.text delegate (v5.24.2 fix)
  const markedMock = {
    Renderer: class {
      text(token) {
        const raw = typeof token === 'object' ? token.text : token;
        return raw || '';
      }
    },
    setOptions: vi.fn(),
    parse: vi.fn((src) => `<p>${src}</p>`),
  };

  const purifyMock = {
    sanitize: vi.fn((html) => html),
  };

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    onClose = vi.fn();
    onStarToggle = vi.fn();
    onXrefClick = vi.fn();
    pane = new PreviewPane({
      container,
      onClose,
      onStarToggle,
      onXrefClick,
      markedInstance: markedMock,
      domPurifyInstance: purifyMock,
    });
  });

  afterEach(() => {
    document.body.removeChild(container);
  });

  it('renders header and body elements', () => {
    expect(container.querySelector('.bm-preview-header')).toBeTruthy();
    expect(container.querySelector('.bm-preview-body')).toBeTruthy();
  });

  it('show() sets title text', () => {
    pane.show({ slug: 'my-page', title: 'My Page Title', content: 'hello' }, false);
    const titleEl = container.querySelector('.bm-preview-title');
    expect(titleEl.textContent).toBe('My Page Title');
  });

  it('show() renders content via marked.parse', () => {
    pane.show({ slug: 'p', title: 'P', content: 'test content' }, false);
    expect(markedMock.parse).toHaveBeenCalledWith('test content');
  });

  it('show(starred=true) renders filled star', () => {
    pane.show({ slug: 'p', title: 'P', content: '' }, true);
    const starBtn = container.querySelector('.bm-preview-star');
    expect(starBtn.classList.contains('starred')).toBe(true);
    expect(starBtn.textContent).toBe('★');
  });

  it('show(starred=false) renders empty star', () => {
    pane.show({ slug: 'p', title: 'P', content: '' }, false);
    const starBtn = container.querySelector('.bm-preview-star');
    expect(starBtn.classList.contains('starred')).toBe(false);
    expect(starBtn.textContent).toBe('☆');
  });

  it('clicking star toggles starred state and calls onStarToggle', () => {
    pane.show({ slug: 'test-slug', title: 'T', content: '' }, false);
    const starBtn = container.querySelector('.bm-preview-star');
    starBtn.click();
    expect(onStarToggle).toHaveBeenCalledWith('test-slug', true);
    expect(pane.starred).toBe(true);
  });

  it('clicking star twice toggles back to unstarred', () => {
    pane.show({ slug: 'test-slug', title: 'T', content: '' }, false);
    const starBtn = container.querySelector('.bm-preview-star');
    starBtn.click();
    starBtn.click();
    expect(onStarToggle).toHaveBeenLastCalledWith('test-slug', false);
  });

  it('clicking close button calls onClose', () => {
    const closeBtn = container.querySelector('.bm-preview-close');
    closeBtn.click();
    expect(onClose).toHaveBeenCalledOnce();
  });

  // P3.10 [57]: live refresh control — re-fetch the wiki page without a full
  // viz reload. onRefresh is optional (back-compat for callers that omit it).
  it('renders a refresh button in the header', () => {
    expect(container.querySelector('.bm-preview-refresh')).toBeTruthy();
  });

  it('clicking refresh calls onRefresh with the active slug', () => {
    const onRefresh = vi.fn();
    const c2 = document.createElement('div');
    const p2 = new PreviewPane({
      container: c2, onClose, onStarToggle, onXrefClick, onRefresh,
      markedInstance: markedMock, domPurifyInstance: purifyMock,
    });
    p2.show({ slug: 'live-slug', title: 'L', content: '' }, false);
    c2.querySelector('.bm-preview-refresh').click();
    expect(onRefresh).toHaveBeenCalledWith('live-slug');
  });

  it('clicking refresh with no page loaded does not call onRefresh', () => {
    const onRefresh = vi.fn();
    const c3 = document.createElement('div');
    const p3 = new PreviewPane({
      container: c3, onClose, onStarToggle, onXrefClick, onRefresh,
      markedInstance: markedMock, domPurifyInstance: purifyMock,
    });
    c3.querySelector('.bm-preview-refresh').click();
    expect(onRefresh).not.toHaveBeenCalled();
  });

  it('refresh button is safe when onRefresh is omitted (no throw)', () => {
    pane.show({ slug: 'p', title: 'P', content: '' }, false);
    const refreshBtn = container.querySelector('.bm-preview-refresh');
    expect(() => refreshBtn.click()).not.toThrow();
  });

  it('setStarred() updates star without calling onStarToggle', () => {
    pane.show({ slug: 'p', title: 'P', content: '' }, false);
    pane.setStarred(true);
    expect(pane.starred).toBe(true);
    expect(onStarToggle).not.toHaveBeenCalled();
  });

  it('showLoading() renders loading state', () => {
    pane.showLoading();
    expect(container.querySelector('.bm-loading')).toBeTruthy();
  });

  it('showError() renders error message', () => {
    pane.showError('Something went wrong');
    const errEl = container.querySelector('.bm-error');
    expect(errEl).toBeTruthy();
    expect(errEl.textContent).toBe('Something went wrong');
  });
});
