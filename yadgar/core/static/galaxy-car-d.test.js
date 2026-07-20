/**
 * galaxy-car-d.test.js — ADR-0152 Car D static-asset guards (bug #1 FOUC + bug #2).
 *
 * The GalaxyScene render materials + the index.html <head> are not exercisable in
 * jsdom (no WebGL), so per repo convention we assert the source ARTIFACTS instead
 * of a live render: (1) galaxy-view.css is linked in index.html <head> so the
 * always-on panel is styled on first paint; (2) the disk-point material uses
 * NormalBlending (not additive) while the core-glow sprites STAY additive.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const read = (f) => readFileSync(resolve(here, f), 'utf8');

describe('Car D bug #1 (FOUC): galaxy-view.css linked in <head>', () => {
  const html = read('index.html');
  const head = html.slice(html.indexOf('<head>'), html.indexOf('</head>'));
  it('index.html <head> links galaxy-view.css', () => {
    expect(head).toMatch(/<link[^>]+href="galaxy-view\.css"/);
  });
  it('reveals the panel/canvas on a body.galaxy-ready gate', () => {
    const css = read('galaxy-view.css');
    expect(css).toMatch(/body:not\(\.galaxy-ready\)/);
    const js = read('galaxy-view.js');
    expect(js).toMatch(/classList\.add\('galaxy-ready'\)/);
  });
});

describe('Car D bug #2: disk-point material NormalBlending; core-glow stays additive', () => {
  const js = read('galaxy-view.js');
  it('pointMat uses NormalBlending (kills the additive spin flicker)', () => {
    // pointMat is the ShaderMaterial for the disk points; its blending line must
    // be NormalBlending. Assert the pointMat block carries NormalBlending.
    const block = js.slice(js.indexOf('this.pointMat = new THREE.ShaderMaterial'));
    const matEnd = block.indexOf('});');
    expect(block.slice(0, matEnd)).toMatch(/blending: THREE\.NormalBlending/);
  });
  it('core-glow sprite materials keep AdditiveBlending (halo layer untouched)', () => {
    for (const mat of ['coreGlowMat', 'coreGlow2Mat']) {
      const block = js.slice(js.indexOf(`this.${mat} = new THREE.SpriteMaterial`));
      const matEnd = block.indexOf('});');
      expect(block.slice(0, matEnd)).toMatch(/blending: THREE\.AdditiveBlending/);
    }
  });
});
