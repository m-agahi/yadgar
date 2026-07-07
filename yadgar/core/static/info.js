/**
 * info.js — Tab data mapping functions for the yadgar viz SPA.
 *
 * Pure functions extracted from inline tab-refresh handlers in index.html (v5.50.0).
 * No DOM dependencies. No side effects. Import and call from both the page and tests.
 *
 * Exports:
 *   mapInfoData   — /api/info payload + vizConfig → info-tab field map
 *   mapStatsData  — /api/graph/stats payload → stats-tab row pairs
 *   mapHealthData — /api/system + /api/daemon-health → health-tab field map
 */

/**
 * Map /api/info to About-tab field values.
 * Returns null for missing fields (caller renders '—').
 *
 * v5.87 car A: the viz-config fields (edge variant/opacity/width, charge,
 * wiki shape) were removed from the About page — config now lives only under
 * System → Config. The `vizConfig` param is accepted for call-site
 * compatibility but no longer read.
 *
 * @param {Object} info      - response from /api/info (may be {})
 * @param {Object} [vizConfig] - unused (kept for back-compat)
 * @returns {Object} map of element-id → value | null
 */
export function mapInfoData(info, vizConfig) {
  return {
    'ti-version':    info.version                           ?? null,
    'ti-python':     info.python_version                    ?? null,
  };
}

/**
 * Map /api/graph/stats payload to ordered label-value row pairs for the stats tab.
 * Missing numeric fields fall back to '—'.
 *
 * Real fields from /api/graph/stats: memory_count, wiki_page_count,
 * temporal_edge_count, transition_edge_count.
 *
 * @param {Object} d - response from /api/graph/stats
 * @returns {Array<[string, string|number]>} ordered pairs [label, value]
 */
export function mapStatsData(d) {
  return [
    ['Memories',   d.memory_count    ?? '—'],
    ['Wiki pages', d.wiki_page_count ?? '—'],
  ];
}

/**
 * Map /api/system + /api/daemon-health payloads to health-tab field values.
 * Values already formatted as display strings (callers set textContent directly).
 * Formatting helpers (_fmtBytes, _fmtUptime) are passed in to keep this pure.
 *
 * Real /api/system fields: daemon_cpu_pct, rss_bytes, daemon_threads, open_fds,
 * uptime_s, db_size_mb, system_ram_available_mb, load_avg_1m/5m/15m.
 * Queue fields still from /api/daemon-health core.queue.
 *
 * @param {Object} sys          - /api/system response
 * @param {Object} dh           - /api/daemon-health response
 * @param {Function} fmtBytes   - bytes formatter, e.g. _fmtBytes
 * @param {Function} fmtUptime  - uptime formatter, e.g. _fmtUptime
 * @returns {Object} map of element-id → display string
 */
export function mapHealthData(sys, dh, fmtBytes, fmtUptime) {
  const s  = sys || {};
  const c  = (dh && dh.core) || {};
  const cq = c.queue         || {};
  return {
    'th-cpu':     s.daemon_cpu_pct != null ? s.daemon_cpu_pct + '%'          : '—',
    'th-rss':     s.rss_bytes      != null ? fmtBytes(s.rss_bytes)           : '—',
    'th-threads': s.daemon_threads ?? '—',
    'th-fds':     s.open_fds       ?? '—',
    'th-uptime':  s.uptime_s       != null ? fmtUptime(s.uptime_s)           : '—',
    'th-qdepth':  cq.depth         ?? '—',
    'th-dlq':     cq.dlq_size      ?? '—',
    'th-lag':     cq.drainer_lag_p95_ms != null ? cq.drainer_lag_p95_ms + 'ms' : '—',
    'th-db':      s.db_size_mb     != null ? (s.db_size_mb.toFixed(1) + ' MB') : '—',
    'th-ram':     s.system_ram_available_mb != null ? (s.system_ram_available_mb.toFixed(0) + ' MB') : '—',
    'th-load':    s.load_avg_1m    != null
                    ? s.load_avg_1m + ' / ' + s.load_avg_5m + ' / ' + s.load_avg_15m
                    : '—',
  };
}
