/**
 * traces-replay.js — PURE replay logic for the viz "Traces" tab (Car B).
 *
 * No DOM, no fetch, no globals. Every export is a pure function or a small
 * state helper so the layout / timeline / scrub math is unit-testable under
 * vitest (jsdom) without a browser render harness (repo convention).
 *
 * Consumes the backend mesh payload from GET /api/traces/{id}/mesh:
 *   { nodes:[{id,label,svc,lane,rel_ms,dur_ms,storm_n,error,type,note}],
 *     edges:[{src,dst,order}], timeline_ms, tool, dropped_boundary, trace_id }
 *
 * The mesh nodes are ALREADY the simplify_trace-selected stages (server-side,
 * yadgar/_shared/trace_mesh.py) — this module does NOT re-run STAGE_DEFS
 * matching (that was mockup-only). It computes the fixed-lane geometry, the
 * per-stage dwell timeline, the scrub/playhead mapping, and the play clock.
 */

// ── geometry ────────────────────────────────────────────────────────────────

export const LANE_Y = { core: 150, backend: 318 };
export const MESH = { w: 1180, h: 430, x0: 80, x1: 1108, r: 15 };
// Lane band (item-2): each lane owns a vertical band centred on LANE_Y[lane];
// nodes force-separate WITHIN their band so circles + labels don't overlap.
// half (50) keeps a clear gap around the divider midline (~y234); minGapX (90)
// ≈ label width — nodes closer than this in x get pushed apart in y.
export const LANE_BAND = { half: 50, minGapX: 90 };
export const LANE_DIVIDER_Y = (LANE_Y.core + LANE_Y.backend) / 2; // ≈234, item-3

/**
 * Horizontal position of stage i of n along the mesh (evenly spaced).
 * n===1 → centered. Pure.
 */
export function laneX(i, n) {
  if (n <= 1) return MESH.x0 + (MESH.x1 - MESH.x0) * 0.5;
  return MESH.x0 + (MESH.x1 - MESH.x0) * (i / (n - 1));
}

/**
 * Assign fixed-lane (x, y) coordinates to each mesh node (legacy: single y-line
 * per lane). Kept for back-compat / callers that want the flat layout. Returns a
 * NEW array of {...node, x, y}. Pure.
 */
export function layoutStages(nodes) {
  const n = nodes.length;
  return nodes.map((node, i) => ({
    ...node,
    x: laneX(i, n),
    y: LANE_Y[node.lane] ?? LANE_Y.core,
  }));
}

/**
 * Build the y-offset ladder for a lane band: [0, +gap, -gap, +2gap, -2gap, …],
 * `slotN` tiers each side, every offset within ±half. Pure. Ordered so the lowest
 * index = closest to the band centre (filled first). Not exported — internal.
 */
function _bandSlots(slotN, gap, half) {
  const out = [0];
  for (let k = 1; k <= slotN; k++) {
    const off = Math.min(half, k * gap);
    out.push(off, -off);
  }
  return out;
}

/**
 * Lane-banded force-scatter layout (item-2). Replaces the single-y-per-lane
 * stacking that made every circle+label in a lane collide.
 *
 *   x = time position (rel_ms fraction across the mesh) — clustered fast stages
 *       naturally sit close; y-separation then de-overlaps them.
 *   y = force-separated WITHIN the node's lane band, centred on LANE_Y[lane],
 *       so no two same-lane nodes closer than minGapX in x share a y.
 *
 * Deterministic (no Math.random): a single relaxation pass per lane, sorted by x,
 * alternating up/down from the band centre. Nodes stay inside their band → they
 * never cross the core/backend divider. Pure — returns a NEW array of {...node,x,y}.
 *
 * Guards (ties to item-5): empty list → []; totalMs<=0 → index fallback (laneX);
 * an empty lane never crashes (its band is simply unused).
 *
 * @param {Array} nodes  mesh nodes ({rel_ms, lane, ...})
 * @param {number} totalMs  trace wall time
 * @returns {Array} new nodes with {x, y}
 */
