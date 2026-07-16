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
export const SPEEDS = [0.5, 1, 2, 4];
export const DILATION = 150; // 1 trace-ms → 150 wall-ms at ×1

/**
 * Horizontal position of stage i of n along the mesh (evenly spaced).
 * n===1 → centered. Pure.
 */
export function laneX(i, n) {
  if (n <= 1) return MESH.x0 + (MESH.x1 - MESH.x0) * 0.5;
  return MESH.x0 + (MESH.x1 - MESH.x0) * (i / (n - 1));
}

/**
 * Assign fixed-lane (x, y) coordinates to each mesh node.
 * Returns a NEW array of {...node, x, y} — does not mutate input.
 */
export function layoutStages(nodes) {
  const n = nodes.length;
  return nodes.map((node, i) => ({
    ...node,
    x: laneX(i, n),
    y: LANE_Y[node.lane] ?? LANE_Y.core,
  }));
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

// ── play clock ───────────────────────────────────────────────────────────────

/**
 * Advance the replay clock by dtWallMs of real time at the given speed.
 * Returns {t, playing}: t clamped to [0,totalMs]; playing=false when the end
 * is reached. Pure — the DOM layer owns requestAnimationFrame.
 */
export function advanceClock(t, dtWallMs, speedIdx, totalMs) {
  const speed = SPEEDS[speedIdx] ?? 1;
  let nt = t + (dtWallMs / DILATION) * speed;
  let playing = true;
  if (nt >= totalMs) {
    nt = totalMs;
    playing = false;
  }
  return { t: Math.max(0, nt), playing };
}

/** Cycle to the next speed index (wraps). Pure. */
export function nextSpeedIdx(speedIdx) {
  return (speedIdx + 1) % SPEEDS.length;
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
