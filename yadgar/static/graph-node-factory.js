/**
 * graph-node-factory.js — v5.50.6
 *
 * Testable node-factory logic extracted for unit testing.
 * index.html inlines equivalent logic directly (plain script, not ES module);
 * this module is used by graph-node-factory.test.js via stubbed THREE.
 *
 * Root cause of v5.10.7–.2 shard failures:
 *   transparent:true → depthWrite:false → THREE renders faces in submit order
 *   (triangle-sort artifact) instead of depth order → fragmented shards on polyhedra.
 * Fix: MeshBasicMaterial with no transparent flag (defaults false → depthWrite:true).
 */

'use strict';

/**
 * Build a THREE.js node object for ForceGraph3D.
 *
 * @param {object} node      - graph node with .type and optional .heat
 * @param {object} THREE     - THREE.js namespace (window.THREE or stub)
 * @param {object} config    - { wiki_shape: 'octahedron'|'sphere', size_3d: number }
 * @param {function} colorFn - (node) → CSS colour string
 * @returns {THREE.Mesh|null} - null → ForceGraph uses its default sphere
 */
export function makeNodeThreeObject(node, THREE, config, colorFn) {
  if (!THREE) return null;
  const nodeRelSize = (config && config.size_3d) || 8;
  const radius = Math.cbrt(1) * nodeRelSize; // nodeVal defaults to 1
  const color = (colorFn && colorFn(node)) || '#8b949e';

  // wiki → octahedron (config-gated). Wiki wins even if also _anchor-tagged.
  const shape = (config && config.wiki_shape) || 'octahedron';
  if (node.type === 'wiki') {
    if (shape !== 'octahedron') return null; // sphere override → ForceGraph default
    const geo = new THREE.OctahedronGeometry(radius, 0);
    // MeshBasicMaterial: unlit, transparent:false (default) → depthWrite:true → solid rendering
    const mat = new THREE.MeshBasicMaterial({ color });
    return new THREE.Mesh(geo, mat);
  }

  // P3.7 / item #6: anchored/protected memories get a distinct GEOMETRY (cube)
  // vs the default sphere. is_protected is absent from the graph payload, so the
  // '_anchor' tag (added by restoration.anchor_memory alongside is_protected) is
  // the available signal. Gated on type==='memory' so untyped/temporal stay null.
  if (node.type === 'memory' && Array.isArray(node.tags) && node.tags.includes('_anchor')) {
    const side = radius * 1.4; // slightly larger so anchors read as prominent
    const geo = new THREE.BoxGeometry(side, side, side);
    const mat = new THREE.MeshBasicMaterial({ color });
    return new THREE.Mesh(geo, mat);
  }

  // Plain memory / other → null → ForceGraph default sphere (cheap; CPU-safe).
  // .nodeColor(_nodeColorFor) still tints the default sphere, so per-node DIM
  // works on these via the color path (dimmed → solid dark RGB) without a mesh.
  return null;
}