export function scatterLayout(nodes, totalMs) {
  if (!Array.isArray(nodes) || nodes.length === 0) return [];
  const span = MESH.x1 - MESH.x0;
  const useTime = Number.isFinite(totalMs) && totalMs > 0;
  const n = nodes.length;

  // 1. x from start-time fraction (or index fallback when totalMs<=0).
  const laid = nodes.map((node, i) => {
    let x;
    if (useTime) {
      const frac = Math.max(0, Math.min(1, (Number(node.rel_ms) || 0) / totalMs));
      x = MESH.x0 + span * frac;
    } else {
      x = laneX(i, n);
    }
    const laneKey = node.lane in LANE_Y ? node.lane : 'core';
    return { ...node, x, _lane: laneKey, _idx: i };
  });

  // 2. De-overlap WITHIN each lane band. Invariant: no two same-lane nodes may be
  //    both closer than minGapX in x AND closer than the ring diameter in y.
  //    A fixed ladder of y-slots fills the band (centre, ±ring, ±2·ring … clamped
  //    to ±half). For each node in x-order we take the lowest free slot not
  //    occupied by an x-neighbour within minGapX. When the band is full (cluster
  //    denser than the band can hold), we NUDGE x rightward past the blocking
  //    neighbour so the x-gap clears minGapX — spread in time rather than stack
  //    beyond the band. Deterministic; preserves non-decreasing x.
  const RING = 2 * MESH.r; // minimum vertical separation to avoid circle overlap
  const slotN = Math.max(1, Math.floor(LANE_BAND.half / RING)); // tiers each side
  const slots = _bandSlots(slotN, RING, LANE_BAND.half); // [0, +RING, -RING, …]
  const byLane = new Map();
  for (const node of laid) {
    if (!byLane.has(node._lane)) byLane.set(node._lane, []);
    byLane.get(node._lane).push(node);
  }
  for (const [laneKey, group] of byLane) {
    const centre = LANE_Y[laneKey];
    group.sort((a, b) => a.x - b.x || a._idx - b._idx);
    const placed = []; // {x, slot}
    for (const node of group) {
      // neighbours still within minGapX at this node's current x
      let live = placed.filter((p) => node.x - p.x < LANE_BAND.minGapX);
      let used = new Set(live.map((p) => p.slot));
      let slot = slots.findIndex((_, s) => !used.has(s));
      if (slot === -1) {
        // band full: nudge x just past the nearest blocking neighbour, then the
        // centre slot is free again (that neighbour is now >minGapX away).
        const blockX = Math.max(...live.map((p) => p.x));
        node.x = blockX + LANE_BAND.minGapX;
        live = placed.filter((p) => node.x - p.x < LANE_BAND.minGapX);
        used = new Set(live.map((p) => p.slot));
        slot = slots.findIndex((_, s) => !used.has(s));
        if (slot === -1) slot = 0; // defensive; cannot happen after the nudge
      }
      node.y = centre + slots[slot];
      placed.push({ x: node.x, slot });
    }
  }

  // 3. strip scratch fields, preserve input order.
  return laid.map((node) => {
    const { _lane, _idx, ...rest } = node;
    return rest;
  });
}

// ── timeline ────────────────────────────────────────────────────────────────

/**
 * Compute the per-stage dwell timeline from mesh nodes + trace total.
 *
 * Each stage starts at its rel_ms; monotonic-clamped so a stage never starts
 * before its predecessor (guards early-http-send-style out-of-order spans).
 * A stage ends where the next stage starts (sequential pipeline dwell); the
 * last stage ends at total_ms. dwell = end - start (>= 0.01).
 *
 * Returns a NEW array of {...node, start, end, dwell}. Pure.
 */
export function computeTimeline(nodes, totalMs) {
  const stages = nodes.map((node) => ({ ...node, start: Number(node.rel_ms) || 0 }));
  // monotonic starts
  for (let i = 0; i < stages.length; i++) {
    const prev = i ? stages[i - 1].start : 0;
    stages[i].start = Math.max(stages[i].start, prev);
  }
  // end = next start; last = total
  for (let i = 0; i < stages.length; i++) {
    stages[i].end = i < stages.length - 1 ? Math.max(stages[i].start, stages[i + 1].start) : totalMs;
    stages[i].dwell = Math.max(0.01, stages[i].end - stages[i].start);
  }
  return stages;
}

/**
 * Stage activation state at time t: 'armed' (running), 'done' (past), or
 * 'pending' (future). Pure — drives node CSS classes.
 */
export function stageStateAt(stage, t) {
  if (t >= stage.end) return 'done';
  if (t >= stage.start) return 'armed';
  return 'pending';
}

// ── scrub / playhead mapping ─────────────────────────────────────────────────

/**
 * Map a click fraction [0..1] over the scrubber to a trace time in ms.
 * Clamped to [0, totalMs]. Pure.
 */
export function scrubFractionToMs(frac, totalMs) {
  const f = Math.max(0, Math.min(1, frac));
  return f * totalMs;
}

/**
 * Map a trace time (ms) to a fraction [0..1] of the total. Clamped. Pure.
 */
