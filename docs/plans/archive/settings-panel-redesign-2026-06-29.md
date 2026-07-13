> ARCHIVED 2026-07-13 — SHIPPED as v5.89.0 (#66) — chrome-style settings panel + config-source fix.

# Settings Panel Redesign + Config Bug Fixes

> **STATUS: IN REVIEW** — plan + mock under review by the user. No code written yet.
> Deliverable this round = this plan + the reviewable mock
> (`docs/plans/settings-panel-redesign.mockup.html`, open in a browser).
> **Supersedes `docs/plans/viz-config-control-panel.md`** (see §8).

**Theme:** viz / config / ops
**Target version:** unassigned (stamp at ship)
**Mock:** [`settings-panel-redesign.mockup.html`](./settings-panel-redesign.mockup.html)
**Replaces:** [`viz-config-control-panel.md`](./viz-config-control-panel.md) (Neural Console — aesthetic rejected; backend resolution carried forward)

---

## 0. Why this plan exists

User feedback on the live config tab (verbatim intent): the current **flat 8-column
table** is "ugly and impractical." They want a settings UI "very similar to Chromium
or Firefox settings" — a **left-nav of categories**, a **fast search**, and **grouped
sections**. Plus three bugs around the config/viz surface need fixing.

Current state (verified against `master`):

- **Config UI (D):** `yadgar/static/control.js` renders an 8-column table
  (`KNOB | TYPE | CURRENT | DEFAULT | SOURCE | RELOAD | EDIT | REF`, control.js:369–380),
  grouped by `knob.category` via `groupKnobsByCategory` (control.js:145).
  `CATEGORY_ORDER` (control.js:117–120) = `retrieval, write-path, brain-dynamics,
  enrichment, gate, wiki, viz, observability, ops, config`. A search box **already
  exists** (`.ctrl-filter`, control.js:349) backed by `filterKnobs` (control.js:56).
  Per-knob metadata is assembled by `_enrich_knob`
  (`yadgar/server/routes/control.py:130–149`).
- An **earlier mock + plan exist** (`viz-config-control-panel.{mockup.html,md}`,
  "Neural Console"). The IA was sound; the aesthetic (Fraunces serif, amber→coral heat
  gradients, grain/grid/glow) is **maximalist and exactly what the user is rejecting.**
  This plan keeps the IA, discards the skin.

**The redesign is mostly a frontend job — but it has one hard backend prerequisite**
(BUG A). The 3-way source badge the user needs (Default / YAML / ENV-locked) is
**impossible to render correctly** until `ConfigEntry.source()` becomes yaml-aware and
the env-write trap is removed. That is why BUG A is **P0**, ahead of the redesign.

---

## 1. The three bugs

### BUG A — env-write trap (P0, critical, blocks the redesign)

**What happens.** The `POST /api/control/config` handler writes the knob to yaml
(`set_config_value(yaml_key, raw_value)`, control.py:404) and then **also mutates the
process environment** as a hot-reload hack:

```python
# yadgar/server/routes/control.py:416
os.environ[entry.name] = value_str
```

This corrupts the source model in two ways:

1. **Self-locking 409.** After the POST, `entry.source()`
   (config_registry.py:52–54) checks **only** `os.environ` and now returns `"env"`.
   `_enrich_knob` (control.py:130–149) marks the knob `locked`, and the next POST to
   that knob is rejected **409** by the env-lock guard (control.py:370–382). The user
   edits a knob once, then can't edit it again until restart.
2. **Phantom default after restart.** `_raw_value()` (config_registry.py:56–58) reads
   `os.environ.get(name, default)` — **never the yaml file.** After a restart the env
   var is gone, so the UI shows the knob as `default` / source=`default`, even though
   the daemon **correctly loaded the yaml value** at boot via
   `Settings.settings_customise_sources` (config.py:849–862, precedence
   env > yaml > default). UI and reality disagree.

**Root cause.** `ConfigEntry` has **zero yaml-awareness.** `source()` is binary
(env-or-default) and `_raw_value()` reads only `os.environ`. The env-write at :416 was
a hack to fake hot-reload by smuggling the new value into the one place `ConfigEntry`
reads.

**Fix (three parts):**

1. **Delete control.py:416.** A POST must never mutate `os.environ`. (env is the
   highest-precedence, machine/nix-owned layer — writing it from an HTTP POST is a
   layering violation and the source of the self-lock.)
2. **Hot-reload via the settings cache, not env.** `get_settings()`
   (config.py:869–870) is `@lru_cache(maxsize=1)`. After `set_config_value`, call
   `get_settings.cache_clear()` so the next `get_settings()` re-reads the yaml.
   That is the legitimate hot-reload for any code path that reads through
   `get_settings()`. (Knobs read once at boot still need a daemon restart — see the
   restart pill in §2; the restart buttons already exist.)
3. **Make `ConfigEntry` yaml-aware** — the linchpin of the 3-way badge:
   - `source()` → return `"env"` if `name in os.environ`; else `"yaml"` if the key is
     present in the loaded yaml config; else `"default"`.
   - `_raw_value()` → return the **env** value if set, else the **yaml** value if set,
     else the default — mirroring the real precedence in
     `settings_customise_sources`.
   - **Source of yaml truth:** read it through the same machinery `Settings` already
     uses — `YamlConfigSource` / the loaded `get_settings()` object — rather than
     re-parsing the yaml file in `ConfigEntry`. The `Settings` object already holds the
     yaml-resolved values; the missing piece is *attribution* (which layer won), which
     means `ConfigEntry` needs to know **whether the key was present in the yaml layer**
     (distinct from whether the effective value happens to equal the default). A thin
     helper — "is this key set in the yaml file?" — fed by the same yaml load is the
     clean shape. **Open decision O1** (§6) covers exactly where that helper lives.

**Why this is P0.** The mock's Default / YAML(saved) / ENV-locked badge — and the rule
that YAML rows are editable+resettable while ENV rows are read-only — is a **direct
projection of a correct `source()`.** Build the redesign on today's binary
env-or-default `source()` and every saved knob mislabels itself. Backend first.

**TDD notes.**
- Red: a test that POSTs a knob, asserts `source()=="yaml"` (not `"env"`), asserts a
  second POST to the same knob succeeds (no spurious 409), and asserts that after
  clearing `os.environ` + `get_settings.cache_clear()` the knob still reads its yaml
  value with `source()=="yaml"`. This fails today on every assertion.
- Red: a test that a genuinely env-set knob still reports `source()=="env"` and is
  `locked` (the lock must survive the fix — we only want to stop *fake* env-locks).
- I25 three-way-sync (`yadgar/tests/test_config_three_way_sync.py`): the fix changes
  `source()`/`_raw_value()` behavior, not `ConfigEntry`'s field shape, so I25 should
  hold — **but run it**; if `as_dict()` output shifts, reconcile there.
- Green: implement parts 1–3; re-run the control-route + config-registry suites.

### BUG B — config tab blank on refresh (P2, real race)

**What happens.** Refresh the page while on the config tab (URL hash is `#control`).
The boot IIFE (index.html:3969–3977) runs:

```js
await loadVizConfig();          // one fetch
_switchTab(_getActiveTab());    // _getActiveTab() → 'control' from the hash
```

`_switchTab('control')` calls `window._lazyInitControlTab?.()` (index.html:3775). But
`window._lazyInitControlTab` is defined inside the **deferred `<script type="module">`**
(index.html:3991–4044, defined at 4029), whose execution is gated on an **8-import
module graph** (tabs/info/overlays/bookmarks/control/config-ref/graph-detail/help).
If the single `loadVizConfig()` fetch resolves **before** that module graph finishes
executing, `_lazyInitControlTab` is still `undefined`, the optional-call `?.()`
**no-ops**, and the control pane never initializes → **blank pane.** A later
user-driven tab switch works because by then the module has executed.

**The false premise is in the code's own comment** (index.html:3987–3988):

> "the boot IIFE awaits loadVizConfig() before calling `_switchTab` — ensuring modules
> are loaded before first tab switch."

Awaiting **one localhost fetch** guarantees **nothing** about a deferred 8-module
graph. A cached/fast fetch racing a cold module parse is not an edge case; it is the
likely ordering. (Note: a fresh load on `#home` masks the bug — `_getActiveTab()`
returns `'home'`, so `_lazyInitControlTab` is never called during boot. The bug only
bites when the active tab at boot **is** control, i.e. refresh-on-config.)

**Fix.** Have the **module render the active tab once it has loaded**: at the end of the
module script, if `_getActiveTab() === 'control'`, call `_lazyInitControlTab()` (and
the same for `config-ref`). This fires control-init only when control is genuinely the
active tab, so:
- refresh-on-config → module loads → initializes the pane (bug fixed);
- fresh-load-on-home → still skips the probe → the gated `GET /api/control/config` 403
  is still **not** triggered on home, so `test_no_uncaught_js_errors`
  (the reason the init is lazy, index.html:4021–4026) **stays green.**

Prefer this over "gate the whole boot on a module-ready promise" — the latter is
messier and risks reintroducing the 403 on home. **Don't rely on fetch-vs-parse
timing.**

**TDD notes.** No browser harness (see §4). This is DOM-wiring, not pure logic — cover
it with a small vitest test of the decision helper: *"given active tab = control and
module-ready, init is called; given active tab = home, init is not called."* Extract
the decision into a pure function so it's testable without a DOM.

### BUG C — View menu shows 1 of 5 overlays (P2, unfinished feature)

**What happens.** `initViewMenu()` (index.html:3937–3964) and its markup
(index.html:1156–1163) hard-wire **only** the `clusters` overlay toggle. There are
**five** floating overlays in the DOM, each tagged
`.floating-overlay[data-overlay-name]`:

| overlay | line |
|---|---|
| `heat-slider` | index.html:1230 |
| `graph-stats` | index.html:1242 |
| `node-types` | index.html:1257 |
| `edge-legend` | index.html:1271 |
| `clusters` | index.html:1289 |

The View menu can toggle only the last one. This is an **unfinished feature, not a
regression.**

**Fix.** Build the View-menu items by iterating
`document.querySelectorAll('.floating-overlay[data-overlay-name]')`, render a checkbox
per overlay (label from `data-overlay-name`), and toggle the `.overlay-hidden` class on
each (the show/hide class the overlays already use; `.floating-overlay.overlay-hidden
{ display:none }`). Reflect each overlay's current visibility in the checkbox initial
state.

**TDD notes.** Extract the "given N overlay elements, produce N menu-item descriptors"
mapping into a pure function and vitest it (count + names + initial checked-state).
The DOM event wiring stays thin.

---

## 2. The settings redesign

Replace the 8-column table in `control.js` with a **chrome://settings / about:preferences**
layout. Familiar, restrained, practical — the opposite of the Neural Console skin.

### Information architecture

- **Left category rail** — the existing `CATEGORY_ORDER` categories (retrieval,
  write-path, brain-dynamics, enrichment, gate, wiki, viz, observability, ops, config;
  add mcp-tools / security if knobs warrant). Each row shows the category name + a
  **count of knobs**. Selectable; active category gets a subtle accent (left bar + bg
  tint + accent text — **no glow**).
- **Fast search** (the headline feature) — a prominent input at the top that filters
  **across ALL categories** as you type (chrome://settings behavior), not just the
  active one. Matching settings render as a flat list, each labeled with its **category
  chip**, with the matched substring **highlighted**. Matches against knob **name +
  label + description**. Clearing search restores the category view. The existing
  `filterKnobs` (control.js:56) is the seed; it must be generalized from
  filter-within-group to **filter-across-all**.
- **Grouped setting rows** — within a category, optionally sub-grouped by `section`
  (small subheading). Each row: **left** = human label + description (muted) + raw
  knob `name` in mono + source badge; **right** = the typed control.

### Per-row controls (by `kind`)

| kind | control |
|---|---|
| `bool` | toggle switch (accent when on) |
| `int` / `float` | slider + small number input (synced), with min/max labels |
| `string` (enum, `enum_choices` present) | `<select>` |
| `string` (free) | text input |

### The 3-way source model (depends on BUG A)

Exactly three badge states, one per row, driven by the **fixed** `source()`:

- **Default** — value equals built-in default. Editable.
- **YAML (saved)** — value saved to yaml. Editable **and** shows **Reset to default**
  (⟲). Reset reverts to the built-in default.
- **ENV-locked** — value set via environment / nix. Control is **read-only / disabled**,
  red lock badge, tooltip: *"Set via environment / nix — edit there, not here."*
  No reset.

This mapping is only correct once `source()` returns the right of the three (BUG A).
The mock shows at least one of each so the user can review the distinction.

### Reset / apply / restart

- **Reset-to-default** per changed/YAML row (⟲) — reverts the control and decrements
  the pending count.
- **Pending-changes bar** (sticky, appears only when there are unsaved edits): count of
  unsaved edits + **Discard** + **Apply**. Apply fires the existing
  `POST /api/control/config` per changed knob.
- **Restart** — when any pending change is **restart-required**, the bar also shows a
  **Restart daemon** button (amber) with a confirm ("restarting drops the live
  connection"). Ties to the **already-shipped** restart endpoints
  (`POST /api/control/restart/{yadgar|backend}`). Once BUG B is fixed, the restart
  buttons render reliably on refresh.
- **Restart-required pill** — knobs that need a restart to take effect show a small
  `↻ restart required` pill. This maps to `_enrich_knob`'s `reload` field
  (restart-required) which **already exists** — no new metadata needed for the pill.

---

## 3. API deltas

**Mostly none — the gap is in `source()`/value semantics, not new fields.**

`_enrich_knob` (control.py:130–149) **adds** five fields — `description`, `section`,
`category`, `locked`, `enum_choices` — onto a knob dict that already carries `name`,
`kind`, `current`, `default`, `source`, `reload` (restart-required). Net per-knob
payload: all eleven fields. `SECTION_TO_CATEGORY` (control.py:61–109) already maps
sections → the rail categories. **That payload is sufficient for the entire redesign IA,
controls, restart pill, and reset affordance.**

The single backend change the redesign actually needs is **BUG A's `source()` /
`_raw_value()` yaml-awareness** (§1). Once `source()` is correct, `locked` (derived
from `source()=="env"`) and the 3-way badge fall out automatically. No new endpoint, no
new fields, no second write path.

- **Write path:** reuse the existing `POST /api/control/config` (one knob per call,
  coerces type, range-validates, writes yaml). **Do not** add a parallel
  `PATCH /admin/config` — that was the over-build trap the superseded plan corrected.
- **Restart:** reuse the existing `POST /api/control/restart/{yadgar|backend}`.

---

## 4. Composition with the no-browser-harness convention

The repo has **no browser test harness**; frontend logic is tested by extracting **pure
helpers** and unit-testing them with **vitest**, keeping DOM wiring thin. The redesign
fits this cleanly:

- **Reuse** the existing exported pure helpers — `filterKnobs` (control.js:56),
  `groupKnobsByCategory` (control.js:145) — already vitest-covered. Generalize
  `filterKnobs` to filter-across-all-categories and add a **match-highlight** helper
  (pure: input string + query → segments to mark) — both vitest-testable.
- New pure helpers to extract + test: source→badge mapping
  (`source, locked → {label, editable, resettable}`); pending-changes reducer
  (rows + edits → count + dirty set); the BUG C overlay→menu-descriptor mapping; the
  BUG B active-tab/module-ready decision.
- DOM construction (rail, rows, controls, sticky bar) stays a thin render layer over
  these helpers — minimal untested surface.

**Red → green → refactor** per the test-driven rule. Bugs A and C and the pure-helper
parts of the redesign all start with a failing test.

---

## 5. Phasing

- **P0 — BUG A backend fix (HARD BLOCKER for badges).** Delete control.py:416; add
  `get_settings.cache_clear()` after `set_config_value`; make `source()` /
  `_raw_value()` yaml-aware; fix the self-lock 409 / phantom-default. Ships correct
  source attribution on the **existing** table immediately, and unblocks the redesign's
  3-way badge. **The redesign must not start its badge work until P0 lands.**
- **P1 — the redesign.** New chrome-style layout in `control.js`: left rail + search +
  grouped rows + typed controls + 3-way badges + reset + pending bar + restart wiring +
  restart pill. Generalize `filterKnobs`; add the pure helpers above.
- **P2 — UI bugs B + C (ride along).** B (refresh-blank) and C (View-menu loop) are
  independent of A/P1 and can land alongside P1 or just after; neither blocks the
  redesign. Both are small.

Rationale: badges depend on A → A is P0. The redesign (P1) is the bulk of the effort. B
and C are small, orthogonal, and can be batched with P1.

---

## 6. Open decisions (for the user)

- **O1 — where yaml-awareness lives.** `source()` needs to answer "is this key present
  in the yaml layer?" Options: (a) `ConfigEntry` consults a small "yaml keys present"
  set populated once from the same yaml load `Settings` uses (clean, single read,
  preferred); (b) re-parse the yaml file inside `ConfigEntry` per call (simple, but
  re-reads the file and can drift from `Settings`' resolution). Recommend (a). **Decide
  before P0 implementation.**
- **O2 — hot-reload scope.** `get_settings.cache_clear()` makes knobs read *through*
  `get_settings()` hot-reload. Knobs captured once at boot (module-level
  `settings = get_settings()` snapshots, e.g. `server/_app.py:15`,
  `server/_helpers.py:21`) still need a restart. Do we (a) keep the restart pill honest
  per knob (the `reload` field already encodes this) and accept a mixed model, or (b)
  invest in eliminating boot-time snapshots so more knobs hot-reload? Recommend (a) for
  this round — the pill already tells the truth.
- **O3 — rail vocabulary.** Use the existing `CATEGORY_ORDER` (10 categories) as-is, or
  align to the 15-capability vocabulary the superseded plan referenced
  (`docs/CAPABILITY_REGISTRY.md`)? The shipped `SECTION_TO_CATEGORY` already targets the
  10. Recommend: keep the 10 shipped categories; revisit only if the user wants the
  capability vocabulary surfaced.
- **O4 — search default view.** chrome://settings opens on a category; some prefer a
  flat "all settings" landing. Recommend: open on the first category (familiar);
  search is the cross-cutting escape hatch.
- **O5 — destructive knobs.** The superseded plan proposed an armed two-step confirm
  for destructive knobs (e.g. `COLD_MEMORY_PURGE_ENABLED`). Out of scope for this
  round (no `destructive` metadata exists today). Defer; flag if the user wants it.

---

## 7. Brutal notes

- **The user already has a search box and category grouping.** `.ctrl-filter` +
  `groupKnobsByCategory` exist today. The complaint is **presentation**, not missing
  features. This redesign is ~80% CSS/markup reskin + ~20% logic (cross-category search,
  3-way badges). Don't oversell it as a from-scratch build, and don't let it balloon
  into a backend project — the only required backend change is BUG A.
- **BUG A is the only thing that genuinely *must* ship before the pretty UI.** Everything
  else is cosmetic or independent. If we ship the redesign on the broken `source()`, the
  green/red badges will lie — worse than the ugly-but-honest table. Resist shipping
  P1 badges before P0.
- **The env-write at :416 is a hot-reload hack that created two bugs to solve one
  problem.** Deleting it + `cache_clear()` is strictly better and smaller. Don't
  preserve the env-write "to be safe" — it's the root cause.
- **BUG B is a latent race the code comment actively misrepresents** as safe. Fixing the
  symptom (init on module-load) is right; we should also delete/replace the false
  comment so the next person doesn't re-trust fetch-vs-parse ordering.
- **Don't build a 3rd overlapping config plan.** This supersedes
  `viz-config-control-panel.md` (§8). One plan, one mock, going forward.
- **The Neural Console mock is genuinely well-built but wrong for this user.** Reusing
  its IA is the right call; reusing its CSS would re-ship the thing the user called
  "ugly and impractical" (different flavor of ugly — maximalist instead of flat, but
  still not chrome://settings).

---

## 8. Supersede / reconcile with `viz-config-control-panel.md`

This plan **supersedes** `docs/plans/viz-config-control-panel.md` ("Neural Console").

**Carried forward (still correct):**
- The information architecture: left category rail + search + grouped rows + source
  badges + pending tray.
- Its **AUDIT pivot**: do **not** build a parallel `/admin/config PATCH` + new panel.
  Extend the **existing** `/api/control/` surface + `control.js`. The live write path is
  `POST /api/control/config`; the live read is `GET /api/control/config` via
  `_enrich_knob`. That resolution stands — this plan does not relitigate the endpoint.
- The two-population model (yaml-backed editable vs env-only locked) — now expressed as
  the 3-way source badge.

**Dropped / changed:**
- **The entire Neural Console aesthetic** (Fraunces serif, amber→coral heat gradients,
  grain/grid/glow, scientific-instrument vibe) — the user rejected maximalism. New skin
  = flat GitHub-dark chrome://settings. New mock: `settings-panel-redesign.mockup.html`.
- The armed destructive-confirm flow → deferred (O5), no `destructive` metadata exists.
- The `restart_required`/`destructive` net-new `ConfigEntry` fields → not needed for
  this round: the restart pill uses the existing `reload` field.

**Action:** mark `viz-config-control-panel.md` as superseded by this plan (header note)
when this plan exits review.

---

## References

- `yadgar/server/routes/control.py` — `_enrich_knob` (130–149), `SECTION_TO_CATEGORY`
  (61–109), env-lock 409 (370–382), `set_config_value` call (404), **env-write trap
  (416)**, `POST`/`GET /api/control/config`, `POST /api/control/restart/*`.
- `yadgar/config_registry.py` — `ConfigEntry`, `source()` (52–54), `_raw_value()`
  (56–58), `as_dict()`.
- `yadgar/config.py` — `Settings` (50), `settings_customise_sources` (849–862,
  precedence env>yaml>default), `get_settings()` `@lru_cache` (869–870).
- `yadgar/static/control.js` — 8-col table (369–380), `filterKnobs` (56),
  `groupKnobsByCategory` (145), `CATEGORY_ORDER` (117–120), `.ctrl-filter` (349).
- `yadgar/static/index.html` — boot IIFE (3969–3977), `_switchTab` (3744–3777),
  `_getActiveTab` (3736–3742), module script (3991–4044), `_lazyInitControlTab` (4029),
  `initViewMenu` (3937–3964), View markup (1156–1163), 5 overlays (1230/1242/1257/1271/1289).
- `yadgar/static/overlays.js` — `.overlay-hidden` toggle pattern.
- `yadgar/tests/test_config_three_way_sync.py` — I25.
- `docs/plans/viz-config-control-panel.md` — superseded plan (data-model reference).
- `docs/plans/settings-panel-redesign.mockup.html` — this round's reviewable mock.
