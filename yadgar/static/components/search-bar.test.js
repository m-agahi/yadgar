/**
 * search-bar.test.js — v5.50.1 SearchBar component behavioral tests
 *
 * Tests pure logic and component behavior (Vitest + jsdom).
 * Does NOT test HTTP calls — those are Python-side.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  buildQueryParams,
  getModeFromStorage,
  setModeToStorage,
  cycleMode,
  SearchBar,
} from './search-bar.js';

// ── buildQueryParams ─────────────────────────────────────────────────────────

describe('buildQueryParams', () => {
  it('returns q and mode fields', () => {
    const p = buildQueryParams('benchmark', 'semantic');
    expect(p.q).toBe('benchmark');
    expect(p.mode).toBe('semantic');
  });

  it('trims whitespace from query', () => {
    const p = buildQueryParams('  spaces  ', 'keyword');
    expect(p.q).toBe('spaces');
  });

  it('preserves mode value for all three modes', () => {
    expect(buildQueryParams('x', 'semantic').mode).toBe('semantic');
    expect(buildQueryParams('x', 'keyword').mode).toBe('keyword');
    expect(buildQueryParams('x', 'slug').mode).toBe('slug');
  });

  it('handles empty query', () => {
    const p = buildQueryParams('', 'semantic');
    expect(p.q).toBe('');
  });
});

// ── getModeFromStorage / setModeToStorage ─────────────────────────────────────

describe('mode localStorage persistence', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('returns semantic when no stored value', () => {
    expect(getModeFromStorage()).toBe('semantic');
  });

  it('round-trips semantic mode', () => {
    setModeToStorage('semantic');
    expect(getModeFromStorage()).toBe('semantic');
  });

  it('round-trips keyword mode', () => {
    setModeToStorage('keyword');
    expect(getModeFromStorage()).toBe('keyword');
  });

  it('round-trips slug mode', () => {
    setModeToStorage('slug');
    expect(getModeFromStorage()).toBe('slug');
  });

  it('falls back to semantic for unknown stored value', () => {
    localStorage.setItem('yadgar.bm.searchMode', 'invalid');
    expect(getModeFromStorage()).toBe('semantic');
  });
});

// ── cycleMode ────────────────────────────────────────────────────────────────

describe('cycleMode', () => {
  it('cycles semantic → keyword', () => {
    expect(cycleMode('semantic')).toBe('keyword');
  });

  it('cycles keyword → slug', () => {
    expect(cycleMode('keyword')).toBe('slug');
  });

  it('cycles slug → semantic (wraps around)', () => {
    expect(cycleMode('slug')).toBe('semantic');
  });
});

// ── SearchBar component ───────────────────────────────────────────────────────

describe('SearchBar component', () => {
  let container;
  let onSearch;
  let bar;

  beforeEach(() => {
    localStorage.clear();
    container = document.createElement('div');
    document.body.appendChild(container);
    onSearch = vi.fn();
    bar = new SearchBar({ container, onSearch, debounceMs: 0 });
  });

  afterEach(() => {
    document.body.removeChild(container);
  });

  it('renders search input', () => {
    expect(container.querySelector('input[type="text"]')).toBeTruthy();
  });

  it('renders three mode chips', () => {
    const chips = container.querySelectorAll('.bm-mode-chip');
    expect(chips.length).toBe(3);
    const labels = Array.from(chips).map(c => c.textContent);
    expect(labels).toContain('semantic');
    expect(labels).toContain('keyword');
    expect(labels).toContain('slug');
  });

  it('first chip is active by default (semantic)', () => {
    const active = container.querySelectorAll('.bm-mode-chip.active');
    expect(active.length).toBe(1);
    expect(active[0].textContent).toBe('semantic');
  });

  it('clicking keyword chip changes active mode', () => {
    const chips = Array.from(container.querySelectorAll('.bm-mode-chip'));
    const kwChip = chips.find(c => c.textContent === 'keyword');
    kwChip.click();
    expect(bar.mode).toBe('keyword');
    expect(kwChip.classList.contains('active')).toBe(true);
  });

  it('clicking mode chip with active query fires onSearch with new mode', () => {
    // Set query manually
    const input = container.querySelector('input');
    input.value = 'test query';
    bar._query = 'test query';

    const chips = Array.from(container.querySelectorAll('.bm-mode-chip'));
    const kwChip = chips.find(c => c.textContent === 'keyword');
    kwChip.click();

    expect(onSearch).toHaveBeenCalledWith('test query', 'keyword');
  });

  it('mode change persists to localStorage', () => {
    const chips = Array.from(container.querySelectorAll('.bm-mode-chip'));
    const slugChip = chips.find(c => c.textContent === 'slug');
    slugChip.click();
    expect(getModeFromStorage()).toBe('slug');
  });

  it('setCount updates count element text', () => {
    bar.setCount(5);
    const countEl = container.querySelector('.bm-search-count');
    expect(countEl.textContent).toBe('5 results');
  });

  it('setCount(1) uses singular form', () => {
    bar.setCount(1);
    const countEl = container.querySelector('.bm-search-count');
    expect(countEl.textContent).toBe('1 result');
  });

  it('setCount(null) clears count', () => {
    bar.setCount(5);
    bar.setCount(null);
    const countEl = container.querySelector('.bm-search-count');
    expect(countEl.textContent).toBe('');
  });

  it('clear() resets input value and query', () => {
    const input = container.querySelector('input');
    input.value = 'old query';
    bar._query = 'old query';
    bar.clear();
    expect(bar.query).toBe('');
    expect(input.value).toBe('');
  });

  it('focus() focuses the input element', () => {
    bar.focus();
    expect(document.activeElement).toBe(container.querySelector('input'));
  });
});