export function msToFraction(t, totalMs) {
  if (totalMs <= 0) return 0;
  return Math.max(0, Math.min(1, t / totalMs));
}

/** Playhead x-position in scrub-svg coordinates (width w). Pure. */
export function playheadX(t, totalMs, w) {
  return msToFraction(t, totalMs) * w;
}

// ── speed presets + persistence (item-4) ──────────────────────────────────────

// Ordered presets; `msPerMs` = wall-ms played per 1 trace-ms.
//   slow=100 · medium=50 · fast=10 · realtime=1 (DEFAULT) · 2×=0.5 · 10×=0.1.
export const SPEED_PRESETS = [
  { id: 'slow', label: 'Slow', msPerMs: 100 },
  { id: 'medium', label: 'Medium', msPerMs: 50 },
  { id: 'fast', label: 'Fast', msPerMs: 10 },
  { id: 'realtime', label: 'Realtime', msPerMs: 1 }, // DEFAULT
  { id: '2x', label: '2×', msPerMs: 0.5 },
  { id: '10x', label: '10×', msPerMs: 0.1 },
];
export const DEFAULT_SPEED_ID = 'realtime';
export const SPEED_STORAGE_KEY = 'yadgar.traces.speed';

/** Preset lookup by id; unknown id → realtime. Pure. */
export function speedById(id) {
  return SPEED_PRESETS.find((p) => p.id === id) || speedById(DEFAULT_SPEED_ID);
}

/**
 * Load the persisted speed id from a localStorage-compatible store (injectable,
 * galaxy-view pattern). Empty / garbage / private-mode-throw → DEFAULT_SPEED_ID.
 * @param {Storage} [storage]
 * @returns {string} a valid preset id
 */
export function loadSpeedId(storage) {
  const store = storage ?? (typeof window !== 'undefined' ? window.localStorage : null);
  if (!store) return DEFAULT_SPEED_ID;
  let raw;
  try {
    raw = store.getItem(SPEED_STORAGE_KEY);
  } catch (_) {
    return DEFAULT_SPEED_ID; // private-mode / access throw
  }
  if (!raw) return DEFAULT_SPEED_ID;
  return SPEED_PRESETS.some((p) => p.id === raw) ? raw : DEFAULT_SPEED_ID;
}

/**
 * Persist the chosen speed id (best-effort; private-mode throw swallowed).
 * @param {string} id
 * @param {Storage} [storage]
 */
export function saveSpeedId(id, storage) {
  const store = storage ?? (typeof window !== 'undefined' ? window.localStorage : null);
  if (!store) return;
  const valid = SPEED_PRESETS.some((p) => p.id === id) ? id : DEFAULT_SPEED_ID;
  try {
    store.setItem(SPEED_STORAGE_KEY, valid);
  } catch (_) {
    /* best-effort persistence */
  }
}

// ── play clock ───────────────────────────────────────────────────────────────

/**
 * Advance the replay clock by dtWallMs of real time at the given preset.
 * `msPerMs` = wall-ms played per 1 trace-ms (so trace-time advance = dt/msPerMs).
 * Returns {t, playing}: t clamped to [0,totalMs]; playing=false at the end.
 * Pure — the DOM layer owns requestAnimationFrame.
 */
export function advanceClock(t, dtWallMs, msPerMs, totalMs) {
  const rate = Number.isFinite(msPerMs) && msPerMs > 0 ? msPerMs : 1;
  let nt = t + dtWallMs / rate;
  let playing = true;
  if (nt >= totalMs) {
    nt = totalMs;
    playing = false;
  }
  return { t: Math.max(0, nt), playing };
}

// ── fault state ──────────────────────────────────────────────────────────────

/** True when the mesh has any error stage (drives fault-red styling). Pure. */
export function meshHasFault(mesh) {
  return Array.isArray(mesh?.nodes) && mesh.nodes.some((n) => n.error === true);
}

/** The first faulting stage node, or null. Pure. */
export function firstFaultStage(mesh) {
  if (!Array.isArray(mesh?.nodes)) return null;
  return mesh.nodes.find((n) => n.error === true) || null;
}

// ── edges ────────────────────────────────────────────────────────────────────

/**
 * Lane-crossing class for the edge from stage a → b. '' for same-lane core,
 * 'to-backend' when entering backend, 'from-backend' when leaving it. Pure.
 */
export function edgeLaneClass(a, b) {
  if (b.lane === 'backend') return 'to-backend';
  if (a.lane === 'backend') return 'from-backend';
  return '';
}
