/**
 * traces-tab.js — DOM wiring for the viz "Traces" tab (viz-trace-replay Car B).
 *
 * Thin DOM layer over the pure replay logic in traces-replay.js (repo convention:
 * no browser render harness — the math is unit-tested there, this file wires it to
 * SVG + fetch). Modelled on the bookmarks-tab trio.
 *
 * Layout (phosphor-oscilloscope, per docs/plans/viz-trace-replay.mockup.html):
 *   LEFT   recent-traces sidebar   (GET /api/traces/recent)
 *   CENTER mesh SVG + transport    (fixed core/backend lanes + scrub waterfall)
 *   RIGHT  drill-down detail panel (per-stage span table)
 *
 * API consumed:
 *   GET /api/traces/recent
 *   GET /api/traces/{trace_id}/mesh
 *
 * Graceful: when Tempo is disabled/unreachable the endpoints return 200 empty;
 * this tab shows an empty-state note and never throws (test_no_uncaught_js_errors).
 */

import {
  scatterLayout,
  computeTimeline,
  stageStateAt,
  scrubFractionToMs,
  msToFraction,
  playheadX,
  advanceClock,
  edgeLaneClass,
  loadSpeedId,
  saveSpeedId,
  speedById,
  SPEED_PRESETS,
  LANE_Y,
  LANE_DIVIDER_Y,
  MESH,
} from './traces-replay.js';

const DAEMON = typeof window !== 'undefined' && window.DAEMON ? window.DAEMON : '';
const SVGNS = 'http://www.w3.org/2000/svg';

// ── module state ──────────────────────────────────────────────────────────────

let _root = null; // tab container
let _listEl = null; // sidebar list
let _svg = null; // mesh <svg>
let _nodesG = null;
let _edgesG = null;
let _lanesG = null;
let _detailEl = null; // right panel body
let _clockEl = null;
let _playBtn = null;
let _speedSel = null;
let _scrubEl = null;
let _playheadEl = null;
let _emptyEl = null;

const _state = {
  recent: [],
  mesh: null,
  reason: '', // Bug 7: WHY replay is empty/partial (surfaced from the endpoint)
  partial: false, // Bug 7: mesh rebuilt from the /api/search spanSet fallback
  stages: [], // laid-out + timelined
  total: 0,
  t: 0,
  playing: false,
  speedId: 'realtime',
  lastFrame: null,
  rafId: null,
};

// ── init ────────────────────────────────────────────────────────────────────

/**
 * Initialise the Traces tab. Called lazily on first #traces open (like control).
 * @param {HTMLElement} tabContainer - #tab-traces
 */
export function initTracesTab(tabContainer) {
  if (!tabContainer) return;
  _root = tabContainer;
  _root.innerHTML = '';

  if (!document.querySelector('link[href*="traces-tab.css"]')) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = './traces-tab.css';
    document.head.appendChild(link);
  }

  _buildScaffold();
  _wireTransport();
  _startRaf();
  _fetchRecent();
}

