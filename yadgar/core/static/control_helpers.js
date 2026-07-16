/**
 * control_helpers.js — pure helpers for the chrome://settings-style config panel
 * redesign (v5.89) + the Bug B / Bug C decision helpers.
 *
 * The viz frontend has no browser test harness; logic lives here as pure
 * functions (vitest-covered in control_helpers.test.js) and control.js wires a
 * thin DOM layer over them.
 *
 * Knob shape = real GET /api/control/config contract (control.py _enrich_knob):
 *   { name, kind, current, default, source, reload, description, section,
 *     category, locked, enum_choices }
 *
 * Categories are presented ALPHABETICALLY (user requirement — both the left rail
 * and the grouped content view).
 */

/**
 * Title-case a category key. Hyphen-separated words are split + each Title-cased.
 * 'write-path' → 'Write Path', 'brain-dynamics' → 'Brain Dynamics'.
 * @param {string} cat
 * @returns {string}
 */
export function categoryLabel(cat) {
  return String(cat || 'config')
    .split('-')
    .map(w => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ');
}

/**
 * The category of a knob, defaulting to 'config'.
 * @param {Object} knob
 * @returns {string}
 */
export function knobCategory(knob) {
  return knob.category || 'config';
}

/**
 * Cross-category search: filter knobs whose name, description, or category
 * contains the (case-insensitive) query. Empty query returns the full list.
 *
 * @param {Array<Object>} knobs
 * @param {string} query
 * @returns {Array<Object>}
 */
export function searchKnobs(knobs, query) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return knobs.slice();
  return knobs.filter(k =>
    String(k.name || '').toLowerCase().includes(q) ||
    String(k.description || '').toLowerCase().includes(q) ||
    String(k.category || '').toLowerCase().includes(q),
  );
}

/**
 * Split text into render segments for <mark> highlighting. Every (case-insensitive)
 * occurrence of query becomes a marked segment; the rest are unmarked. Empty query
 * (or no match) → a single unmarked segment of the whole string.
 *
 * @param {string} text
 * @param {string} query
 * @returns {Array<{text: string, mark: boolean}>}
 */
export function highlightSegments(text, query) {
  const s = String(text ?? '');
  const q = String(query || '');
  if (!q) return [{ text: s, mark: false }];

  const lc = s.toLowerCase();
  const lq = q.toLowerCase();
  const segments = [];
  let i = 0;
  let idx = lc.indexOf(lq, i);
  while (idx !== -1) {
    if (idx > i) segments.push({ text: s.slice(i, idx), mark: false });
    segments.push({ text: s.slice(idx, idx + q.length), mark: true });
    i = idx + q.length;
    idx = lc.indexOf(lq, i);
  }
  if (i < s.length) segments.push({ text: s.slice(i), mark: false });
  if (segments.length === 0) return [{ text: s, mark: false }];
  return segments;
}

/**
 * Count knobs per category (missing category → 'config').
 * @param {Array<Object>} knobs
 * @returns {Object<string, number>}
 */
export function categoryCounts(knobs) {
  const counts = {};
  for (const k of knobs) {
    const cat = knobCategory(k);
    counts[cat] = (counts[cat] || 0) + 1;
  }
  return counts;
}

/**
 * Alphabetically-ordered category descriptors for the left rail.
 * @param {Array<Object>} knobs
 * @returns {Array<{category: string, label: string, count: number}>}
 */
export function alphabeticalCategories(knobs) {
  const counts = categoryCounts(knobs);
  return Object.keys(counts)
    .sort((a, b) => a.localeCompare(b))
    .map(category => ({ category, label: categoryLabel(category), count: counts[category] }));
}

/**
 * Group knobs by category (alphabetical), with knobs sub-grouped by section
 * (sections alpha-ordered, knobs alpha by name within a section).
 *
 * @param {Array<Object>} knobs
 * @returns {Array<{category, label, count, sections: Array<{section, knobs}>}>}
 */
export function groupKnobsAlphabetical(knobs) {
  const byCat = new Map();
  for (const k of knobs) {
    const cat = knobCategory(k);
    if (!byCat.has(cat)) byCat.set(cat, []);
    byCat.get(cat).push(k);
  }

  return [...byCat.keys()]
    .sort((a, b) => a.localeCompare(b))
    .map(category => {
      const catKnobs = byCat.get(category);
      const bySection = new Map();
      for (const k of catKnobs) {
        const section = k.section || 'misc';
        if (!bySection.has(section)) bySection.set(section, []);
        bySection.get(section).push(k);
      }
      const sections = [...bySection.keys()]
        .sort((a, b) => a.localeCompare(b))
        .map(section => ({
          section,
          knobs: bySection.get(section).slice().sort((a, b) => a.name.localeCompare(b.name)),
        }));
      return { category, label: categoryLabel(category), count: catKnobs.length, sections };
    });
}

/**
 * Derive the 3-way source-badge state of a knob.
 * env (or locked) → read-only red lock; yaml → green + resettable; default → grey.
 *
 * @param {Object} knob - needs { source, locked }
 * @returns {{state, label, editable, resettable, locked}}
 */
export function deriveBadgeState(knob) {
  const isEnv = knob.locked === true || knob.source === 'env';
  if (isEnv) {
    return { state: 'env', label: 'ENV', editable: false, resettable: false, locked: true };
  }
  if (knob.source === 'yaml') {
    return { state: 'yaml', label: 'YAML', editable: true, resettable: true, locked: false };
  }
  return { state: 'default', label: 'Default', editable: true, resettable: false, locked: false };
}

