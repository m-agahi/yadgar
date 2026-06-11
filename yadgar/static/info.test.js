/**
 * info.test.js — v5.50.0 info-tab mapping tests
 *
 * Tests the pure mapInfoData function extracted to info.js.
 * Verifies field mapping from /api/stats payload + vizConfig.
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

const MOCK_STATS = {
  version: '5.50.0',
  python_version: '3.12.3',
};

describe('mapInfoData', () => {
  it('extracts version from stats payload', () => {
    const result = mapInfoData(MOCK_STATS, MOCK_VIZ_CONFIG);
    expect(result['ti-version']).toBe('5.50.0');
  });

  it('extracts python_version from stats payload', () => {
    const result = mapInfoData(MOCK_STATS, MOCK_VIZ_CONFIG);
    expect(result['ti-python']).toBe('3.12.3');
  });

  it('extracts edge variant from viz config', () => {
    const result = mapInfoData(MOCK_STATS, MOCK_VIZ_CONFIG);
    expect(result['ti-variant']).toBe('line');
  });

  it('extracts edge opacity from viz config', () => {
    const result = mapInfoData(MOCK_STATS, MOCK_VIZ_CONFIG);
    expect(result['ti-opacity']).toBe(0.8);
  });

  it('extracts edge width_3d_multiplier from viz config', () => {
    const result = mapInfoData(MOCK_STATS, MOCK_VIZ_CONFIG);
    expect(result['ti-width']).toBe(1.5);
  });

  it('extracts charge_strength from viz config physics', () => {
    const result = mapInfoData(MOCK_STATS, MOCK_VIZ_CONFIG);
    expect(result['ti-charge']).toBe(-120);
  });

  it('extracts wiki_shape from viz config node', () => {
    const result = mapInfoData(MOCK_STATS, MOCK_VIZ_CONFIG);
    expect(result['ti-wiki-shape']).toBe('diamond');
  });

  it('returns null for missing stats fields', () => {
    const result = mapInfoData({}, MOCK_VIZ_CONFIG);
    expect(result['ti-version']).toBeNull();
    expect(result['ti-python']).toBeNull();
  });

  it('returns null for missing viz config fields', () => {
    const result = mapInfoData(MOCK_STATS, { edge: {}, physics: {}, node: {} });
    expect(result['ti-variant']).toBeNull();
    expect(result['ti-opacity']).toBeNull();
    expect(result['ti-width']).toBeNull();
    expect(result['ti-charge']).toBeNull();
    expect(result['ti-wiki-shape']).toBeNull();
  });
});

// ── mapStatsData ──────────────────────────────────────────────────────────────

describe('mapStatsData', () => {
  const MOCK = {
    total_memories: 1234,
    total_wiki_pages: 56,
    total_embeddings: 1200,
    hot_memories: 78,
    orphan_edge_count: 0,
  };

  it('returns five rows in correct order', () => {
    const rows = mapStatsData(MOCK);
    expect(rows).toHaveLength(5);
    expect(rows[0][0]).toBe('Memories');
    expect(rows[4][0]).toBe('Orphan edges');
  });

  it('maps total_memories', () => {
    expect(mapStatsData(MOCK)[0][1]).toBe(1234);
  });

  it('maps total_wiki_pages', () => {
    expect(mapStatsData(MOCK)[1][1]).toBe(56);
  });

  it('maps orphan_edge_count = 0 (not falsy fallback)', () => {
    expect(mapStatsData(MOCK)[4][1]).toBe(0);
  });

  it('uses — for missing fields', () => {
    const rows = mapStatsData({});
    rows.forEach(([, v]) => expect(v).toBe('—'));
  });

  it('uses — for null orphan_edge_count but not 0', () => {
    expect(mapStatsData({ orphan_edge_count: null })[4][1]).toBe('—');
    expect(mapStatsData({ orphan_edge_count: 0 })[4][1]).toBe(0);
  });
});

// ── mapHealthData ─────────────────────────────────────────────────────────────

describe('mapHealthData', () => {
  const MOCK_SYS = {
    db_size_bytes: 1048576,
    ram_free_bytes: 2097152,
    load_avg: [0.5, 0.8, 1.0],
  };

  const MOCK_DH = {
    core: {
      process: {
        cpu_pct: 12,
        rss_bytes: 524288,
        thread_count: 4,
        open_fds: 20,
        uptime_s: 3661,
      },
      queue: {
        depth: 3,
        dlq_size: 0,
        drainer_lag_p95_ms: 45,
      },
    },
  };

  it('maps cpu_pct with % suffix', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-cpu']).toBe('12%');
  });

  it('maps rss_bytes using fmtBytes', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-rss']).toBe('512 KB');
  });

  it('maps uptime_s using fmtUptime', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-uptime']).toBe('1h 1m');
  });

  it('maps load_avg as slash-joined string', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-load']).toBe('0.5 / 0.8 / 1');
  });

  it('maps db_size_bytes using fmtBytes', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-db']).toBe('1.0 MB');
  });

  it('maps lag with ms suffix', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-lag']).toBe('45ms');
  });

  it('returns — for missing core data', () => {
    const r = mapHealthData({}, {}, _fmtBytes, _fmtUptime);
    expect(r['th-cpu']).toBe('—');
    expect(r['th-rss']).toBe('—');
    expect(r['th-uptime']).toBe('—');
    expect(r['th-load']).toBe('—');
  });

  it('maps dlq_size = 0 (not falsy fallback)', () => {
    const r = mapHealthData(MOCK_SYS, MOCK_DH, _fmtBytes, _fmtUptime);
    expect(r['th-dlq']).toBe(0);
  });
});
