# Viz galaxy layout — backend-authoritative rework

**Status:** REVIEW (no build until approved)
**Date:** 2026-07-20
**Task:** #72 · **ADR:** [[yadgar-adr-0152]] (accepted 2026-07-20)
**Branch/worktree:** `feat/viz-layout-backend` @ `/home/max/git/yadgar/.claude/worktrees/viz-layout-backend` (off master @ core 5.155.0 / backend 5.56.1)
**Target version:** core **5.156.0** / backend **5.57.0** (this PR touches BOTH sides)

> Locked by ADR-0152 (do not re-litigate): galaxy layout becomes BACKEND-AUTHORITATIVE. `graph_layout.py` computes/caches positions + recomputes nightly; `/api/graph` serves x/y/z; the CLIENT (`galaxy-view.js`) becomes a pure renderer (stops computing on load). Live layout-control sliders = **OPTION A** (server recompute on slider release, debounced round-trip, ONE Python impl). Option B (client-live) deferred. Do NOT delete `graph_layout.py`.

---

## BLUF (read this first)

**This is a REWIRE, not a rebuild.** The backend layout chain is already fully wired and live:
`/api/graph` already computes, caches (nightly + full + startup-bootstrap), and **already attaches x/y/z** to every served node unconditionally (`viz_exec/__init__.py:65–78`). The `VIZ_PRECOMPUTED_LAYOUT_ENABLED` gate is already gone (ADR-0131). **The only reason the user sees client-computed positions is that `galaxy-view.js` throws the served x/y/z away and recomputes** (`layoutPositions()` line 461, called unconditionally from the scene constructor line 768). So the core of ADR-0152 — "backend authoritative, client renders served" — is ~70% already built on the backend; the genuinely-new work is: (a) fix 2 layout math bugs that exist on BOTH sides but will now be fixed once in Python, (b) make the client render served positions and stop computing, (c) a NEW parameterised slider-recompute endpoint, (d) 2 orthogonal pure-client render fixes.

**Genuinely new vs already-wired:**

| Piece | Status |
|---|---|
| Backend compute + cache (`graph_layout.py`, `ops.py` cache row) | ✅ ALREADY BUILT |
| Nightly + full + startup-bootstrap recompute | ✅ ALREADY WIRED (`consolidation/service.py:178`, `embed_service_lifecycle.py:170–211`) |
| `/api/graph` attaches x/y/z unconditionally | ✅ ALREADY WIRED (`viz_exec/__init__.py:65–78`) |
| Bug #4 arm-budget fix (`arms*3` cap) | 🔶 fix ONCE in `graph_layout.py:296` (client dup dies when client stops computing) |
| Bug #3a entity/wiki cluster-eligibility (hubs leave core) | 🔴 NEW — **build-size decision below (light vs heavy)** |
| Bug #3b core-node edge suppression | 🔴 NEW — needs a backend-stamped per-node `loose` flag (does not exist today) |
| Client renders served x/y/z + stops computing | 🔴 NEW (delete `layoutPositions` call path) |
| Slider recompute endpoint (Option A) | 🔴 NEW endpoint + parameterise `galaxy_layout()` |
| **Cache-miss / cold-start fallback** | 🔴 NEW + **critical correctness risk** (see Risk R1) |
| Bug #1 FOUC (CSS in `<head>` + hide-until-ready) | 🔴 NEW (pure client) |
| Bug #2 disk-point `NormalBlending` | 🔴 NEW (pure client, one-line-ish) |

