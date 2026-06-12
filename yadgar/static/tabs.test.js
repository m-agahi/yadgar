/**
 * tabs.test.js — v5.50.0 hash-router behavioral tests
 *
 * Tests the pure router logic extracted to tabs.js.
 * Uses jsdom (vitest environment: 'jsdom') for DOM assertions.
 *
 * Run: cd viz-tests && npm test
 */

import { describe, expect, it, beforeEach } from 'vitest';
import { resolveTab, VALID_TABS } from './tabs.js';

// ── resolveTab — pure hash-to-tab-name ────────────────────────────────────────

describe('resolveTab', () => {
  it('returns home for empty string', () => {
    expect(resolveTab('')).toBe('home');
  });

  it('returns home for bare # (no name)', () => {
    expect(resolveTab('#')).toBe('home');
  });

  it('returns home for #home', () => {
    expect(resolveTab('#home')).toBe('home');
  });

  it('resolves each valid tab from hash string', () => {
    expect(resolveTab('#stats')).toBe('stats');
    expect(resolveTab('#health')).toBe('health');
    expect(resolveTab('#bookmarks')).toBe('bookmarks');
    expect(resolveTab('#info')).toBe('info');
    expect(resolveTab('#control')).toBe('control');
    expect(resolveTab('#debug')).toBe('debug');
    expect(resolveTab('#help')).toBe('help');
  });

  it('falls back to home for unknown hash', () => {
    expect(resolveTab('#unknown')).toBe('home');
    expect(resolveTab('#foobar')).toBe('home');
  });

  it('strips sub-path after /', () => {
    expect(resolveTab('#stats/detail')).toBe('stats');
    expect(resolveTab('#home/section')).toBe('home');
  });

  it('strips leading # before lookup', () => {
    // Both with and without # should resolve the same — # is stripped before VALID_TABS lookup
    expect(resolveTab('#health')).toBe('health');
    expect(resolveTab('health')).toBe('health');
  });
});

// ── VALID_TABS — exported set ─────────────────────────────────────────────────

describe('VALID_TABS', () => {
  it('contains all eight defined tabs', () => {
    expect(VALID_TABS.has('home')).toBe(true);
    expect(VALID_TABS.has('stats')).toBe(true);
    expect(VALID_TABS.has('health')).toBe(true);
    expect(VALID_TABS.has('bookmarks')).toBe(true);
    expect(VALID_TABS.has('info')).toBe(true);
    expect(VALID_TABS.has('control')).toBe(true);
    expect(VALID_TABS.has('debug')).toBe(true);
    expect(VALID_TABS.has('help')).toBe(true);
  });

  it('has exactly 8 entries', () => {
    expect(VALID_TABS.size).toBe(8);
  });
});

// ── DOM behavioral tests (requires jsdom) ─────────────────────────────────────

import { switchTab } from './tabs.js';

/**
 * Build a minimal DOM matching the SPA tab structure.
 * Returns document with #tab-bar + 7 .tab-pane divs.
 */
function makeTabDOM() {
  const tabs = ['home', 'stats', 'health', 'bookmarks', 'info', 'control', 'debug', 'help'];

  // tab bar
  const tabBar = document.createElement('nav');
  tabBar.id = 'tab-bar';
  tabs.forEach(t => {
    const a = document.createElement('a');
    a.className = 'tab-link';
    a.dataset.tab = t;
    a.href = '#' + t;
    tabBar.appendChild(a);
  });

  // panes
  const panes = tabs.map(t => {
    const div = document.createElement('div');
    div.id = 'tab-' + t;
    div.className = 'tab-pane';
    return div;
  });

  // mount
  document.body.innerHTML = '';
  document.body.appendChild(tabBar);
  panes.forEach(p => document.body.appendChild(p));

  return { tabs, panes };
}

describe('switchTab DOM behavior', () => {
  beforeEach(() => {
    makeTabDOM();
  });

  it('activates correct pane + link for each tab', () => {
    const tabs = ['home', 'stats', 'health', 'bookmarks', 'info', 'control', 'debug', 'help'];
    for (const t of tabs) {
      switchTab(t);
      expect(document.getElementById('tab-' + t).classList.contains('active')).toBe(true);
      const link = document.querySelector('#tab-bar a[data-tab="' + t + '"]');
      expect(link.classList.contains('active')).toBe(true);
    }
  });

  it('deactivates all other panes when switching', () => {
    switchTab('stats');
    const panes = document.querySelectorAll('.tab-pane');
    const active = [...panes].filter(p => p.classList.contains('active'));
    expect(active.length).toBe(1);
    expect(active[0].id).toBe('tab-stats');
  });

  it('defaults to home for unknown tab name', () => {
    switchTab('nonexistent');
    expect(document.getElementById('tab-home').classList.contains('active')).toBe(true);
  });

  it('only one tab-link is active at a time', () => {
    switchTab('health');
    const links = document.querySelectorAll('#tab-bar .tab-link');
    const active = [...links].filter(l => l.classList.contains('active'));
    expect(active.length).toBe(1);
    expect(active[0].dataset.tab).toBe('health');
  });
});
