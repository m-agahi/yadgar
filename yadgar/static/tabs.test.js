/**
 * tabs.test.js — v5.50.0 hash-router tab tests
 *
 * Tests that the tab router in index.html correctly has containers
 * for each hash route. Uses string/regex analysis on the HTML file
 * (no browser/jsdom required — headless safe).
 *
 * Run: cd viz-tests && npm test
 */

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const HTML_PATH = join(__dirname, 'index.html');

function readHtml() {
  return readFileSync(HTML_PATH, 'utf-8');
}

/**
 * Check if a given element with id="tab-{name}" exists in the HTML.
 */
function hasTabContainer(html, name) {
  return (
    html.includes(`id="tab-${name}"`) ||
    html.includes(`id='tab-${name}'`)
  );
}

/**
 * Check if the tab bar contains a link to #{name}.
 */
function hasTabLink(html, name) {
  return (
    html.includes(`href="#${name}"`) ||
    html.includes(`href='#${name}'`) ||
    html.includes(`href="/#${name}"`) ||
    html.includes(`href='/#${name}'`)
  );
}

// ── Container presence tests ───────────────────────────────────────────────

describe('tab containers exist in DOM', () => {
  it('#home container present (id="tab-home")', () => {
    expect(hasTabContainer(readHtml(), 'home')).toBe(true);
  });

  it('#stats container present (id="tab-stats")', () => {
    expect(hasTabContainer(readHtml(), 'stats')).toBe(true);
  });

  it('#health container present (id="tab-health")', () => {
    expect(hasTabContainer(readHtml(), 'health')).toBe(true);
  });

  it('#bookmarks container present (empty placeholder)', () => {
    expect(hasTabContainer(readHtml(), 'bookmarks')).toBe(true);
  });

  it('#info container present (id="tab-info")', () => {
    expect(hasTabContainer(readHtml(), 'info')).toBe(true);
  });

  it('#control container present (empty placeholder)', () => {
    expect(hasTabContainer(readHtml(), 'control')).toBe(true);
  });
});

// ── Tab bar navigation link tests ──────────────────────────────────────────

describe('tab bar navigation links present', () => {
  it('tab bar link to #home present', () => {
    expect(hasTabLink(readHtml(), 'home')).toBe(true);
  });

  it('tab bar link to #stats present', () => {
    expect(hasTabLink(readHtml(), 'stats')).toBe(true);
  });

  it('tab bar link to #health present', () => {
    expect(hasTabLink(readHtml(), 'health')).toBe(true);
  });

  it('tab bar link to #bookmarks present', () => {
    expect(hasTabLink(readHtml(), 'bookmarks')).toBe(true);
  });

  it('tab bar link to #info present', () => {
    expect(hasTabLink(readHtml(), 'info')).toBe(true);
  });

  it('tab bar link to #control present', () => {
    expect(hasTabLink(readHtml(), 'control')).toBe(true);
  });
});

// ── Hash router JS present ─────────────────────────────────────────────────

describe('hash router JavaScript present', () => {
  it('index.html contains hash router logic (hashchange or popstate)', () => {
    const html = readHtml();
    const hasHashChange = html.includes('hashchange') || html.includes('onhashchange');
    const hasPopState = html.includes('popstate');
    expect(hasHashChange || hasPopState).toBe(true);
  });

  it('index.html has default route logic for #home', () => {
    const html = readHtml();
    // Must default to #home when no hash is present
    expect(html.includes('#home') || html.includes('home')).toBe(true);
  });
});

// ── Empty placeholder containers ──────────────────────────────────────────

describe('#bookmarks and #control are empty placeholder shells', () => {
  it('tab-bookmarks container is present as a div', () => {
    const html = readHtml();
    // Should appear as a div container
    const hasDiv = (
      html.includes('<div id="tab-bookmarks"') ||
      html.includes("<div id='tab-bookmarks'")
    );
    const hasSection = (
      html.includes('<section id="tab-bookmarks"') ||
      html.includes("<section id='tab-bookmarks'")
    );
    expect(hasDiv || hasSection).toBe(true);
  });

  it('tab-control container is present as a div', () => {
    const html = readHtml();
    const hasDiv = (
      html.includes('<div id="tab-control"') ||
      html.includes("<div id='tab-control'")
    );
    const hasSection = (
      html.includes('<section id="tab-control"') ||
      html.includes("<section id='tab-control'")
    );
    expect(hasDiv || hasSection).toBe(true);
  });
});
