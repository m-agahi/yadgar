/**
 * overlays_behavior.test.js — v5.50.0 floating overlays DOM behavioral tests
 *
 * Tests initOverlays(): drag-reposition, collapse toggle, localStorage persist,
 * restore on init, pointer-events rules, auto-fade on canvas interaction.
 *
 * Run: cd viz-tests && npx vitest run
 */

import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import {
  initOverlays,
  loadOverlayState,
  saveOverlayPosition,
  saveOverlayCollapsed,
} from './overlays.js';

// ── Helpers ───────────────────────────────────────────────────────────────────

function makeLocalStorage() {
  const store = {};
  return {
    getItem:    (k)    => (k in store ? store[k] : null),
    setItem:    (k, v) => { store[k] = String(v); },
    removeItem: (k)    => { delete store[k]; },
    clear:      ()     => { Object.keys(store).forEach(k => delete store[k]); },
  };
}

/**
 * Build a minimal DOM: one #canvas-wrap plus N .floating-overlay elements,
 * each with a .overlay-grip child and a .overlay-collapse button.
 * Returns { canvas, overlays }.
 *
 * Use names that are NOT in DEFAULT_POSITIONS (e.g. 'test-a', 'test-b') to
 * ensure overlays start at 0,0 for predictable drag-position assertions.
 */
function makeOverlayDOM(names = ['test-a', 'test-b']) {
  document.body.innerHTML = '';

  const canvas = document.createElement('div');
  canvas.id = 'canvas-wrap';
  document.body.appendChild(canvas);

  const overlays = names.map(name => {
    const wrap = document.createElement('div');
    wrap.className = 'floating-overlay';
    wrap.dataset.overlayName = name;
    wrap.style.position = 'absolute';
    wrap.style.left = '0px';
    wrap.style.top = '0px';

    const grip = document.createElement('div');
    grip.className = 'overlay-grip';
    grip.textContent = '⋮ ' + name;

    const collapseBtn = document.createElement('button');
    collapseBtn.className = 'overlay-collapse';
    collapseBtn.textContent = '−';

    const body = document.createElement('div');
    body.className = 'overlay-body';
    body.textContent = 'content';

    grip.appendChild(collapseBtn);
    wrap.appendChild(grip);
    wrap.appendChild(body);
    document.body.appendChild(wrap);
    return wrap;
  });

  return { canvas, overlays };
}

function firePointer(el, type, x = 0, y = 0, extra = {}) {
  el.dispatchEvent(new PointerEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX: x,
    clientY: y,
    pointerId: 1,
    ...extra,
  }));
}

function fireWheel(el) {
  el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: -100 }));
}

// ── Drag: position changes ────────────────────────────────────────────────────

describe('drag: pointerdown→move→up changes element position', () => {
  let ls;
  beforeEach(() => {
    ls = makeLocalStorage();
    makeOverlayDOM();
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });
  });

  it('moves overlay left/top via style after drag', () => {
    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    const grip = overlay.querySelector('.overlay-grip');

    // Start drag from (50, 60)
    firePointer(grip, 'pointerdown', 50, 60);
    // Move to (120, 150) — delta (+70, +90)
    firePointer(document, 'pointermove', 120, 150);
    firePointer(document, 'pointerup', 120, 150);

    const left = parseFloat(overlay.style.left);
    const top  = parseFloat(overlay.style.top);
    expect(left).toBeCloseTo(70, 0);
    expect(top).toBeCloseTo(90, 0);
  });

  it('does not move overlay when drag starts outside grip', () => {
    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    const body = overlay.querySelector('.overlay-body');

    firePointer(body, 'pointerdown', 50, 60);
    firePointer(document, 'pointermove', 200, 200);
    firePointer(document, 'pointerup', 200, 200);

    // Position should remain at default
    expect(overlay.style.left).toBe('0px');
    expect(overlay.style.top).toBe('0px');
  });

  it('drag stops after pointerup — further moves ignored', () => {
    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    const grip = overlay.querySelector('.overlay-grip');

    firePointer(grip, 'pointerdown', 0, 0);
    firePointer(document, 'pointermove', 30, 40);
    firePointer(document, 'pointerup', 30, 40);
    // Extra move after up
    firePointer(document, 'pointermove', 999, 999);

    const left = parseFloat(overlay.style.left);
    const top  = parseFloat(overlay.style.top);
    expect(left).toBeCloseTo(30, 0);
    expect(top).toBeCloseTo(40, 0);
  });
});

