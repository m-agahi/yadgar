/**
 * versions-rail.test.js — v5.50.1 VersionsRail component behavioral tests
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  applySelection,
  relTime,
  computeSparklineValues,
  VersionsRail,
} from './versions-rail.js';

// ── applySelection ────────────────────────────────────────────────────────────

describe('applySelection', () => {
  const versions = [{ version: 4 }, { version: 3 }, { version: 2 }, { version: 1 }];

  it('plain click selects only clicked index', () => {
    const result = applySelection(versions, 2, false, []);
    expect(result).toEqual([2]);
  });

  it('plain click replaces previous selection', () => {
    const result = applySelection(versions, 1, false, [0, 3]);
    expect(result).toEqual([1]);
  });

  it('shift-click with existing anchor selects range', () => {
    // anchor at 0, clicked at 2 → select [0, 1, 2]
    const result = applySelection(versions, 2, true, [0]);
    expect(result).toEqual([0, 1, 2]);
  });

  it('shift-click range is always sorted ascending', () => {
    // anchor at 3, clicked at 1 → select [1, 2, 3]
    const result = applySelection(versions, 1, true, [3]);
    expect(result).toEqual([1, 2, 3]);
  });

  it('shift-click with empty selection falls back to single select', () => {
    const result = applySelection(versions, 2, true, []);
    expect(result).toEqual([2]);
  });

  it('selecting index 0 works', () => {
    expect(applySelection(versions, 0, false, [2])).toEqual([0]);
  });

  it('selecting last index works', () => {
    expect(applySelection(versions, 3, false, [])).toEqual([3]);
  });
});

// ── relTime ───────────────────────────────────────────────────────────────────

describe('relTime', () => {
  const BASE = 1_000_000_000_000; // fixed "now"

  it('returns empty string for null', () => {
    expect(relTime(null, BASE)).toBe('');
  });

  it('returns empty string for invalid date', () => {
    expect(relTime('not-a-date', BASE)).toBe('');
  });

  it('formats seconds', () => {
    const iso = new Date(BASE - 30_000).toISOString();
    expect(relTime(iso, BASE)).toBe('30s');
  });

  it('formats minutes', () => {
    const iso = new Date(BASE - 5 * 60_000).toISOString();
    expect(relTime(iso, BASE)).toBe('5m');
  });

  it('formats hours', () => {
    const iso = new Date(BASE - 3 * 3_600_000).toISOString();
    expect(relTime(iso, BASE)).toBe('3h');
  });

  it('formats days', () => {
    const iso = new Date(BASE - 2 * 86_400_000).toISOString();
    expect(relTime(iso, BASE)).toBe('2d');
  });

  it('returns just now for future timestamps', () => {
    const iso = new Date(BASE + 5000).toISOString();
    expect(relTime(iso, BASE)).toBe('just now');
  });
});

// ── computeSparklineValues ────────────────────────────────────────────────────

describe('computeSparklineValues', () => {
  it('returns values in [0, 8] range', () => {
    const versions = [
      { size_bytes: 100 },
      { size_bytes: 500 },
      { size_bytes: 250 },
    ];
    const vals = computeSparklineValues(versions);
    for (const v of vals) {
      expect(v).toBeGreaterThanOrEqual(0);
      expect(v).toBeLessThanOrEqual(8);
    }
  });

  it('max size maps to 8', () => {
    const versions = [{ size_bytes: 50 }, { size_bytes: 200 }];
    const vals = computeSparklineValues(versions);
    expect(vals[1]).toBe(8); // 200 is max → 8
  });

  it('handles null size_bytes as 0', () => {
    const versions = [{ size_bytes: null }, { size_bytes: 100 }];
    const vals = computeSparklineValues(versions);
    expect(vals[0]).toBe(0);
  });

  it('single version maps to 8', () => {
    const vals = computeSparklineValues([{ size_bytes: 300 }]);
    expect(vals[0]).toBe(8);
  });
});

// ── VersionsRail component ────────────────────────────────────────────────────

describe('VersionsRail component', () => {
  let container;
  let onVersionClick;
  let onSelectionChange;
  let onCompare;
  let onRestore;
  let rail;

  const VERSIONS = [
    { version: 3, created_at: new Date().toISOString(), change_summary: 'v3 update', size_bytes: 300 },
    { version: 2, created_at: new Date(Date.now() - 3600000).toISOString(), change_summary: 'v2 update', size_bytes: 200 },
    { version: 1, created_at: new Date(Date.now() - 7200000).toISOString(), change_summary: 'initial', size_bytes: 100 },
  ];

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    onVersionClick = vi.fn();
    onSelectionChange = vi.fn();
    onCompare = vi.fn();
    onRestore = vi.fn();
    rail = new VersionsRail({ container, onVersionClick, onSelectionChange, onCompare, onRestore });
    rail.setVersions(VERSIONS, 3);
  });

  afterEach(() => {
    document.body.removeChild(container);
  });

  it('renders a lozenge for each version', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    expect(lozenges.length).toBe(3);
  });

  it('current version lozenge has .current class', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    expect(lozenges[0].classList.contains('current')).toBe(true);
  });

  it('non-current lozenges do not have .current class', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    expect(lozenges[1].classList.contains('current')).toBe(false);
    expect(lozenges[2].classList.contains('current')).toBe(false);
  });

  it('clicking a lozenge calls onVersionClick with that version', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    lozenges[1].click();
    expect(onVersionClick).toHaveBeenCalledWith(VERSIONS[1], 1);
  });

  it('clicking a lozenge calls onSelectionChange', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    lozenges[2].click();
    expect(onSelectionChange).toHaveBeenCalledWith([2]);
  });

  it('compare button is disabled with single selection', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    lozenges[0].click();
    const compareBtn = container.querySelector('.bm-versions-btn');
    expect(compareBtn.disabled).toBe(true);
  });

  it('compare button enabled after shift-selecting two versions', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    // First click: select index 0
    lozenges[0].click();
    // Shift-click index 1: selects range [0,1]
    lozenges[1].dispatchEvent(new MouseEvent('click', { shiftKey: true, bubbles: true }));
    const compareBtn = container.querySelector('.bm-versions-btn');
    expect(compareBtn.disabled).toBe(false);
  });

  it('clicking compare button calls onCompare', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    lozenges[0].click();
    lozenges[1].dispatchEvent(new MouseEvent('click', { shiftKey: true, bubbles: true }));
    const buttons = container.querySelectorAll('.bm-versions-btn');
    const compareBtn = Array.from(buttons).find(b => b.textContent.includes('compare'));
    compareBtn.click();
    expect(onCompare).toHaveBeenCalled();
  });

  it('restore button disabled for current version', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    lozenges[0].click(); // current version (v3)
    const buttons = container.querySelectorAll('.bm-versions-btn');
    const restoreBtn = Array.from(buttons).find(b => b.textContent.includes('restore'));
    expect(restoreBtn.disabled).toBe(true);
  });

  it('restore button enabled for non-current version', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    lozenges[2].click(); // v1 — not current
    const buttons = container.querySelectorAll('.bm-versions-btn');
    const restoreBtn = Array.from(buttons).find(b => b.textContent.includes('restore'));
    expect(restoreBtn.disabled).toBe(false);
  });

  it('cycleVersion(1) moves selection forward', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    lozenges[0].click(); // select index 0
    rail.cycleVersion(1);
    expect(rail.selection).toEqual([1]);
    expect(onVersionClick).toHaveBeenCalledTimes(2);
  });

  it('cycleVersion(-1) moves selection backward and clamps at 0', () => {
    const lozenges = container.querySelectorAll('.bm-version-lozenge');
    lozenges[0].click();
    rail.cycleVersion(-1); // already at 0, should stay
    expect(rail.selection).toEqual([0]);
  });
});
