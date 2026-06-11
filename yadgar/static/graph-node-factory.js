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
  const shape = (config && config.wiki_shape) || 'octahedron';
  if (node.type !== 'wiki' || shape !== 'octahedron') return null;

  const nodeRelSize = (config && config.size_3d) || 8;
  const radius = Math.cbrt(1) * nodeRelSize; // nodeVal defaults to 1

  const geo = new THREE.OctahedronGeometry(radius, 0);
  const color = (colorFn && colorFn(node)) || '#8b949e';
  // MeshBasicMaterial: unlit, transparent:false (default) → depthWrite:true → solid rendering
  const mat = new THREE.MeshBasicMaterial({ color });
  return new THREE.Mesh(geo, mat);
}