// ── Drag: position persisted ──────────────────────────────────────────────────

describe('drag: position persisted to localStorage', () => {
  let ls;
  beforeEach(() => {
    ls = makeLocalStorage();
    makeOverlayDOM();
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });
  });

  it('persists position on pointerup', () => {
    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    const grip = overlay.querySelector('.overlay-grip');

    firePointer(grip, 'pointerdown', 0, 0);
    firePointer(document, 'pointermove', 55, 77);
    firePointer(document, 'pointerup', 55, 77);

    const state = loadOverlayState(ls, 'test-a', { position: { x: 0, y: 0 } });
    expect(state.position.x).toBeCloseTo(55, 0);
    expect(state.position.y).toBeCloseTo(77, 0);
  });
});

// ── Reload: position restored ─────────────────────────────────────────────────

describe('reload: restores position from localStorage', () => {
  it('restores saved position on initOverlays', () => {
    const ls = makeLocalStorage();
    saveOverlayPosition(ls, 'test-a', 200, 300);

    makeOverlayDOM();
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });

    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    expect(parseFloat(overlay.style.left)).toBeCloseTo(200, 0);
    expect(parseFloat(overlay.style.top)).toBeCloseTo(300, 0);
  });

  it('corrupt localStorage falls back to default position — no throw', () => {
    const ls = makeLocalStorage();
    ls.setItem('viz.overlay.test-a.position', '{ NOT JSON }');

    let overlay;
    expect(() => {
      makeOverlayDOM();
      initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });
      overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    }).not.toThrow();

    // Falls back to DEFAULT_POSITIONS (no entry for 'test-a', so {x:0,y:0}) — not the corrupt value
    const left = parseFloat(overlay.style.left);
    const top  = parseFloat(overlay.style.top);
    // Should be a defined number, not NaN, and not the corrupt value
    expect(Number.isNaN(left)).toBe(false);
    expect(Number.isNaN(top)).toBe(false);
  });
});

// ── Collapse: toggle + persist ────────────────────────────────────────────────

describe('collapse: toggle adds class + persists', () => {
  let ls;
  beforeEach(() => {
    ls = makeLocalStorage();
    makeOverlayDOM();
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });
  });

  it('clicking [−] adds .collapsed class to overlay', () => {
    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    const btn = overlay.querySelector('.overlay-collapse');

    btn.click();

    expect(overlay.classList.contains('collapsed')).toBe(true);
  });

  it('clicking [−] twice toggles back to uncollapsed', () => {
    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    const btn = overlay.querySelector('.overlay-collapse');

    btn.click();
    btn.click();

    expect(overlay.classList.contains('collapsed')).toBe(false);
  });

  it('collapse state persisted to localStorage', () => {
    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    const btn = overlay.querySelector('.overlay-collapse');

    btn.click();

    const state = loadOverlayState(ls, 'test-a', { position: { x: 0, y: 0 } });
    expect(state.collapsed).toBe(true);
  });
});

// ── Reload: collapse restored ─────────────────────────────────────────────────

