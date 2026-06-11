/**
 * bookmark-spine.test.js — v5.50.1 BookmarkShelf component behavioral tests
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { reorderArray, truncateSlug, BookmarkShelf } from './bookmark-spine.js';

// ── reorderArray ──────────────────────────────────────────────────────────────

describe('reorderArray', () => {
  it('moves item forward in array', () => {
    expect(reorderArray([1, 2, 3, 4], 0, 2)).toEqual([2, 3, 1, 4]);
  });

  it('moves item backward in array', () => {
    expect(reorderArray([1, 2, 3, 4], 3, 1)).toEqual([1, 4, 2, 3]);
  });

  it('same-index is a no-op copy', () => {
    expect(reorderArray([1, 2, 3], 1, 1)).toEqual([1, 2, 3]);
  });

  it('does not mutate original array', () => {
    const orig = [1, 2, 3];
    reorderArray(orig, 0, 2);
    expect(orig).toEqual([1, 2, 3]);
  });

  it('works with single element', () => {
    expect(reorderArray(['a'], 0, 0)).toEqual(['a']);
  });

  it('moves first to last', () => {
    expect(reorderArray(['a', 'b', 'c'], 0, 2)).toEqual(['b', 'c', 'a']);
  });

  it('moves last to first', () => {
    expect(reorderArray(['a', 'b', 'c'], 2, 0)).toEqual(['c', 'a', 'b']);
  });
});

// ── truncateSlug ──────────────────────────────────────────────────────────────

describe('truncateSlug', () => {
  it('returns slug as-is when within maxLen', () => {
    expect(truncateSlug('short', 12)).toBe('short');
  });

  it('truncates with ellipsis when over maxLen', () => {
    const result = truncateSlug('a-very-long-slug-name', 12);
    expect(result.length).toBeLessThanOrEqual(13); // 12 + ellipsis char
    expect(result.endsWith('…')).toBe(true);
  });

  it('handles empty slug', () => {
    expect(truncateSlug('', 12)).toBe('');
  });

  it('handles null slug', () => {
    expect(truncateSlug(null, 12)).toBe('');
  });

  it('default maxLen is 12', () => {
    const long = 'abcdefghijklmnop'; // 16 chars
    const result = truncateSlug(long);
    expect(result.length).toBeLessThanOrEqual(13);
  });
});

// ── BookmarkShelf component ───────────────────────────────────────────────────

describe('BookmarkShelf component', () => {
  let container;
  let onSpineClick;
  let onReorder;
  let shelf;

  const BOOKMARKS = [
    { slug: 'roadmap', label_override: '', position: 0, added_at: '2026-01-01' },
    { slug: 'benchmarks', label_override: 'bench', position: 1, added_at: '2026-01-02' },
    { slug: 'architecture', label_override: '', position: 2, added_at: '2026-01-03' },
  ];

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    onSpineClick = vi.fn();
    onReorder = vi.fn();
    shelf = new BookmarkShelf({ container, onSpineClick, onReorder });
    shelf.setBookmarks(BOOKMARKS);
  });

  afterEach(() => {
    document.body.removeChild(container);
  });

  it('renders a spine for each bookmark', () => {
    const spines = container.querySelectorAll('.bm-spine');
    expect(spines.length).toBe(3);
  });

  it('spine shows slug text', () => {
    const spines = container.querySelectorAll('.bm-spine');
    const text = spines[0].querySelector('.bm-spine-slug').textContent;
    expect(text).toBeTruthy();
    expect(text.length).toBeGreaterThan(0);
  });

  it('spine uses label_override when set', () => {
    const spines = container.querySelectorAll('.bm-spine');
    const label = spines[1].querySelector('.bm-spine-slug').textContent;
    // bench (label_override) not benchmarks
    expect(label).toContain('bench');
  });

  it('spine shows filled star', () => {
    const stars = container.querySelectorAll('.bm-spine-star');
    stars.forEach(s => expect(s.textContent).toBe('★'));
  });

  it('clicking a spine calls onSpineClick with slug', () => {
    const spines = container.querySelectorAll('.bm-spine');
    spines[0].click();
    expect(onSpineClick).toHaveBeenCalledWith('roadmap');
  });

  it('clicking second spine calls onSpineClick with correct slug', () => {
    const spines = container.querySelectorAll('.bm-spine');
    spines[1].click();
    expect(onSpineClick).toHaveBeenCalledWith('benchmarks');
  });

  it('empty bookmarks shows empty state message', () => {
    shelf.setBookmarks([]);
    const emptyEl = container.querySelector('.bm-shelf-empty');
    expect(emptyEl.style.display).not.toBe('none');
  });

  it('non-empty bookmarks hides empty state', () => {
    const emptyEl = container.querySelector('.bm-shelf-empty');
    expect(emptyEl.style.display).toBe('none');
  });

  it('navigate(+1) then navigate(+1) moves focus to index 1', () => {
    shelf.navigate(1); // navIdx: 0
    shelf.navigate(1); // navIdx: 1
    const spines = container.querySelectorAll('.bm-spine');
    expect(spines[1].classList.contains('hover')).toBe(true);
  });

  it('navigate clamps at 0', () => {
    shelf.navigate(-10); // should clamp at 0
    const spines = container.querySelectorAll('.bm-spine');
    expect(spines[0].classList.contains('hover')).toBe(true);
  });

  it('activateNav() calls onSpineClick for nav-focused spine', () => {
    shelf.navigate(1); // navIdx: 0
    shelf.navigate(1); // navIdx: 1
    shelf.activateNav();
    expect(onSpineClick).toHaveBeenCalledWith('benchmarks');
  });

  it('bookmarks getter returns copy of current bookmarks', () => {
    const bms = shelf.bookmarks;
    expect(bms.length).toBe(3);
    bms.push({ slug: 'extra' });
    expect(shelf.bookmarks.length).toBe(3); // original not mutated
  });
});
