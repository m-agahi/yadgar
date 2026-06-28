/**
 * info.test.js — v5.50.4 info-tab mapping tests
 *
 * Tests the pure mapping functions extracted to info.js.
 * All mocks use REAL field names from the live API (verified against daemon).
 *
 * /api/info    → {version, python_version}
 * /api/graph/stats → {memory_count, wiki_page_count, temporal_edge_count, transition_edge_count}
 * /api/system  → {daemon_cpu_pct, rss_bytes, daemon_threads, open_fds, uptime_s,
 *                  db_size_mb, system_ram_available_mb, load_avg_1m, load_avg_5m, load_avg_15m}
 * /api/daemon-health → {core: {queue: {depth, dlq_size, drainer_lag_p95_ms}}}
 *
 * Run: cd viz-tests && npm test
 */

import { describe, expect, it } from 'vitest';
import { mapInfoData, mapStatsData, mapHealthData } from './info.js';
import { _fmtBytes, _fmtUptime } from './viz_helpers.js';

const MOCK_VIZ_CONFIG = {
  edge: { variant: 'line', opacity: 0.8, width_3d_multiplier: 1.5 },
  physics: { charge_strength: -120 },
  node: { wiki_shape: 'diamond' },
};

// Real /api/info payload shape
const MOCK_INFO = {
  version: '5.50.4',
  python_version: '3.12.3',
};

describe('mapInfoData', () => {
  it('extracts version from /api/info payload', () => {
    const result = mapInfoData(MOCK_INFO, MOCK_VIZ_CONFIG);
    expect(result['ti-version']).toBe('5.50.4');
  });

  it('extracts python_version from /api/info payload', () => {
    const result = mapInfoData(MOCK_INFO, MOCK_VIZ_CONFIG);
    expect(result['ti-python']).toBe('3.12.3');
  });

  it('returns null for missing info fields', () => {
    const result = mapInfoData({}, MOCK_VIZ_CONFIG);
    expect(result['ti-version']).toBeNull();
    expect(result['ti-python']).toBeNull();
  });

  // v5.87 car A: viz-config (variant/opacity/width/charge/wiki-shape) was removed
  // from the About page — config now lives only under System → Config. mapInfoData
  // no longer emits those keys.
  it('does not emit viz-config fields (moved to System → Config)', () => {
    const result = mapInfoData(MOCK_INFO, MOCK_VIZ_CONFIG);
    expect(result).not.toHaveProperty('ti-variant');
    expect(result).not.toHaveProperty('ti-opacity');
    expect(result).not.toHaveProperty('ti-width');
    expect(result).not.toHaveProperty('ti-charge');
    expect(result).not.toHaveProperty('ti-wiki-shape');
  });
});

// ── mapStatsData ──────────────────────────────────────────────────────────────
// Real /api/graph/stats payload shape: {memory_count, wiki_page_count,
//   temporal_edge_count, transition_edge_count}

describe('mapStatsData', () => {
  const MOCK = {
    memory_count: 1234,
    wiki_page_count: 56,
    temporal_edge_count: 300,
    transition_edge_count: 42,
  };

  it('returns two rows in correct order', () => {
    const rows = mapStatsData(MOCK);
    expect(rows).toHaveLength(2);
    expect(rows[0][0]).toBe('Memories');
    expect(rows[1][0]).toBe('Wiki pages');
  });

  it('maps memory_count', () => {
    expect(mapStatsData(MOCK)[0][1]).toBe(1234);
  });

  it('maps wiki_page_count', () => {
    expect(mapStatsData(MOCK)[1][1]).toBe(56);
  });

  it('uses — for missing fields', () => {
    const rows = mapStatsData({});
    rows.forEach(([, v]) => expect(v).toBe('—'));
  });

  it('uses — for null memory_count', () => {
    expect(mapStatsData({ memory_count: null })[0][1]).toBe('—');
  });

  it('maps memory_count = 0 (not falsy fallback)', () => {
    expect(mapStatsData({ memory_count: 0 })[0][1]).toBe(0);
  });
});

// ── mapHealthData ─────────────────────────────────────────────────────────────
// Real /api/system fields: daemon_cpu_pct, rss_bytes, daemon_threads, open_fds,
// uptime_s, db_size_mb, system_ram_available_mb, load_avg_1m/5m/15m
// Queue fields from /api/daemon-health core.queue

describe('mapHealthData', () => {
  const MOCK_SYS = {
    daemon_cpu_pct: 12,
    rss_bytes: 524288,
    daemon_threads: 4,
    open_fds: 20,
    uptime_s: 3661,
    db_size_mb: 391.7,
    system_ram_available_mb: 30752.3,
    load_avg_1m: 0.5,
    load_avg_5m: 0.8,
    load_avg_15m: 1.0,
  };

  const MOCK_DH = {
    core: {
      queue: {
        depth: 3,
        dlq_size: 0,
        drainer_lag_p95_ms: 45,
      },
    },
  };

  it('maps daemon_cpu_pct with % suffix', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-cpu']).toBe('12%');
  });

  it('maps rss_bytes using fmtBytes', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-rss']).toBe('512 KB');
  });

  it('maps daemon_threads', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-threads']).toBe(4);
  });

  it('maps open_fds', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-fds']).toBe(20);
  });

  it('maps uptime_s using fmtUptime', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-uptime']).toBe('1h 1m');
  });

  it('maps load_avg_1m/5m/15m as slash-joined string', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-load']).toBe('0.5 / 0.8 / 1');
  });

  it('maps db_size_mb with MB suffix', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-db']).toBe('391.7 MB');
  });

  it('maps system_ram_available_mb with MB suffix', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-ram']).toBe('30752 MB');
  });

  it('maps lag with ms suffix', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-lag']).toBe('45ms');
  });

  it('returns — for missing sys data', () => {
    const r = mapHealthData({}, {}, _fmtBytes, _fmtUptime);
    expect(r['th-cpu']).toBe('—');
    expect(r['th-rss']).toBe('—');
    expect(r['th-threads']).toBe('—');
    expect(r['th-uptime']).toBe('—');
    expect(r['th-load']).toBe('—');
    expect(r['th-db']).toBe('—');
    expect(r['th-ram']).toBe('—');
  });

  it('maps dlq_size = 0 (not falsy fallback)', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-dlq']).toBe(0);
  });

  it('maps depth from core.queue', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-qdepth']).toBe(3);
  });
});
