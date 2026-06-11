/**
 * overlays.js — Overlay state persistence helpers.
 *
 * Pure functions for reading/writing overlay position and collapse state
 * to a localStorage-compatible interface.
 *
 * Key schema:
 *   viz.overlay.<name>.position  → JSON {x, y}
 *   viz.overlay.<name>.collapsed → string 'true' | 'false'
 *
 * Exports:
 *   loadOverlayState    — read position + collapsed from storage with fallbacks
 *   saveOverlayPosition — persist {x, y} for a named overlay
 *   saveOverlayCollapsed — persist collapsed boolean for a named overlay
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
