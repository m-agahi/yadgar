/**
 * overlays.js — Overlay state persistence helpers + DOM behavior.
 *
 * Pure functions for reading/writing overlay position and collapse state
 * to a localStorage-compatible interface.
 *
 * Key schema:
 *   viz.overlay.<name>.position  → JSON {x, y}
 *   viz.overlay.<name>.collapsed → string 'true' | 'false'
 *
 * Exports (pure helpers):
 *   loadOverlayState    — read position + collapsed from storage with fallbacks
 *   saveOverlayPosition — persist {x, y} for a named overlay
 *   saveOverlayCollapsed — persist collapsed boolean for a named overlay
 *
 * Exports (DOM behavior):
 *   initOverlays({ canvasSelector, fadeDebounceMs, storage })
 *     — find .floating-overlay elements; wire drag, collapse, pointer-events,
 *       auto-fade. Must be called after DOMContentLoaded.
 */

/**
 * Load persisted overlay state, falling back to provided defaults on any error.
 *
 * @param {Storage} storage - localStorage-compatible object
 * @param {string}  name    - overlay identifier (e.g. 'heat-slider')
 * @param {{ position: {x:number,y:number}, collapsed?: boolean }} defaults
 * @returns {{ position: {x:number,y:number}, collapsed: boolean }}
 */
export function loadOverlayState(storage, name, defaults) {
  const posKey = `viz.overlay.${name}.position`;
  const colKey = `viz.overlay.${name}.collapsed`;
  let position = defaults.position;
  let collapsed = defaults.collapsed ?? false;

  try {
    const raw = storage.getItem(posKey);
    if (raw !== null) {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed.x === 'number' && typeof parsed.y === 'number') {
        position = parsed;
      }
    }
  } catch {
    // corrupt JSON → fall back to defaults
  }

  try {
    const rawCol = storage.getItem(colKey);
    if (rawCol !== null) {
      collapsed = rawCol === 'true';
    }
  } catch {
    // ignore
  }

  return { position, collapsed };
}

/**
 * Persist overlay position to storage.
 *
 * @param {Storage} storage
 * @param {string}  name
 * @param {number}  x
 * @param {number}  y
 */
export function saveOverlayPosition(storage, name, x, y) {
  storage.setItem(`viz.overlay.${name}.position`, JSON.stringify({ x, y }));
}

/**
 * Persist overlay collapsed state to storage.
 *
 * @param {Storage} storage
 * @param {string}  name
 * @param {boolean} collapsed
 */
export function saveOverlayCollapsed(storage, name, collapsed) {
  storage.setItem(`viz.overlay.${name}.collapsed`, String(collapsed));
}

// ── Default position table ─────────────────────────────────────────────────
// Fallback positions for named overlays on first load (no localStorage entry).
const DEFAULT_POSITIONS = {
  'heat-slider':  { x: 16,  y: 16  },
  'graph-stats':  { x: 220, y: 16  },
  'node-types':   { x: 16,  y: 200 },
  'edge-legend':  { x: 16,  y: 400 },
  'clusters':     { x: 220, y: 200 },
};

/**
 * Wire floating overlay DOM behavior: drag-reposition, collapse toggle,
 * pointer-events rules, and canvas-interaction auto-fade.
 *
 * Finds all `.floating-overlay[data-overlay-name]` elements in the document.
 * Safe to call multiple times; each call re-wires fresh (old listeners on
 * replaced DOM are naturally garbage-collected).
 *
 * @param {{ canvasSelector: string, fadeDebounceMs?: number, storage?: Storage }} opts
 *   canvasSelector  — CSS selector for the graph canvas wrapper (e.g. '#canvas-wrap')
 *   fadeDebounceMs  — ms after last canvas interaction before opacity restores (default 200)
 *   storage         — localStorage-compatible; defaults to window.localStorage
 */