function _buildScaffold() {
  const deck = _el('div', 'tr-deck');

  // LEFT — recent traces
  const aside = _el('aside', 'tr-recent');
  aside.appendChild(_el('div', 'tr-side-lbl', 'RECENT TRACES'));
  _listEl = _el('div', 'tr-list');
  aside.appendChild(_listEl);
  _emptyEl = _el('div', 'tr-side-note');
  _emptyEl.textContent = 'Loading recent traces…';
  aside.appendChild(_emptyEl);
  deck.appendChild(aside);

  // CENTER — mesh + transport
  const main = _el('main', 'tr-stage');
  const meshWrap = _el('div', 'tr-mesh-wrap');
  _svg = document.createElementNS(SVGNS, 'svg');
  _svg.setAttribute('id', 'tr-mesh');
  _svg.setAttribute('viewBox', `0 0 ${MESH.w} ${MESH.h}`);
  _svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
  _svg.innerHTML =
    '<defs><filter id="tr-glow" x="-80%" y="-80%" width="260%" height="260%">' +
    '<feGaussianBlur stdDeviation="2.6" result="b"/>' +
    '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>';
  _lanesG = document.createElementNS(SVGNS, 'g');
  _edgesG = document.createElementNS(SVGNS, 'g');
  _nodesG = document.createElementNS(SVGNS, 'g');
  _svg.appendChild(_lanesG);
  _svg.appendChild(_edgesG);
  _svg.appendChild(_nodesG);
  meshWrap.appendChild(_svg);
  main.appendChild(meshWrap);

  // transport
  const transport = _el('div', 'tr-transport');
  _playBtn = _el('button', 'tr-tb tr-play', '▶');
  // Speed: a <select> of the 6 presets (item-4). 6 is too many to cycle blindly.
  _speedSel = document.createElement('select');
  _speedSel.className = 'tr-tb tr-speed';
  _speedSel.setAttribute('aria-label', 'Replay speed');
  SPEED_PRESETS.forEach((p) => {
    const opt = document.createElement('option');
    opt.value = p.id;
    opt.textContent = p.label;
    _speedSel.appendChild(opt);
  });
  const restartBtn = _el('button', 'tr-tb', '⟲');
  restartBtn.addEventListener('click', () => {
    _setTime(0);
    _setPlaying(true);
  });
  const btns = _el('div', 'tr-tbtns');
  btns.appendChild(_playBtn);
  btns.appendChild(_speedSel);
  btns.appendChild(restartBtn);
  transport.appendChild(btns);

  _clockEl = _el('div', 'tr-clock', 'T+00.00 ms');
  transport.appendChild(_clockEl);

  _scrubEl = _el('div', 'tr-scrub');
  const scrubSvg = document.createElementNS(SVGNS, 'svg');
  scrubSvg.setAttribute('viewBox', '0 0 1000 20');
  scrubSvg.setAttribute('preserveAspectRatio', 'none');
  scrubSvg.setAttribute('class', 'tr-scrub-svg');
  const track = document.createElementNS(SVGNS, 'line');
  track.setAttribute('class', 'tr-track');
  track.setAttribute('x1', '0');
  track.setAttribute('y1', '10');
  track.setAttribute('x2', '1000');
  track.setAttribute('y2', '10');
  _playheadEl = document.createElementNS(SVGNS, 'line');
  _playheadEl.setAttribute('class', 'tr-playhead');
  _playheadEl.setAttribute('x1', '0');
  _playheadEl.setAttribute('y1', '0');
  _playheadEl.setAttribute('x2', '0');
  _playheadEl.setAttribute('y2', '20');
  scrubSvg.appendChild(track);
  scrubSvg.appendChild(_playheadEl);
  _scrubEl.appendChild(scrubSvg);
  transport.appendChild(_scrubEl);
  main.appendChild(transport);
  deck.appendChild(main);

  // RIGHT — detail
  const detail = _el('aside', 'tr-detail');
  detail.appendChild(_el('div', 'tr-d-kicker', 'TRACE OVERVIEW'));
  _detailEl = _el('div', 'tr-d-body');
  _detailEl.textContent = 'Select a trace to replay.';
  detail.appendChild(_detailEl);
  deck.appendChild(detail);

  _root.appendChild(deck);
}

// ── live append (trace-replay Phase 3) ────────────────────────────────────────

const _RECENT_MAX = 50; // mirror the backend _RECENT_LIMIT_MAX cap

/**
 * Live-append a completed trace to the recent-traces sidebar (finish-viz Phase 3).
 *
 * Fired from the `trace_complete` SSE event (index.html connectSSE → this). The
 * event carries {trace_id, tool, total_ms, status}; we prepend it (newest-first,
 * matching /api/traces/recent ordering), de-dupe by trace_id, cap the list, and
 * re-render — but ONLY when the tab has already been built (_root set on first
 * open). Before first open, _fetchRecent() picks up the trace from Tempo anyway,
 * so a no-op is correct (no buffering needed).
 *
 * @param {{trace_id?:string, tool?:string, total_ms?:number, status?:string}} tr
 */
