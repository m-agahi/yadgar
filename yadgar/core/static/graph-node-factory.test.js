/**
 * graph-node-factory.test.js — v5.50.6
 *
 * Unit tests for makeNodeThreeObject with stubbed THREE.
 * No WebGL required — stubs record constructor args only.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { makeNodeThreeObject } from './graph-node-factory.js';

// ── THREE stub ────────────────────────────────────────────────────────────────

class FakeGeometry {
  constructor(radius, detail) {
    this.type = 'OctahedronGeometry';
    this.radius = radius;
    this.detail = detail;
  }
}

class FakeSphereGeometry {
  constructor(radius, ws, hs) {
    this.type = 'SphereGeometry';
    this.radius = radius;
    this.ws = ws;
    this.hs = hs;
  }
}

class FakeMaterial {
  constructor(opts) {
    this.type = 'MeshBasicMaterial';
    this.color = opts && opts.color;
    // transparent defaults to false when not passed
    this.transparent = (opts && opts.transparent) || false;
  }
}

class FakeBoxGeometry {
  constructor(w, h, d) {
    this.type = 'BoxGeometry';
    this.w = w;
    this.h = h;
    this.d = d;
  }
}

class FakeMesh {
  constructor(geometry, material) {
    this.geometry = geometry;
    this.material = material;
    this.type = 'Mesh';
  }
}

const FAKE_THREE = {
  OctahedronGeometry: FakeGeometry,
  SphereGeometry: FakeSphereGeometry,
  BoxGeometry: FakeBoxGeometry,
  MeshBasicMaterial: FakeMaterial,
  Mesh: FakeMesh,
};

const DEFAULT_CONFIG = { wiki_shape: 'octahedron', size_3d: 8 };
const colorFn = (node) => '#58a6ff';

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('makeNodeThreeObject — wiki nodes', () => {
  it('returns a Mesh for wiki nodes when wiki_shape=octahedron', () => {
    const node = { type: 'wiki', category: 'architecture', id: 'wiki:arch' };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    expect(result).not.toBeNull();
    expect(result).toBeInstanceOf(FakeMesh);
  });

  it('geometry is OctahedronGeometry for wiki node', () => {
    const node = { type: 'wiki', id: 'wiki:foo' };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    expect(result.geometry).toBeInstanceOf(FakeGeometry);
    expect(result.geometry.type).toBe('OctahedronGeometry');
  });

  it('octahedron radius matches Math.cbrt(1) * size_3d', () => {
    const node = { type: 'wiki', id: 'wiki:foo' };
    const result = makeNodeThreeObject(node, FAKE_THREE, { wiki_shape: 'octahedron', size_3d: 8 }, colorFn);
    expect(result.geometry.radius).toBe(8); // Math.cbrt(1) * 8 = 8
  });

  it('material is MeshBasicMaterial', () => {
    const node = { type: 'wiki', id: 'wiki:foo' };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    expect(result.material).toBeInstanceOf(FakeMaterial);
    expect(result.material.type).toBe('MeshBasicMaterial');
  });

  it('material has transparent:false (depthWrite stays true — the shard fix)', () => {
    const node = { type: 'wiki', id: 'wiki:foo' };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    // transparent:false (the default) ensures depthWrite=true → solid octahedron
    expect(result.material.transparent).toBe(false);
  });

  it('material color comes from colorFn', () => {
    const node = { type: 'wiki', id: 'wiki:foo' };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, () => '#ff0000');
    expect(result.material.color).toBe('#ff0000');
  });

  it('falls back to #8b949e when colorFn returns falsy', () => {
    const node = { type: 'wiki', id: 'wiki:foo' };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, () => null);
    expect(result.material.color).toBe('#8b949e');
  });
});

describe('makeNodeThreeObject — memory/other nodes', () => {
  it('returns null for memory nodes (ForceGraph uses default sphere)', () => {
    const node = { type: 'memory', id: 'entity:123', heat: 0.5 };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    expect(result).toBeNull();
  });

  it('returns null for temporal nodes', () => {
    const node = { type: 'temporal', id: 'entity:456' };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    expect(result).toBeNull();
  });

  it('returns null for nodes with no type', () => {
    const node = { id: 'entity:789' };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    expect(result).toBeNull();
  });

  it('returns null for a plain memory node carrying no _anchor tag', () => {
    const node = { type: 'memory', id: 'mem:1', tags: ['work', 'note'] };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    expect(result).toBeNull();
  });
});

// ── P3.7 / item #6: anchored/protected memory nodes get a distinct 3D shape ──
// Anchored memories carry the '_anchor' tag (restoration.anchor_memory adds it
// alongside is_protected=True). is_protected itself is NOT in the graph payload,
// so the '_anchor' tag is the available signal. Distinct geometry (BoxGeometry,
// rendered as a cube/diamond) marks them vs the default sphere for plain memories.
describe('makeNodeThreeObject — anchored memory shape [54]', () => {
  it('returns a Mesh for memory nodes tagged _anchor', () => {
    const node = { type: 'memory', id: 'mem:a', tags: ['_anchor', 'decision'] };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    expect(result).not.toBeNull();
    expect(result).toBeInstanceOf(FakeMesh);
  });

  it('anchored memory geometry is BoxGeometry (distinct from sphere/octahedron)', () => {
    const node = { type: 'memory', id: 'mem:a', tags: ['_anchor'] };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    expect(result.geometry.type).toBe('BoxGeometry');
  });

  it('anchored memory material is MeshBasicMaterial with transparent:false (shard fix preserved)', () => {
    const node = { type: 'memory', id: 'mem:a', tags: ['_anchor'] };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    expect(result.material.type).toBe('MeshBasicMaterial');
    expect(result.material.transparent).toBe(false);
  });

  it('anchored memory color comes from colorFn (honors dim/cluster/heat)', () => {
    const node = { type: 'memory', id: 'mem:a', tags: ['_anchor'] };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, () => '#112233');
    expect(result.material.color).toBe('#112233');
  });

  it('wiki node still wins octahedron even if also _anchor-tagged', () => {
    const node = { type: 'wiki', id: 'wiki:a', tags: ['_anchor'] };
    const result = makeNodeThreeObject(node, FAKE_THREE, DEFAULT_CONFIG, colorFn);
    expect(result.geometry.type).toBe('OctahedronGeometry');
  });
});

describe('makeNodeThreeObject — wiki_shape config', () => {
  it('returns null for wiki nodes when wiki_shape=sphere (config override)', () => {
    const node = { type: 'wiki', id: 'wiki:foo' };
    const result = makeNodeThreeObject(node, FAKE_THREE, { wiki_shape: 'sphere', size_3d: 8 }, colorFn);
    expect(result).toBeNull();
  });

  it('returns null when THREE is undefined', () => {
    const node = { type: 'wiki', id: 'wiki:foo' };
    const result = makeNodeThreeObject(node, undefined, DEFAULT_CONFIG, colorFn);
    expect(result).toBeNull();
  });

  it('returns null when THREE is null', () => {
    const node = { type: 'wiki', id: 'wiki:foo' };
    const result = makeNodeThreeObject(node, null, DEFAULT_CONFIG, colorFn);
    expect(result).toBeNull();
  });
});