export function initOverlays({ canvasSelector, fadeDebounceMs = 200, storage } = {}) {
  const store = storage ?? (typeof window !== 'undefined' ? window.localStorage : null);
  const overlays = Array.from(document.querySelectorAll('.floating-overlay[data-overlay-name]'));
  if (!overlays.length) return;

  // ── 1. Restore position + collapsed state ────────────────────────────────
  for (const ov of overlays) {
    const name = ov.dataset.overlayName;
    const defPos = DEFAULT_POSITIONS[name] ?? { x: 0, y: 0 };
    const state = store
      ? loadOverlayState(store, name, { position: defPos, collapsed: false })
      : { position: defPos, collapsed: false };

    ov.style.left = state.position.x + 'px';
    ov.style.top  = state.position.y + 'px';

    if (state.collapsed) ov.classList.add('collapsed');
    else                 ov.classList.remove('collapsed');
  }

  // ── 2. Pointer-events: body=none, grip+controls=auto ────────────────────
  for (const ov of overlays) {
    const grip    = ov.querySelector('.overlay-grip');
    const colBtn  = ov.querySelector('.overlay-collapse');
    const body    = ov.querySelector('.overlay-body');

    if (body)   body.style.pointerEvents   = 'none';
    if (grip)   grip.style.pointerEvents   = 'auto';
    if (colBtn) colBtn.style.pointerEvents = 'auto';
  }

  // ── 3. Drag-reposition via .overlay-grip ─────────────────────────────────
  for (const ov of overlays) {
    const name = ov.dataset.overlayName;
    const grip = ov.querySelector('.overlay-grip');
    if (!grip) continue;

    let dragging = false;
    let startClientX = 0, startClientY = 0;
    let startLeft    = 0, startTop     = 0;

    grip.addEventListener('pointerdown', e => {
      // Only handle direct grip events, not collapse button clicks
      if (e.target && e.target.classList && e.target.classList.contains('overlay-collapse')) return;

      dragging     = true;
      startClientX = e.clientX;
      startClientY = e.clientY;
      startLeft    = parseFloat(ov.style.left)  || 0;
      startTop     = parseFloat(ov.style.top)   || 0;

      // Capture pointer if supported (no-throw in jsdom)
      try { grip.setPointerCapture(e.pointerId); } catch (_) {}

      e.preventDefault();
    });

    document.addEventListener('pointermove', e => {
      if (!dragging) return;
      const dx = e.clientX - startClientX;
      const dy = e.clientY - startClientY;
      ov.style.left = (startLeft + dx) + 'px';
      ov.style.top  = (startTop  + dy) + 'px';
    });

    document.addEventListener('pointerup', e => {
      if (!dragging) return;
      dragging = false;

      const x = parseFloat(ov.style.left)  || 0;
      const y = parseFloat(ov.style.top)   || 0;
      if (store) saveOverlayPosition(store, name, x, y);

      try { grip.releasePointerCapture(e.pointerId); } catch (_) {}
    });
  }

  // ── 4. Collapse toggle via .overlay-collapse ─────────────────────────────
  for (const ov of overlays) {
    const name   = ov.dataset.overlayName;
    const colBtn = ov.querySelector('.overlay-collapse');
    if (!colBtn) continue;

    colBtn.addEventListener('click', e => {
      e.stopPropagation();
      const nowCollapsed = !ov.classList.contains('collapsed');
      ov.classList.toggle('collapsed', nowCollapsed);
      if (store) saveOverlayCollapsed(store, name, nowCollapsed);
    });
  }

  // ── 5. Auto-fade on canvas interaction ───────────────────────────────────
  const canvasEl = canvasSelector ? document.querySelector(canvasSelector) : null;
  if (!canvasEl) return;

  let _fadeTimer = null;

  function _startFade() {
    for (const ov of overlays) ov.style.opacity = '0.3';
    if (_fadeTimer) clearTimeout(_fadeTimer);
    _fadeTimer = setTimeout(() => {
      for (const ov of overlays) ov.style.opacity = '1';
      _fadeTimer = null;
    }, fadeDebounceMs);
  }

  // Capture phase so ForceGraph3D/OrbitControls stopPropagation doesn't swallow
  canvasEl.addEventListener('pointerdown', _startFade, true);
  canvasEl.addEventListener('wheel',       _startFade, true);
}