describe('reload: restores collapsed state from localStorage', () => {
  it('restores collapsed=true on initOverlays', () => {
    const ls = makeLocalStorage();
    saveOverlayCollapsed(ls, 'test-a', true);

    makeOverlayDOM();
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });

    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    expect(overlay.classList.contains('collapsed')).toBe(true);
  });

  it('restores collapsed=false on initOverlays', () => {
    const ls = makeLocalStorage();
    saveOverlayCollapsed(ls, 'test-a', false);

    makeOverlayDOM();
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });

    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    expect(overlay.classList.contains('collapsed')).toBe(false);
  });
});

// ── Pointer-events rules ──────────────────────────────────────────────────────

describe('pointer-events: body=none, grip+controls=auto', () => {
  let ls;
  beforeEach(() => {
    ls = makeLocalStorage();
    makeOverlayDOM();
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });
  });

  it('overlay body has pointer-events: none', () => {
    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    const body = overlay.querySelector('.overlay-body');
    expect(body.style.pointerEvents).toBe('none');
  });

  it('overlay grip has pointer-events: auto', () => {
    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    const grip = overlay.querySelector('.overlay-grip');
    expect(grip.style.pointerEvents).toBe('auto');
  });

  it('collapse button has pointer-events: auto', () => {
    const overlay = document.querySelector('.floating-overlay[data-overlay-name="test-a"]');
    const btn = overlay.querySelector('.overlay-collapse');
    expect(btn.style.pointerEvents).toBe('auto');
  });
});

// ── Auto-fade: opacity drops on canvas pointerdown ────────────────────────────

describe('auto-fade: opacity drops on canvas interaction, restores after debounce', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('overlay opacity drops to ~0.3 on canvas pointerdown', () => {
    const ls = makeLocalStorage();
    const { canvas, overlays } = makeOverlayDOM();
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });

    firePointer(canvas, 'pointerdown', 400, 300);

    const opacity = parseFloat(overlays[0].style.opacity);
    expect(opacity).toBeLessThanOrEqual(0.35);
    expect(opacity).toBeGreaterThan(0);
  });

  it('overlay opacity drops on canvas wheel event', () => {
    const ls = makeLocalStorage();
    const { canvas, overlays } = makeOverlayDOM();
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });

    fireWheel(canvas);

    const opacity = parseFloat(overlays[0].style.opacity);
    expect(opacity).toBeLessThanOrEqual(0.35);
  });

  it('overlay opacity restores to 1.0 after debounce elapsed', () => {
    const ls = makeLocalStorage();
    const { canvas, overlays } = makeOverlayDOM();
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });

    firePointer(canvas, 'pointerdown');

    // Advance past debounce
    vi.advanceTimersByTime(250);

    expect(parseFloat(overlays[0].style.opacity)).toBeCloseTo(1.0, 1);
  });

  it('debounce resets on repeated canvas events', () => {
    const ls = makeLocalStorage();
    const { canvas, overlays } = makeOverlayDOM();
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });

    firePointer(canvas, 'pointerdown');
    vi.advanceTimersByTime(100);

    // Second event before debounce fires
    firePointer(canvas, 'pointerdown');
    vi.advanceTimersByTime(100);

    // Only 100ms since last event — still faded
    expect(parseFloat(overlays[0].style.opacity)).toBeLessThanOrEqual(0.35);

    // Now full debounce elapses
    vi.advanceTimersByTime(150);
    expect(parseFloat(overlays[0].style.opacity)).toBeCloseTo(1.0, 1);
  });

  it('all overlays fade and restore together', () => {
    const ls = makeLocalStorage();
    const { canvas, overlays } = makeOverlayDOM(['test-a', 'test-b']);
    initOverlays({ canvasSelector: '#canvas-wrap', fadeDebounceMs: 200, storage: ls });

    firePointer(canvas, 'pointerdown');

    overlays.forEach(ov => {
      expect(parseFloat(ov.style.opacity)).toBeLessThanOrEqual(0.35);
    });

    vi.advanceTimersByTime(250);

    overlays.forEach(ov => {
      expect(parseFloat(ov.style.opacity)).toBeCloseTo(1.0, 1);
    });
  });
});
