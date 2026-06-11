/**
 * info.js — Tab data mapping functions for the yadgar viz SPA.
 *
 * Pure functions extracted from inline tab-refresh handlers in index.html (v5.50.0).
 * No DOM dependencies. No side effects. Import and call from both the page and tests.
 *
 * Exports:
 *   mapInfoData   — /api/stats payload + vizConfig → info-tab field map
 *   mapStatsData  — /api/stats payload → stats-tab row pairs
 *   mapHealthData — /api/system + /api/daemon-health → health-tab field map
 */

/**
 * Map /api/stats + vizConfig to info-tab field values.
 * Returns null for missing fields (caller renders '—').
 *
 * @param {Object} stats     - response from /api/stats (may be {})
 * @param {Object} vizConfig - YADGAR_VIZ_CONFIG structure
 * @returns {Object} map of element-id → value | null
 */
export function mapInfoData(stats, vizConfig) {
  return {
    'ti-version':    stats.version                          ?? null,
    'ti-python':     stats.python_version                   ?? null,
    'ti-variant':    vizConfig?.edge?.variant               ?? null,
    'ti-opacity':    vizConfig?.edge?.opacity               ?? null,
    'ti-width':      vizConfig?.edge?.width_3d_multiplier   ?? null,
    'ti-charge':     vizConfig?.physics?.charge_strength    ?? null,
    'ti-wiki-shape': vizConfig?.node?.wiki_shape            ?? null,
  };
}

/**
 * Map /api/stats payload to ordered label-value row pairs for the stats tab.
 * Missing numeric fields fall back to '—'.
 *
 * @param {Object} d - response from /api/stats
 * @returns {Array<[string, string|number]>} ordered pairs [label, value]
 */
export function mapStatsData(d) {
  return [
    ['Memories',       d.total_memories   ?? '—'],
    ['Wiki pages',     d.total_wiki_pages ?? '—'],
    ['Embeddings',     d.total_embeddings ?? '—'],
    ['Hot (heat>0.5)', d.hot_memories     ?? '—'],
    ['Orphan edges',   d.orphan_edge_count != null ? d.orphan_edge_count : '—'],
  ];
}

/**
 * Map /api/system + /api/daemon-health payloads to health-tab field values.
 * Values already formatted as display strings (callers set textContent directly).
 * Formatting helpers (_fmtBytes, _fmtUptime) are passed in to keep this pure.
 *
 * @param {Object} sys          - /api/system response
 * @param {Object} dh           - /api/daemon-health response
 * @param {Function} fmtBytes   - bytes formatter, e.g. _fmtBytes
 * @param {Function} fmtUptime  - uptime formatter, e.g. _fmtUptime
 * @returns {Object} map of element-id → display string
 */
export function mapHealthData(sys, dh, fmtBytes, fmtUptime) {
  const c  = (dh && dh.core)    || {};
  const cp = c.process           || {};
  const cq = c.queue             || {};
  const load = sys && sys.load_avg;
  return {
    'th-cpu':     cp.cpu_pct    != null ? cp.cpu_pct + '%'         : '—',
    'th-rss':     cp.rss_bytes  != null ? fmtBytes(cp.rss_bytes)   : '—',
    'th-threads': cp.thread_count  ?? '—',
    'th-fds':     cp.open_fds      ?? '—',
    'th-uptime':  cp.uptime_s   != null ? fmtUptime(cp.uptime_s)   : '—',
    'th-qdepth':  cq.depth         ?? '—',
    'th-dlq':     cq.dlq_size      ?? '—',
    'th-lag':     cq.drainer_lag_p95_ms != null ? cq.drainer_lag_p95_ms + 'ms' : '—',
    'th-db':      sys && sys.db_size_bytes  != null ? fmtBytes(sys.db_size_bytes)  : '—',
    'th-ram':     sys && sys.ram_free_bytes != null ? fmtBytes(sys.ram_free_bytes) : '—',
    'th-load':    Array.isArray(load) ? load.join(' / ') : '—',
  };
}