**Recommended build order:** Car A (backend math + membership flag) → Car B (client renders served, remove client compute) — **A+B ship as a unit** → Car C (slider endpoint + debounced client wiring) → Car D (bug #1 CSS + bug #2 blending, pure client, parallelisable) → Car E (TDD sweep, version bump, CHANGELOG). Car D is independent of A/B/C and could land first as a quick win if desired.

**Two decisions the user must make before build** (both size the build):
1. **Bug #3a path** — light (connectivity via the already-present-but-ignored `edges` param) vs heavy (wire Louvain/wiki_crossref clustering). Plan recommends LIGHT. See Car A.
2. **Cache-miss fallback** — serve-time place-if-missing (backend) vs client sentinel placement. Plan recommends serve-time. See Risk R1.

---

## Current data-flow map (file:line for the 6 scoping answers)

### Q1 — Does `/api/graph` currently attach x/y/z? Gated?
**YES, unconditionally, no gate.** Handler is `_op_graph` in `yadgar/backend/viz_exec/__init__.py:31–79`. At **lines 65–78** it calls `attach_cached_positions(data, storage.get_graph_layout_cache())` whenever a cache row exists (wrapped non-fatal try/except). `attach_cached_positions` (`graph_layout.py:345–383`) stamps `data["layout_mode"]` (line 375) and mutates each node in place with `n["x"], n["y"], n["z"]` (lines 376–382). The `VIZ_PRECOMPUTED_LAYOUT_ENABLED` knob was REMOVED (ADR-0131) — referenced only in stale comments.

### Q2 — Does the client read served x/y/z or always recompute?
**Always recomputes; ignores served positions.** `layoutPositions()` at `galaxy-view.js:461–536`, called from `relayout()` (line 955) which is called from the `GalaxyScene` constructor (line 768) on every load. `buildNodeModel()` (lines 353–404) extracts only `id`, `type`, `heat`, timestamps, and `clusters[].member_node_ids` from the payload — **it never reads `node.x/.y/.z`**. So the served coordinates are dead on arrival.

### Q3 — Is the backend galaxy math the same buggy formula as the client?
**Yes — both bugs are mirrored, so fixing the backend fixes it once the client renders served positions.**
- **Bug #4 (arms×3 cap):** backend `graph_layout.py:296` `n_spine = min(len(ranked), arms * 3)` → 4 arms = 12 spine slots; clusters past slot 12 get `arm = -2` inter-arm scatter (line 298, 323–325). Client mirrors this at `galaxy-view.js:473` `Math.min(real.length, P.arms * 3)`. With ~769 clusters this is ~98% overflow to scatter.
- **Bug #3a (entity/wiki always core):** backend membership `_galaxy_node_membership` (`graph_layout.py:200–226`) reads `clusters[].member_node_ids` (line 222) with a ≥2-member gate (line 220). But `clusters[].member_node_ids` is **memory-only by construction** — `graph_edges.py:301` and `:346` build it as `[f"mem:{mid}" for …]`. Entity/wiki node ids never appear → they are always `loose=True` → always core bulge (`graph_layout.py:306–314`). Client hits the identical structural exclusion (it derives membership from the same memory-only `clusters[]`). **This is a payload/membership bug, not a `graph_layout` math bug — it is fixed upstream of the position math.**

### Q4 — Is recompute wired to nightly consolidation, or lazy-on-request?
**Wired to nightly + full + a startup bootstrap. Signature-gated. No lazy-on-request path.**
- `run_consolidation_cycle(mode)` (`consolidation/service.py:150–179`) tails both nightly and full paths with `_maybe_precompute_graph_layout(...)` (line 178); **light mode skips it** (line 172).
- `_maybe_precompute_graph_layout` (`service.py:72–147`) recomputes only if the full-graph **signature changed** OR `layout_mode` flipped (lines 117–122) — else no-op keep-cache.
- Startup bootstrap: `_bootstrap_graph_layout_if_empty` (`embed_service/embed_service_lifecycle.py:199–211`) spawns a daemon thread on boot that computes once if the cache is empty (`_run_layout_bootstrap`, lines 170–196), non-fatal.
- Cache rebuild trigger = graph-signature change on the next nightly/full cycle (or empty-cache boot).

### Q5 — Slider recompute: existing endpoint or must one be added?
**Must be ADDED.** The viz ops are all read-only (`viz_exec/__init__.py:139–145`): `_op_graph`, `_op_graph_stats`, `_op_graph_edges`, `_op_graph_neighborhood`, `_op_events`. Galaxy params are read from `VIZ_GALAXY_ARMS/SPIRAL_PITCH/CORE_DENSITY` settings (`service.py:131–133`) — never from a request. **No parameterised recompute path exists.** `galaxy_layout()` (`graph_layout.py:249–257`) already takes `arms`/`spiral_pitch`/`core_density` as function args, so parameterisation is a call-site change, not a signature change. See Car C for the client→backend→re-render round-trip.

### Q6 — Which sliders change POSITIONS (backend) vs pure RENDER (client)?
The layout-control panel (`#galaxy-side-panel` `.gsp-*`, index.html ~1635–1705) has 9 controls, wired at `galaxy-view.js:1278–1329`:

| # | id | label | Effect | Class |
|---|---|---|---|---|
| 1 | `g-arms` | Spiral arms | `P.arms` → arm count | **POSITION** (backend) |
| 2 | `g-pitch` | Spiral tightness | `P.pitch` → log-spiral winding | **POSITION** (backend) |
| 3 | `g-radmode` | Arm radius mapping (heat/age) | `P.radmode` → radial binning | **POSITION** (backend) |
| 4 | `g-thick` | Disk thickness | `P.thick` → z jitter | **POSITION** (backend) |
| 5 | `g-single` | Loose/single (core/halo) | `P.single` → core vs halo placement | **POSITION** (backend) |
| 6 | `g-coredens` | Core density | `P.coredens` → bulge packing | **POSITION** (backend) |
| 7 | `g-bulge` | Core-bulge size / glow | `P.bulge` → bulge exponent **+ glow scale** | **HYBRID** (position + render) |
| 8 | `g-layer` | Type z-layering | `P.layer` → per-type z offset | **POSITION** (backend) |
| 9 | `g-spin` | Auto-rotate speed | `P.spin` → camera azimuth Δ | **RENDER** (client-only; early-return at `galaxy-view.js:1289`) |

**Position sliders (need backend recompute under Option A):** arms, pitch, radmode, thick, single, coredens, layer (7) + the position half of bulge.
**Render sliders (stay client, no round-trip):** spin (9), + the glow half of bulge (7).

> ⚠️ **Slider-param gap:** `galaxy_layout()` today only parameterises arms/spiral_pitch/core_density. The other position sliders (radmode, thick, single, layer, bulge-position) are baked into the client `layoutPositions` and have **no backend equivalent yet**. Under Option A these must either (a) be added as `galaxy_layout()` params (Car C scope grows) OR (b) be scoped out of the first PR (sliders 1/2/6 = arms/pitch/coredens work via backend; the rest are deferred/disabled). **This is a Car C sizing decision — see Car C "Open question."**

---

## Car breakdown (ordered; each car = one coherent commit)

### Car A — Backend layout math fix + entity/wiki cluster-eligibility + membership flag
**Files:** `yadgar/backend/graph/graph_layout.py`, `yadgar/backend/graph/graph_edges.py` (or the membership seam), possibly `yadgar/backend/graph/graph_api.py`.

**Changes:**
1. **Bug #4 — arm budget.** Replace `n_spine = min(len(ranked), arms*3)` (`graph_layout.py:296`) so **every multi-member cluster maps to exactly one arm** (round-robin `i % arms`), with tight per-member jitter and one "home" radius/angle per cluster (ADR-0152: "every cluster → one arm, one home position, tight member jitter"). Remove the `arm = -2` inter-arm scatter overflow (lines 298, 323–325). Clusters share arms but each cluster sits at a coherent home band rather than random scatter.
2. **Bug #3a — entity/wiki cluster-eligibility.** Make hub entity/wiki nodes leave the core. **DECISION REQUIRED (build-size driver):**
   - **LIGHT (recommended):** Use the **already-present-and-already-passed `edges` param**. `galaxy_layout` is `# noqa: ARG001` at `graph_layout.py:251` but the caller **already passes real edges** — `service.py:127–129` calls `galaxy_layout(nodes, edges, clusters=…)` (edges is the 2nd positional arg, sourced from `get_full_graph()` at `service.py:108`). So the param is threaded through the call chain TODAY and just discarded. Light path = un-`noqa` and consume it: assign each entity/wiki node to the arm of its **dominant neighbour cluster** (the arm most of its edges point into); truly-single (0-edge) entity/wiki → core. Matches ADR-0152's "hubs leave the core / truly-single → core" verbatim, NO new clustering pipeline, NO new plumbing. Scope: ~1 new helper in `graph_layout.py`.
   - **HEAVY (alternative):** Wire a real entity/wiki clustering signal into `clusters[].member_node_ids` upstream (`graph_edges.py:301/346`). Signals that EXIST: Louvain community detection over entity relationships (`sleep_compute/community.py:80–156`, uses `co_occurrence`/`resolved_by`/`caused_by`/`derived_from`), and `wiki_crossref` for wiki-wiki. Scope: new cluster-payload assembly + a membership source merge + determinism/perf review. Materially bigger; defer unless the light path's placement quality is poor on smoke-check.
3. **Membership flag for #3b (see Car A/B coupling).** `galaxy_layout()` currently returns `{node_id: [x,y,z]}` only (line 342); `attach_cached_positions` stamps only x/y/z (lines 376–382). **Stamp a per-node `loose`/membership flag** into the cached layout so the client (Car B) can suppress core-node edges from a single backend source of truth. Two sub-options: extend the cache value to `{node_id: {pos, loose}}`, or stamp `n["loose"]` at attach time by recomputing membership in the viz path. Prefer caching the flag alongside positions (compute-side, no serve-time recompute).

4. **Bust the signature cache (see Risk R6 — REQUIRED or the fix no-ops on deploy).** `graph_signature(nodes, edges)` (`graph_layout.py:66`, called `service.py:112`) hashes graph SHAPE only — not layout code or params. Shipping new Car A math will NOT recompute on the nightly unless the graph shape changed that day. Fold a `_LAYOUT_VERSION` constant **and** the galaxy params (arms/spiral_pitch/core_density) into the signature input so shipping new math (or changing a `VIZ_GALAXY_*` setting) auto-invalidates the cache. Bump `_LAYOUT_VERSION` in this PR.

**Test approach:** pytest — unit-test `galaxy_layout()` with a fixture of N clusters > arms asserting (a) every cluster maps to a real arm 0..arms-1 (no `-2`), (b) an entity/wiki node with edges into cluster-C lands on C's arm (light path), (c) a 0-edge entity → core, (d) the returned/cached structure carries the `loose` flag. Determinism test (seeded PRNG, sorted ids) must still pass. Signature test: bumping `_LAYOUT_VERSION` or a galaxy param changes `graph_signature` output on identical graph shape.

**Risk:** MEDIUM. Light-path connectivity placement is new logic in the hot precompute path — needs a determinism guarantee and a perf check at ~thousands of nodes (it already runs uncapped nightly). Membership-flag cache-shape change touches `ops.py` serialization + `attach_cached_positions` readers (schemaless row, but any consumer that unpacks positions must tolerate the new shape).

---

### Car B — `/api/graph` serves x/y/z + client renders served (remove client compute-on-load)
**Files:** `yadgar/core/static/galaxy-view.js` (primary), possibly `viz_exec/__init__.py` if the `loose` flag is stamped at serve time.

**Changes:**
1. **Client reads served positions.** In `buildNodeModel()`/`relayout()` (`galaxy-view.js:353–404`, 950–995): read `node.x/.y/.z` from the payload into `diskPos` instead of calling `layoutPositions()`. Keep the `layoutPositions()` function body ONLY if it becomes the cache-miss fallback (Risk R1) — otherwise delete it.
2. **Read the backend `loose` flag** (from Car A) for edge suppression + any core/halo rendering the client still does; stop deriving membership from `clusters[]` client-side (that derivation is the client half of the #3a/#4 bugs — it must die with client compute).
3. **Bug #3b — core-node edge suppression.** In `edgeSegments()` (`galaxy-view.js:659–706`) skip edges where both endpoints are backend-`loose` (the ADR-0152 "core-node edges suppressed" + the #216-removed `if(nd.single) continue;` restored, but keyed off the **backend flag**, not client-derived `single`).

**Test approach:** vitest (jsdom) — assert `buildNodeModel` consumes served `x/y/z` (add positions to the fixture payload, assert they reach `diskPos`); assert `edgeSegments` drops core-core edges given the `loose` flag; **flip the existing `layoutPositions` tests** — they pin client-computed positions and WILL break (mirror ADR-0138's "tests pinning the old reality must flip", not hollow them).

**Risk:** HIGH-ish coupling. **Car A + Car B are NOT independently shippable as an improvement** — A alone = better backend layout the client still ignores; B alone = client faithfully renders the *current buggy* backend math. Separate commits are fine but they ship together. The `loose` flag is the single seam threading A (stamp) → B (read); get its shape right in A.

---

### Car C — Slider server-recompute endpoint (Option A) + debounced client wiring
**Files:** `yadgar/backend/viz_exec/__init__.py` (new op), `yadgar/backend/graph/graph_layout.py` (parameterise), `yadgar/core/static/galaxy-view.js` (slider handlers 1278–1329).

**Changes:**
1. **New backend op** `_op_graph_layout` (or `graph_relayout`) accepting `{arms, spiral_pitch, core_density, …}` in the request body. It calls `galaxy_layout(nodes, edges, clusters, arms=…, …)` with the **per-request overrides** and returns `{node_id: [x,y,z], loose-flags}` — **it MUST NOT write the `graph_layout_cache:current` singleton** (that row is canonical/nightly; overwriting it hands one user's slider fiddle to everyone AND makes the signature-gate no-op the next nightly). Read-and-compute, return, discard.
2. **Parameterise `galaxy_layout()` call.** Function already takes the args (lines 253–255); the new op passes request values instead of `VIZ_GALAXY_*` settings.
3. **Client wiring.** Position sliders (arms/pitch/coredens confirmed-parameterisable; see gap below) → `debouncedRelayout()` becomes a **debounced POST** on slider *release* → apply returned x/y/z to `diskPos` and re-render (no client recompute). Render sliders (spin, glow-half of bulge) stay client-instant.

**Open question (Car C sizing):** only arms/spiral_pitch/core_density are `galaxy_layout()` params today. The other position sliders (radmode, thick, single, layer, bulge-position — Q6) have **no backend equivalent**. Decide: (a) add them as `galaxy_layout()` params (Car C grows, more Python math), or (b) first PR ships backend recompute for arms/pitch/coredens only and the rest are deferred or converted to render-only/disabled. **Recommend (b)** — ship the 3 confirmed-parameterised sliders end-to-end, defer the rest to a follow-up, note it in CHANGELOG.

**Test approach:** pytest — the new op returns positions for given params without touching the cache (assert `get_graph_layout_cache` unchanged after call); param overrides actually change output. vitest — slider release fires one debounced request (fake timers), response applied to positions.

**Risk:** MEDIUM. Singleton-cache-overwrite is the sharp edge (guard with a test). Debounce/round-trip latency is the ADR-0152 revisit-trigger (if too slow → Option B). Payload size: recompute round-trip returns positions for all nodes each release — fine for cached-node counts, watch at uncapped scale.

---

### Car D — Bug #1 (FOUC CSS) + Bug #2 (node blending) — pure client quick fixes
**Files:** `yadgar/core/static/index.html`, `yadgar/core/static/galaxy-view.js`, (`galaxy-view.css` unchanged content).

**Changes:**
1. **Bug #1 FOUC.** `galaxy-view.css` is injected late in `_buildDom()` (`galaxy-view.js:776–781`, appends a `<link>` to `document.head` inside the scene constructor, which runs only AFTER `/api/graph` returns). The `#galaxy-side-panel` markup is always-on in `index.html` (~1602–1630) so it flashes unstyled. Fix: add `<link rel="stylesheet" href="./galaxy-view.css">` to `index.html <head>` (remove the JS injection, or keep it idempotent-guarded as fallback), AND hide the panel + canvas until data/positions are ready (gate on positions-ready — this ALSO covers the cold-start blank from Risk R1).
2. **Bug #2 node blending.** Disk-point material uses `THREE.AdditiveBlending` (`galaxy-view.js:940`) + continuous auto-spin (`P.spin`, line 1383) → per-pixel additive overlap re-sums as the camera orbits = the colour flicker. Fix: disk points → `THREE.NormalBlending`. **DO NOT touch the core-glow sprites** — `coreGlowMat` (line 870) and `coreGlow2Mat` (line 881) are SEPARATE Sprite materials that legitimately need `AdditiveBlending` for the halo. Only `pointMat` (line 940) changes.

**Test approach:** vitest (jsdom) — assert `pointMat.blending === NormalBlending` and both core-glow mats stay `AdditiveBlending`; assert `index.html <head>` contains the `galaxy-view.css` link; assert the panel/canvas start hidden and reveal on data-ready. Render appearance (flicker gone, no smudge) = user smoke-check (no browser harness).

**Risk:** LOW. Independent of A/B/C — can land first as a quick win. **Smoke-check caveat:** `NormalBlending` makes overlapping disk points dimmer/flatter (same tradeoff as the v5.154 edge NormalBlending fix) — user should eyeball node brightness.

---

### Car E — TDD sweep + version bump + CHANGELOG
**Files:** `pyproject.toml` (core → 5.156.0), backend version slot (→ 5.57.0; located via `scripts/check_versions.py`/`sync_version.py`), `CHANGELOG.md`, ADR back-reference.

**Changes:** run full gates (`ruff`, import-linter, `check_versions`, vitest, viz pytest, touched-suite pytest); flip the client `layoutPositions` tests (Car B) to the served-render reality; confirm the R6 signature-bust test is green; bump both versions (`scripts/bump_version.py`); write the CHANGELOG entry (backend NO LONGER "UNCHANGED" — first backend bump since 5.56.1). Update ADR-0152 status/consequences if scope shifted (e.g. #3a light path chosen, sliders 4-8 deferred).

**Build-time TODO:** the backend `5.56.1` version slot was NOT located during scoping (greps of `pyproject.toml`/source came back empty — core `5.155.0` lives in `pyproject.toml:7`; backend version is elsewhere). Locate it via `scripts/check_versions.py` / `scripts/sync_version.py` before bumping — do not stall.

**Test approach:** the gate run itself. Confirm no red pre-existing suites are silenced.

**Risk:** LOW, except the test-flip volume (Car B breaks every vitest that pins client-computed positions — budget for it, mirror ADR-0138 precedent).

---

## Risks (cross-cutting)

**R1 — Cache-miss / cold-start / intra-day nodes (CRITICAL, not flagged by any RCA).**
`attach_cached_positions` only sets x/y/z `if coords and len(coords) >= 3` (`graph_layout.py:376–382`) — **uncached nodes get no position.** Today that is harmless because the client computes for everyone. The moment the client becomes a pure renderer (Car B), **every uncached node renders at origin/undefined**: (a) fresh-deploy cold start before the bootstrap thread finishes (~19s blank per the v5.88 benchmark), and (b) every node created since the last nightly precompute. ADR-0131 explicitly KEPT the client `layoutPositions` as "the REQUIRED seed-miss fallback"; ADR-0152 removes exactly that. **DECISION REQUIRED:**
- **Serve-time place-if-missing (recommended):** if the cache is missing nodes, compute positions for the missing ones at serve time (or trigger a lazy recompute) before returning. Keeps the client pure.
- **Client sentinel placement:** client parks uncached nodes at origin/outer-shell, they settle on the next nightly (consistent with ADR-0134's accepted "intra-day nodes sit near origin" precedent). Cheapest, but visible until next nightly.
This connects to Car D bug #1: "hide until data ready" should gate on **positions ready**, which also masks the cold-start blank. **Must be resolved before Car B build.**

**R2 — #3a/#3b coupling (design, resolved in-plan).** If the backend takes membership authority (Car A), the client's old `clusters[]`-derived `single` flag DIVERGES (backend puts a hub on an arm; client still sees it as not-in-cluster → suppresses its edges → mismatch). Resolution baked into the plan: backend stamps ONE `loose` flag (Car A), client reads THAT for both positioning and edge suppression (Car B). Do NOT restore the old client-derived `if(nd.single) continue;` as-is.

**R3 — Singleton cache overwrite (Car C).** Slider recompute must not write `graph_layout_cache:current`. Guard with a test.

**R4 — Slider param coverage (Car C).** Only 3 of 7 position sliders are backend-parameterised today. Recommend shipping those 3 end-to-end, deferring the rest — else Car C grows into a Python port of the full client `layoutPositions` math.

**R5 — Test-flip volume (Car B/E).** Removing client compute breaks all vitest that pin `layoutPositions` output. Expected, not a regression — flip them.

**R6 — Signature-gate no-op on deploy (CRITICAL — fix appears to do nothing).** The nightly recompute is gated: `_maybe_precompute_graph_layout` recomputes ONLY if `graph_signature` changed or `layout_mode` flipped (`service.py:117–122`). But `graph_signature(nodes, edges)` (`graph_layout.py:66`) hashes **graph shape only** — not the layout code or the `VIZ_GALAXY_*` params. Consequence: after Car A ships new arm-budget/membership math, on any day the graph shape is unchanged the nightly **no-ops and keeps serving the OLD buggy positions**. The user deploys, reloads, sees no change, thinks the fix is broken. (Same latent bug means changing `arms`/`pitch`/`density` in settings never takes effect today either.) **Fix (folded into Car A, verified in Car E):** add a `_LAYOUT_VERSION` constant + the galaxy params to the `graph_signature` input so new math / param changes auto-invalidate. Alternative: a manual `consolidate_now(mode='full')` in the deploy notes — fragile, not recommended. This is a hard prerequisite: without it the entire PR is invisible on any graph-shape-stable day.

---

## Already-done / do-not-rebuild (explicit)

- Backend compute + cache + nightly/full/bootstrap recompute + signature-gate: **built, working** (ADR-0010/0131/0134). Do not touch except Car A math + the `loose` flag.
- `/api/graph` x/y/z attach: **already unconditional** — no gate to remove, no attach to add. Car B is purely client-side (read what's already served).
- `VIZ_PRECOMPUTED_LAYOUT_ENABLED`: **already removed** (ADR-0131). Do not re-add.
- Edge additive-blend whiteout: **already fixed** in v5.154.0 (#216) — that was the EDGE `LineSegments` material. Bug #2 here is the SEPARATE disk-point NODE material (`pointMat` line 940). Do not conflate.

---

## Open decisions for approval (block build)

1. **Bug #3a path:** LIGHT (connectivity via ignored `edges` param) — *recommended* — vs HEAVY (Louvain/wiki_crossref clustering pipeline).
2. **R1 cache-miss fallback:** serve-time place-if-missing — *recommended* — vs client sentinel placement.
3. **Car C slider coverage:** ship arms/pitch/coredens end-to-end + defer radmode/thick/single/layer/bulge — *recommended* — vs port all 7 to backend params now.

Once decided, build order: **A+B (unit) → C → D → E** (D parallelisable/first-if-desired).
