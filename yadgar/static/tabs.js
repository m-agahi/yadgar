/**
 * tabs.js — Hash router for the yadgar viz SPA.
 *
 * Extracted from index.html (v5.50.0) for unit testing.
 * Behavior-preserving: no new features.
 *
 * Exports:
 *   VALID_TABS   — Set<string> of recognised tab names
 *   resolveTab   — pure: hash string → tab name (with fallback)
 *   switchTab    — DOM: activate pane + link, hide others
 */

/** @type {Set<string>} */
export const VALID_TABS = new Set(['home', 'stats', 'health', 'bookmarks', 'info', 'control']);

/**
 * Resolve a window.location.hash value to a tab name.
 * Strips leading #, takes the first path segment, falls back to 'home'.
 *
 * @param {string} hash - e.g. '' | '#home' | '#stats/detail'
 * @returns {string} valid tab name
 */
export function resolveTab(hash) {
  const name = (hash || '#home').replace(/^#/, '').split('/')[0];
  return VALID_TABS.has(name) ? name : 'home';
}

/**
 * Activate a tab pane and its nav link; deactivate all others.
 * Falls back to 'home' for unknown names.
 * Does NOT trigger any per-tab data fetch — callers do that.
 *
 * @param {string} tabName
 */
export function switchTab(tabName) {
  if (!VALID_TABS.has(tabName)) tabName = 'home';
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('#tab-bar .tab-link').forEach(a => a.classList.remove('active'));
  const pane = document.getElementById('tab-' + tabName);
  if (pane) pane.classList.add('active');
  const link = document.querySelector('#tab-bar a[data-tab="' + tabName + '"]');
  if (link) link.classList.add('active');
}