/**
 * Which typed control a knob needs.
 * bool → toggle; int/float → slider; string+enum_choices → select; string → text.
 *
 * @param {Object} knob
 * @returns {'toggle'|'slider'|'select'|'text'}
 */
export function controlKind(knob) {
  if (knob.kind === 'bool') return 'toggle';
  if (knob.kind === 'int' || knob.kind === 'float') return 'slider';
  if (Array.isArray(knob.enum_choices) && knob.enum_choices.length > 0) return 'select';
  return 'text';
}

/**
 * Pending-changes reducer. Compares an original-values map to a current-values
 * map (keyed by knob name) and reports the dirty set + whether any dirty knob is
 * restart-required (reload === 'restart_required') + how many dirty knobs are
 * destructive (Car D — surfaced in red in the pending bar).
 *
 * Values are compared as strings so '8' and 8 don't spuriously differ.
 *
 * @param {Array<Object>} knobs    - knob list (for reload + destructive lookup)
 * @param {Object} originalValues  - { name: value }
 * @param {Object} currentValues   - { name: value }
 * @returns {{count: number, dirty: Set<string>, restartRequired: boolean, destructiveCount: number}}
 */
export function computePending(knobs, originalValues, currentValues) {
  const reloadByName = new Map(knobs.map(k => [k.name, k.reload]));
  const destructiveByName = new Map(knobs.map(k => [k.name, isDestructive(k)]));
  const dirty = new Set();
  let restartRequired = false;
  let destructiveCount = 0;
  for (const name of Object.keys(currentValues)) {
    if (String(currentValues[name]) !== String(originalValues[name])) {
      dirty.add(name);
      if (reloadByName.get(name) === 'restart_required') restartRequired = true;
      if (destructiveByName.get(name)) destructiveCount += 1;
    }
  }
  return { count: dirty.size, dirty, restartRequired, destructiveCount };
}

/**
 * Car D: whether a knob is destructive (retention/purge/DLQ pruning). The GET
 * /api/control/config response carries a `destructive` boolean per knob.
 * @param {Object} knob
 * @returns {boolean}
 */
export function isDestructive(knob) {
  return !!(knob && knob.destructive);
}

/**
 * Car D armed-state reducer. Returns a NEW Set (pure — input is never mutated)
 * with `name` added when `armed` is truthy, removed otherwise. The set tracks
 * which destructive rows the user has armed via the typed-confirm control.
 *
 * @param {Set<string>} armedSet - current armed names
 * @param {string} name          - knob name to arm/disarm
 * @param {boolean} armed        - desired armed state
 * @returns {Set<string>}
 */
export function toggleArmed(armedSet, name, armed) {
  const next = new Set(armedSet);
  if (armed) next.add(name);
  else next.delete(name);
  return next;
}

/**
 * Car D: defensively classify a POST /api/control/config response. A 428 means
 * the knob is destructive and the write must be re-sent armed. We treat ANY 428
 * as needs-arming (even if a proxy stripped the JSON body), reading the hint
 * when present.
 *
 * @param {{status: number, body?: Object}} response
 * @returns {{needsArming: boolean, destructive: boolean, hint: string}}
 */
export function classify428(response) {
  const status = response && response.status;
  const body = (response && response.body) || {};
  const needsArming = status === 428;
  return {
    needsArming,
    destructive: needsArming || !!body.destructive,
    hint: body.hint || (needsArming ? 'resend with "armed": true' : ''),
  };
}

/**
 * Bug B decision helper: should a lazy tab init fire for the currently-active tab?
 * The control / config-ref probes hit a gated endpoint that logs a 403 to the
 * console (tripping test_no_uncaught_js_errors) unless that tab is actually the
 * active one. Init only when activeTab === tabName.
 *
 * @param {string} activeTab - the tab resolved from the URL hash at boot
 * @param {string} tabName   - the lazy tab this init guards ('control'|'config-ref')
 * @returns {boolean}
 */
export function shouldInitTab(activeTab, tabName) {
  return activeTab === tabName;
}

/**
 * Bug C mapping: given the floating-overlay elements, produce one View-menu
 * descriptor per overlay. Label is derived from the overlay's grip title (the
 * leading '⋮ ' marker and the trailing collapse glyph stripped), falling back to
 * the overlay's data-overlay-name. `checked` reflects current visibility
 * (an overlay carrying .overlay-hidden is NOT visible → checkbox unchecked).
 *
 * @param {Array<Element>} overlayEls - .floating-overlay[data-overlay-name] nodes
 * @returns {Array<{name: string, label: string, checked: boolean}>}
 */
export function overlaysToMenuDescriptors(overlayEls) {
  return Array.from(overlayEls).map(el => {
    const name = el.getAttribute('data-overlay-name') || '';
    const hidden = el.classList.contains('overlay-hidden');
    const grip = el.querySelector ? el.querySelector('.overlay-grip') : null;
    let label = name;
    if (grip && grip.textContent) {
      const cleaned = grip.textContent
        .replace(/⋮/g, '')
        .replace(/[−–-]\s*$/, '')
        .trim();
      if (cleaned) label = cleaned;
    }
    return { name, label, checked: !hidden };
  });
}