export function ingestTraceComplete(tr) {
  if (!tr || !tr.trace_id) return;
  const entry = {
    trace_id: String(tr.trace_id),
    tool: tr.tool || '',
    total_ms: Number(tr.total_ms) || 0,
    status: tr.status === 'error' || tr.status === 'timeout' ? tr.status : 'ok',
  };
  // De-dupe (a re-delivered event must not double-list the same trace).
  _state.recent = _state.recent.filter((r) => r.trace_id !== entry.trace_id);
  _state.recent.unshift(entry);
  if (_state.recent.length > _RECENT_MAX) _state.recent.length = _RECENT_MAX;
  // Only touch the DOM when the tab is built (lazy-init on first open).
  if (_root && _listEl) _renderRecent();
}

// ── data ──────────────────────────────────────────────────────────────────────

async function _fetchRecent() {
  try {
    const resp = await fetch(`${DAEMON}/api/traces/recent`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const body = await resp.json();
    if (body.tempo === false) {
      _renderEmpty('Tempo not configured (set TEMPO_QUERY_URL). Trace replay disabled.');
      return;
    }
    _state.recent = Array.isArray(body.traces) ? body.traces : [];
    _renderRecent();
  } catch (err) {
    _renderEmpty(`Could not load recent traces: ${err.message}`);
  }
}

async function _loadTrace(traceId) {
  try {
    const resp = await fetch(`${DAEMON}/api/traces/${encodeURIComponent(traceId)}/mesh`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const body = await resp.json();
    const mesh = body.mesh || { nodes: [], edges: [], timeline_ms: 0 };
    // Bug 7: the endpoint surfaces WHY replay is empty/partial (Tempo 500 body,
    // network error, empty trace). Carry it through so the overview can show it.
    _applyMesh(mesh, { reason: body.reason || '', partial: !!body.partial });
  } catch (err) {
    _detailEl.textContent = `Could not load trace: ${err.message}`;
  }
}

function _applyMesh(mesh, meta) {
  _state.mesh = mesh;
  _state.reason = (meta && meta.reason) || '';
  _state.partial = !!(meta && meta.partial);
  _state.total = Number(mesh.timeline_ms) || 0;
  const laid = scatterLayout(mesh.nodes || [], _state.total);
  _state.stages = computeTimeline(laid, _state.total);
  _buildMesh();
  _renderOverview();
  _setTime(0);
  _setPlaying(true);
}

// ── rendering ─────────────────────────────────────────────────────────────────

function _renderEmpty(msg) {
  if (_emptyEl) _emptyEl.textContent = msg;
  if (_listEl) _listEl.innerHTML = '';
}

function _renderRecent() {
  _listEl.innerHTML = '';
  if (_state.recent.length === 0) {
    _renderEmpty('No recent traces in the last hour.');
    return;
  }
  _emptyEl.textContent = 'Click a trace to replay it on the mesh.';
  _state.recent.forEach((tr) => {
    const item = _el('div', 'tr-item');
    const status = tr.status === 'ok' ? 'ok' : 'err';
    const row1 = _el('div', 'tr-item-row1');
    row1.appendChild(_el('span', 'tr-item-tool', tr.tool || tr.trace_id.slice(0, 12)));
    row1.appendChild(_el('span', 'tr-item-ms', `${tr.total_ms} ms`));
    const row2 = _el('div', 'tr-item-row2');
    row2.appendChild(_el('span', `tr-item-st ${status}`));
    row2.appendChild(_el('span', '', tr.trace_id.slice(0, 16) + '…'));
    item.appendChild(row1);
    item.appendChild(row2);
    item.addEventListener('click', () => {
      _listEl.querySelectorAll('.tr-item').forEach((n) => n.classList.remove('sel'));
      item.classList.add('sel');
      _loadTrace(tr.trace_id);
    });
    _listEl.appendChild(item);
  });
}

function _buildMesh() {
  _nodesG.innerHTML = '';
  _edgesG.innerHTML = '';
  _lanesG.innerHTML = '';

  // lane guides
  [
    ['CORE LANE — yadgar-core', LANE_Y.core],
    ['BACKEND LANE — yadgar-backend', LANE_Y.backend],
  ].forEach(([txt, y]) => {
    const ln = document.createElementNS(SVGNS, 'line');
    ln.setAttribute('class', 'tr-lane-line');
    ln.setAttribute('x1', '30');
    ln.setAttribute('x2', String(MESH.w - 30));
    ln.setAttribute('y1', String(y));
    ln.setAttribute('y2', String(y));
    _lanesG.appendChild(ln);
    const t = document.createElementNS(SVGNS, 'text');
    t.setAttribute('class', 'tr-lane-tag');
    t.setAttribute('x', '30');
    t.setAttribute('y', String(y - 58));
    t.textContent = txt;
    _lanesG.appendChild(t);
  });

  // orange dotted core/backend divider midline (item-3) — brighter than the guides.
  const divider = document.createElementNS(SVGNS, 'line');
  divider.setAttribute('class', 'tr-lane-divider');
  divider.setAttribute('x1', '30');
  divider.setAttribute('x2', String(MESH.w - 30));
  divider.setAttribute('y1', String(LANE_DIVIDER_Y));
  divider.setAttribute('y2', String(LANE_DIVIDER_Y));
  _lanesG.appendChild(divider);

  const stages = _state.stages;
  // edges
  for (let i = 0; i < stages.length - 1; i++) {
    const a = stages[i];
    const b = stages[i + 1];
    const dx = b.x - a.x;
    const d = `M ${a.x + MESH.r} ${a.y} C ${a.x + dx * 0.55} ${a.y}, ${b.x - dx * 0.55} ${b.y}, ${b.x - MESH.r} ${b.y}`;
    const cls = edgeLaneClass(a, b);
    const edge = document.createElementNS(SVGNS, 'path');
    edge.setAttribute('class', `tr-edge ${cls}`);
    edge.setAttribute('d', d);
    _edgesG.appendChild(edge);
    b._edge = edge;
  }

  // nodes
  stages.forEach((st) => {
    const g = document.createElementNS(SVGNS, 'g');
    g.setAttribute('class', 'tr-node' + (st.lane === 'backend' ? ' backend' : '') + (st.error ? ' fault' : ''));
    g.setAttribute('transform', `translate(${st.x},${st.y})`);
    const ring = document.createElementNS(SVGNS, 'circle');
    ring.setAttribute('class', 'tr-ring');
    ring.setAttribute('r', String(MESH.r));
    const core = document.createElementNS(SVGNS, 'circle');
    core.setAttribute('class', 'tr-core');
    core.setAttribute('r', '3.6');
    const lbl = document.createElementNS(SVGNS, 'text');
    lbl.setAttribute('class', 'tr-nlabel');
    lbl.setAttribute('y', '34');
    lbl.textContent = st.label + (st.storm_n ? ` ×${st.storm_n}` : '');
    const dwell = document.createElementNS(SVGNS, 'text');
    dwell.setAttribute('class', 'tr-ndwell');
    dwell.setAttribute('y', '-26');
    dwell.textContent = `${st.dwell.toFixed(st.dwell < 1 ? 2 : 1)} ms`;
    g.appendChild(ring);
    g.appendChild(core);
    g.appendChild(dwell);
    g.appendChild(lbl);
    g.addEventListener('click', () => _pickStage(st));
    _nodesG.appendChild(g);
    st._el = g;
  });
}

function _renderOverview() {
  const m = _state.mesh;
  const dropped = m.dropped_boundary ? ' · boundary span dropped (flat forest)' : '';
  // Bug 7: when the by-id trace was unavailable, the endpoint surfaces WHY
  // (Tempo 500 / network / empty). Show it so an empty or partial mesh reads as
  // "replay degraded because X", not a silent blank. Tempo text is semi-trusted
  // → escape (XSS guard, same as span labels).
  const partial = _state.partial ? ' · partial replay (search fallback)' : '';
  const note = _state.reason
    ? `<div class="tr-d-note">⚠ ${_esc(_state.reason)}</div>`
    : '';
  _detailEl.innerHTML =
    `<div class="tr-d-title">${_esc(m.tool || m.label || 'trace')}</div>` +
    `<div class="tr-d-sub">${(m.nodes || []).length} stages · ${_esc(_state.total)} ms${dropped}${partial}</div>` +
    note +
    `<ul class="tr-stage-list">` +
    _state.stages
      .map(
        (st) =>
          `<li class="${st.error ? 'fault' : ''}">${_esc(st.label)}<span class="tr-lms">${st.dwell.toFixed(st.dwell < 1 ? 2 : 1)} ms</span></li>`,
      )
      .join('') +
    `</ul>`;
}

function _pickStage(st) {
  _state.stages.forEach((s) => s._el && s._el.classList.toggle('picked', s === st));
  _detailEl.innerHTML =
    `<div class="tr-d-title ${st.error ? 'fault' : ''}">${_esc(st.label)}</div>` +
    `<div class="tr-d-sub">${_esc(st.name || '')}</div>` +
    `<div class="tr-d-stats">` +
    `<span><b>${st.dwell.toFixed(st.dwell < 1 ? 2 : 1)}</b> ms dwell</span>` +
    `<span><b>${_esc(st.rel_ms)}</b> ms start</span>` +
    (st.storm_n ? `<span><b>×${_esc(st.storm_n)}</b> storm</span>` : '') +
    `</div>`;
}

// ── replay loop ────────────────────────────────────────────────────────────────

function _setTime(ms) {
  _state.t = Math.max(0, Math.min(_state.total, ms));
  _renderFrame();
}

function _renderFrame() {
  const t = _state.t;
  if (_clockEl) _clockEl.textContent = 'T+' + t.toFixed(2).padStart(5, '0') + ' ms';
  if (_playheadEl) {
    const px = playheadX(t, _state.total, 1000);
    _playheadEl.setAttribute('x1', String(px));
    _playheadEl.setAttribute('x2', String(px));
  }
  _state.stages.forEach((st) => {
    if (!st._el) return;
    const s = stageStateAt(st, t);
    st._el.classList.toggle('armed', s === 'armed');
    st._el.classList.toggle('done', s === 'done');
    if (st._edge) st._edge.classList.toggle('lit', t >= st.start);
  });
}

function _setPlaying(on) {
  _state.playing = on;
  if (_playBtn) _playBtn.textContent = on ? '❚❚' : '▶';
}

function _tick(now) {
  if (_state.lastFrame == null) _state.lastFrame = now;
  const dt = now - _state.lastFrame;
  _state.lastFrame = now;
  if (_state.playing && _state.total > 0) {
    const msPerMs = speedById(_state.speedId).msPerMs;
    const { t, playing } = advanceClock(_state.t, dt, msPerMs, _state.total);
    _state.t = t;
    _renderFrame();
    if (!playing) _setPlaying(false);
  }
  _state.rafId = requestAnimationFrame(_tick);
}

function _startRaf() {
  if (_state.rafId == null && typeof requestAnimationFrame === 'function') {
    _state.rafId = requestAnimationFrame(_tick);
  }
}

function _wireTransport() {
  // restore the persisted speed choice (localStorage; realtime on first use).
  _state.speedId = loadSpeedId();
  if (_speedSel) _speedSel.value = _state.speedId;
  _playBtn.addEventListener('click', () => {
    if (!_state.playing && _state.t >= _state.total) _setTime(0);
    _setPlaying(!_state.playing);
  });
  _speedSel.addEventListener('change', () => {
    _state.speedId = _speedSel.value;
    saveSpeedId(_state.speedId);
  });
  // scrub: click/drag over the track
  let dragging = false;
  const seek = (ev) => {
    const rect = _scrubEl.getBoundingClientRect();
    if (!rect.width) return;
    const frac = (ev.clientX - rect.left) / rect.width;
    _setTime(scrubFractionToMs(frac, _state.total));
  };
  _scrubEl.addEventListener('pointerdown', (ev) => {
    dragging = true;
    _scrubEl.setPointerCapture?.(ev.pointerId);
    seek(ev);
  });
  _scrubEl.addEventListener('pointermove', (ev) => dragging && seek(ev));
  _scrubEl.addEventListener('pointerup', () => {
    dragging = false;
  });
}

// ── helpers ────────────────────────────────────────────────────────────────────

function _el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

/** HTML-escape a value for safe interpolation into innerHTML (XSS guard). */
function _esc(v) {
  const s = String(v == null ? '' : v);
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// exported for symmetry / potential future direct use (kept minimal)
export const _internals = { _state };
