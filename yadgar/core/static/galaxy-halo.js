/**
 * galaxy-halo.js — pure math for the galaxy node-selection halo (Car D #4).
 *
 * LANDMINE (audit): the popup mockup's pulsing halo is a CSS ::after on a faux
 * DOM node, but the real galaxy is a WebGL Points cloud with NO per-node DOM.
 * The halo is therefore implemented as a THREE object (billboard Sprite) added
 * to the galaxy scene at the picked node's WORLD position (galaxy-view.js
 * GalaxyScene.showHalo). Because the diskPoints have no transform (added at
 * origin) and the "spin" rotates the CAMERA (MiniOrbit.addAzimuth), not the
 * disk, a world-space halo tracks the node correctly as the camera orbits — no
 * per-frame reprojection needed.
 *
 * These pure helpers cover the two bits of math that are unit-testable without
 * THREE/WebGL:
 *   - ndcToScreen:  project.() gives NDC coords; this converts NDC → screen px
 *                   (used ONCE at click to place the popup near the node).
 *   - haloScale:    pulsing scale factor over time (mirrors the mockup's
 *                   ring-pulse / galaxy-pulse keyframes).
 */

'use strict';

/**
 * Convert normalized-device-coordinates (from THREE Vector3.project(camera)) to
 * screen pixel coordinates. NDC x,y ∈ [-1,1] with +y up; screen y is flipped
 * (0 = top). When ndcZ is supplied, onscreen is false if it falls outside the
 * clip volume [-1,1] (behind camera / beyond far plane).
 *
 * @param {number} ndcX
 * @param {number} ndcY
 * @param {number} width   viewport width in px
 * @param {number} height  viewport height in px
 * @param {number} [ndcZ]  optional clip-space depth for onscreen test
 * @returns {{x:number, y:number, onscreen:boolean}}
 */
export function ndcToScreen(ndcX, ndcY, width, height, ndcZ) {
  const x = ((ndcX + 1) / 2) * width;
  const y = ((1 - ndcY) / 2) * height;
  const onscreen =
    ndcZ === undefined || (ndcZ >= -1 && ndcZ <= 1 && ndcX >= -1 && ndcX <= 1 && ndcY >= -1 && ndcY <= 1);
  return { x, y, onscreen };
}

/**
 * Pulsing halo scale over time. Oscillates around `base` between 0.82× and
 * 1.12× on a ~1.8s period (mirrors the mockup ring-pulse keyframes).
 *
 * @param {number} base     base scale (world units)
 * @param {number} nowMs    animation clock (ms)
 * @returns {number} scaled value in [base*0.82, base*1.12]
 */
export function haloScale(base, nowMs) {
  const period = 1800;
  const phase = ((nowMs % period) / period) * Math.PI * 2;
  const s = 0.97 + 0.15 * Math.sin(phase); // ∈ [0.82, 1.12]
  return base * s;
}
