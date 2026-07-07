/**
 * config-ref.js — Config Reference tab implementation (v5.87.0 car B2b).
 *
 * Renders a searchable, grouped reference for every yadgar config knob.
 * Each knob gets an anchor id="cfgref-<name>" that the per-knob help icon
 * in control.js can scrollIntoView() after switchTab('config-ref').
 *
 * Exports:
 *   buildConfigRefModel  — pure: group + map knobs to reference entries
 *   initConfigRefTab     — DOM: render or fetch+render into root element
 *
 * Wire-in: imported by index.html module block; lazy-initialised on first
 *   navigation to #config-ref via window._lazyInitConfigRefTab.
 */

import { groupKnobsByCategory } from './control.js';

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

/**
 * Build the reference model: ordered groups of knob reference entries.
 * Reuses groupKnobsByCategory for consistent ordering with the config editor.
 *
 * @param {Array<Object>} knobs - knob objects from GET /api/control/config
 * @returns {Array<{category: string, label: string, knobs: Array<Object>}>}
 */
export function buildConfigRefModel(knobs) {
  const groups = groupKnobsByCategory(knobs);
  return groups.map(group => ({
    category: group.category,
    label: group.label,
    knobs: group.knobs.map(k => ({
      name:         k.name,
      description:  k.description || '',
      kind:         k.kind,
      default:      k.default ?? '',
      category:     k.category || group.category,
      enum_choices: k.enum_choices || [],
    })),
  }));
}

// ---------------------------------------------------------------------------
// DOM rendering
// ---------------------------------------------------------------------------

/**
 * Minimal element factory — mirrors control.js _el to avoid importing DOM utils.
 * @param {string} tag
 * @param {Object} [attrs]
 * @param {string} [text]
 * @returns {HTMLElement}
 */
function _el(tag, attrs = {}, text = '') {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text) el.textContent = text;
  return el;
}

/**
 * Render a single knob reference card into the given parent element.
 * All knob data is written via textContent — never innerHTML.
 *
 * @param {Object} entry - reference entry from buildConfigRefModel
 * @param {HTMLElement} parent - container to append into
 */
function _renderKnobCard(entry, parent) {
  const section = _el('section', {
    class: 'cfgref-knob',
    id: `cfgref-${entry.name}`,
  });

  const nameEl = _el('h3', { class: 'cfgref-knob-name' });
  nameEl.textContent = entry.name;
  section.appendChild(nameEl);

  const desc = _el('p', { class: 'cfgref-knob-desc' });
  desc.textContent = entry.description || '—';
  section.appendChild(desc);

  const meta = _el('dl', { class: 'cfgref-knob-meta' });

  const addMeta = (label, value) => {
    const dt = _el('dt');
    dt.textContent = label;
    const dd = _el('dd');
    dd.textContent = value;
    meta.appendChild(dt);
    meta.appendChild(dd);
  };

  addMeta('Type', entry.kind);
  addMeta('Default', String(entry.default));
  addMeta('Category', entry.category);

  if (entry.enum_choices && entry.enum_choices.length > 0) {
    const dt = _el('dt');
    dt.textContent = 'Choices';
    const dd = _el('dd');
    const ul = _el('ul', { class: 'cfgref-enum-list' });
    for (const choice of entry.enum_choices) {
      const li = _el('li');
      li.textContent = choice;
      ul.appendChild(li);
    }
    dd.appendChild(ul);
    meta.appendChild(dt);
    meta.appendChild(dd);
  }

  section.appendChild(meta);
  parent.appendChild(section);
}

/**
 * Render the config reference into root using the provided knobs array.
 * If knobs is null/undefined, fetch from GET /api/control/config first.
 *
 * @param {HTMLElement} root - the #tab-config-ref div (or cfgref-body container)
 * @param {Array<Object>|null} [knobs] - pre-fetched knob array (optional)
 */
export async function initConfigRefTab(root, knobs) {
  if (!root) return;

  // Render into .cfgref-body if it exists (real app); otherwise directly into root (tests)
  const host = root.querySelector('.cfgref-body') || root;
  host.innerHTML = '';

  if (!knobs) {
    const token = (typeof localStorage !== 'undefined') ? localStorage.getItem('yadgar_token') : '';
    const headers = Object.assign(
      { 'Content-Type': 'application/json' },
      token ? { Authorization: `Bearer ${token}` } : {},
    );
    try {
      const r = await fetch('/api/control/config', { headers });
      if (r.status === 403 || r.status === 401) {
        const banner = _el('p', { class: 'cfgref-banner cfgref-banner--warn' });
        banner.textContent = '⚠ Config Reference requires YADGAR_DEBUG_APIS_ENABLED=on';
        host.appendChild(banner);
        return;
      }
      if (!r.ok) {
        const banner = _el('p', { class: 'cfgref-banner cfgref-banner--error' });
        banner.textContent = `⚠ Config Reference — error ${r.status} fetching config`;
        host.appendChild(banner);
        return;
      }
      const data = await r.json();
      knobs = data.knobs || [];
    } catch (err) {
      const banner = _el('p', { class: 'cfgref-banner cfgref-banner--error' });
      banner.textContent = `⚠ Config Reference — network error: ${err.message}`;
      host.appendChild(banner);
      return;
    }
  }

  const model = buildConfigRefModel(knobs);

  for (const group of model) {
    const groupEl = _el('div', { class: 'cfgref-group' });

    const header = _el('h2', { class: 'cfgref-group-header' });
    header.textContent = group.label;
    groupEl.appendChild(header);

    for (const entry of group.knobs) {
      _renderKnobCard(entry, groupEl);
    }

    host.appendChild(groupEl);
  }
}
