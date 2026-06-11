/**
 * diff-view.test.js — v5.50.1 DiffView component behavioral tests
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { classifyDiffLines, DiffView } from './diff-view.js';

// ── classifyDiffLines ─────────────────────────────────────────────────────────

describe('classifyDiffLines', () => {
  it('returns empty arrays for empty input', () => {
    const { left, right } = classifyDiffLines('');
    expect(left).toEqual([]);
    expect(right).toEqual([]);
  });

  it('returns empty arrays for null input', () => {
    const { left, right } = classifyDiffLines(null);
    expect(left).toEqual([]);
    expect(right).toEqual([]);
  });

  it('classifies --- line as hdr on both sides', () => {
    const { left, right } = classifyDiffLines('--- a/file\n+++ b/file');
    expect(left[0].type).toBe('hdr');
    expect(right[0].type).toBe('hdr');
  });

  it('classifies +++ line as hdr on both sides', () => {
    const { left, right } = classifyDiffLines('+++ b/file');
    expect(left[0].type).toBe('hdr');
    expect(right[0].type).toBe('hdr');
  });

  it('classifies @@ line as hdr on both sides', () => {
    const { left, right } = classifyDiffLines('@@ -1,3 +1,3 @@');
    expect(left[0].type).toBe('hdr');
    expect(right[0].type).toBe('hdr');
  });

  it('classifies - line as del on left, placeholder (ctx) on right', () => {
    const { left, right } = classifyDiffLines('-removed line');
    expect(left[0].type).toBe('del');
    expect(left[0].text).toBe('-removed line');
    expect(right[0].type).toBe('ctx');
    expect(right[0].text).toBe('');
  });

  it('classifies + line as placeholder (ctx) on left, add on right', () => {
    const { left, right } = classifyDiffLines('+added line');
    expect(left[0].type).toBe('ctx');
    expect(left[0].text).toBe('');
    expect(right[0].type).toBe('add');
    expect(right[0].text).toBe('+added line');
  });

  it('classifies context line on both sides', () => {
    const { left, right } = classifyDiffLines(' unchanged line');
    expect(left[0].type).toBe('ctx');
    expect(right[0].type).toBe('ctx');
    expect(left[0].text).toBe(' unchanged line');
  });

  it('processes a realistic diff correctly', () => {
    const diff = [
      '--- a/wiki',
      '+++ b/wiki',
      '@@ -1,3 +1,3 @@',
      ' context line',
      '-old line',
      '+new line',
      ' another context',
    ].join('\n');

    const { left, right } = classifyDiffLines(diff);

    // Line counts must match (same length for synced scroll)
    expect(left.length).toBe(right.length);

    // Context line on both sides
    expect(left[3].type).toBe('ctx');
    expect(right[3].type).toBe('ctx');

    // Del line on left, placeholder on right
    expect(left[4].type).toBe('del');
    expect(right[4].type).toBe('ctx');

    // Add line on right, placeholder on left
    expect(left[5].type).toBe('ctx');
    expect(right[5].type).toBe('add');
  });

  it('left and right arrays are always same length', () => {
    const diff = '--- a\n+++ b\n@@ -1 +1 @@\n-del\n+add\n context';
    const { left, right } = classifyDiffLines(diff);
    expect(left.length).toBe(right.length);
  });
});

// ── DiffView component ────────────────────────────────────────────────────────

describe('DiffView component', () => {
  let container;
  let onClose;
  let view;

  const VERSIONS = [
    { version: 2, created_at: new Date().toISOString() },
    { version: 1, created_at: new Date(Date.now() - 3600000).toISOString() },
  ];

  const DIFF_DATA = {
    v1: 1,
    v2: 2,
    slug: 'test-page',
    diff: '--- a/test-page\n+++ b/test-page\n@@ -1 +1 @@\n-old content\n+new content',
  };

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    onClose = vi.fn();
    view = new DiffView({ container, onClose });
  });

  afterEach(() => {
    document.body.removeChild(container);
  });

  it('renders diff headers and body', () => {
    expect(container.querySelector('.bm-diff-headers')).toBeTruthy();
    expect(container.querySelector('.bm-diff-body')).toBeTruthy();
  });

  it('renders two panes', () => {
    const panes = container.querySelectorAll('.bm-diff-pane');
    expect(panes.length).toBe(2);
  });

  it('show() populates column headers with version labels', () => {
    view.show(DIFF_DATA, VERSIONS);
    const headers = container.querySelectorAll('.bm-diff-col-header');
    expect(headers[0].textContent).toContain('v1');
    expect(headers[1].textContent).toContain('v2');
  });

  it('show() renders del lines in left pane', () => {
    view.show(DIFF_DATA, VERSIONS);
    const delLines = container.querySelectorAll('.bm-diff-pane:first-child .bm-diff-line.del');
    expect(delLines.length).toBeGreaterThan(0);
  });

  it('show() renders add lines in right pane', () => {
    view.show(DIFF_DATA, VERSIONS);
    const addLines = container.querySelectorAll('.bm-diff-pane:last-child .bm-diff-line.add');
    expect(addLines.length).toBeGreaterThan(0);
  });

  it('del lines use textContent not innerHTML (no XSS)', () => {
    const xssData = {
      ...DIFF_DATA,
      diff: '-<script>alert(1)</script>',
    };
    view.show(xssData, VERSIONS);
    const leftPane = container.querySelector('.bm-diff-pane:first-child');
    // The script tag should appear as literal text, not be executed
    const delLine = leftPane.querySelector('.bm-diff-line.del');
    if (delLine) {
      // textContent should include the raw string, not execute it
      expect(delLine.textContent).toContain('<script>');
    }
  });

  it('clear() removes all lines from panes', () => {
    view.show(DIFF_DATA, VERSIONS);
    view.clear();
    const panes = container.querySelectorAll('.bm-diff-pane');
    panes.forEach(p => expect(p.children.length).toBe(0));
  });

  it('left and right panes have same number of lines (synced scroll)', () => {
    view.show(DIFF_DATA, VERSIONS);
    const panes = container.querySelectorAll('.bm-diff-pane');
    expect(panes[0].children.length).toBe(panes[1].children.length);
  });
});
