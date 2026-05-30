# Migration Notes

## v5.10.10 — Viz polish: 2x 3D node size + auto-zoom-fit (2026-05-30)

Core 5.10.9 → 5.10.10. Backend unchanged at 5.4.0. No schema changes. No config changes. Plan: `docs/PLAN_V5_10_10_VIZ_NODE_SIZE_AND_ZOOM_FIT.md`.

### Why

User feedback after v5.10.9 LIVE (viz now functional):

> *"3d, make the nodes a bit bigger. 2x their current size. also when the page refreshes it zoomes in and not show all nodes and i have to zoom out to see them all. same zoom issue with 2d. coloring works very nicely in both 2d and 3d so that is fixed as well. dont touch :D"*

Two small polish fixes. No backend changes. Coloring completely untouched.

### What changed

**`yadgar/static/index.html`** — three additions, no removals:

1. Module-level flag: `let _zoomFitDone = false;` (after `_engineTickCount`).
2. 3D init block: `.nodeRelSize(8)` chained after `.backgroundColor(...)`. ForceGraph3D default is 4 — value 8 doubles the sphere radius.
3. `onEngineTick` callback extended in BOTH 2D and 3D init blocks:

   ```javascript
   .onEngineTick(() => {
     _engineTickCount++;
     if (!_zoomFitDone && _engineTickCount === 80) {
       _zoomFitDone = true;
       if (typeof graph.zoomToFit === 'function') {
         graph.zoomToFit(800, 50);
       }
     }
   })
   ```

4. `_zoomFitDone = false;` reset at top of `initGraph()` (alongside `_engineTickCount = 0`).
5. `_zoomFitDone = false;` reset at top of `loadGraph()` (so reload button re-fits).

**`yadgar/tests/test_viz_static_assets.py`** — `TestV51010VizPolish` class with 3 regression tests.

**Version bump files**: `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock`.

### Deploy procedure

Standard container rebuild + restart — same as every viz patch.

```bash
docker compose pull core || docker compose build core
docker compose up -d core
```

### Manual smoke procedure (post deploy, hard-refresh)

1. Open viz in browser → hard-refresh (`Ctrl+Shift+R`).
2. **3D mode**: nodes visibly larger than v5.10.9 (~2x area). After ~1 second, view auto-fits to show all nodes — no manual zoom-out needed.
3. **2D mode**: click "2D" button → same auto-fit behavior on initial load.
4. **Toggle**: switch 2D↔3D → each switch re-fits view on layout settle.
5. **Reload button** (↺): re-fits view after data reload.
6. **Coloring verification**: heat/wiki colors unchanged from v5.10.9 — same color distribution expected.

### Rollback

Revert to v5.10.9 container. No data migration needed.

---

## v5.10.9 — Viz orphan-edge filter (2026-05-30)

Core 5.10.8 → 5.10.9. Backend unchanged at 5.4.0. No schema changes. Plan: `docs/PLAN_V5_10_9_VIZ_ORPHAN_EDGE_FILTER.md`.

### Why

After v5.10.8 deploy + hard refresh, viz STILL showed nodes clumped at origin with 0 engine ticks. Live DevTools console pasted by user: `Uncaught Error: node not found: entity:172` from `force-graph.min.js:34398` during `f.links` resolution inside `_.update`. The library throws synchronously when any link references a node ID not in the node set. After the throw, simulation never advances — every downstream symptom (no ticks, clumped nodes, 0 tick count) cascades from this one crash.

All v5.10.7–v5.10.8 attempts (Lambert→Basic material, transparent fix, tick-count guard, mesh-leak removal) targeted downstream effects. None addressed the library crash.

Root cause: after v5.0.0 monolith split, `yadgar/graph_api.py` assembles nodes and edges from separate queries. Causal edges reference `entity:*` IDs — but entity nodes are never added to the node list in `get_full_graph()`. Every causal edge is therefore an orphan. One orphan crashes force-graph.

### What changed

**`yadgar/graph_api.py`** — after assembling `nodes` and `edges`, add orphan filter:

```python
# Before (v5.10.8):
return {"nodes": nodes, "edges": edges}

# After (v5.10.9):
node_ids = {n["id"] for n in nodes}
filtered_edges = [
    e for e in edges
    if e.get("source") in node_ids and e.get("target") in node_ids
]
orphan_count = len(edges) - len(filtered_edges)
if orphan_count > 0:
    logger.info("graph_api: dropped %d orphan edge(s) ...", orphan_count)
    yadgar_graph_api_orphan_edges_dropped_total.inc(orphan_count)
return {"nodes": nodes, "edges": filtered_edges}
```

**`yadgar/metrics.py`** — new counter:

```python
yadgar_graph_api_orphan_edges_dropped_total = Counter(
    "yadgar_graph_api_orphan_edges_dropped_total",
    "Total edges dropped by get_full_graph() because one or both endpoints "
    "were absent from the returned node set.",
    registry=_registry,
)
```

**`yadgar/static/index.html`** — in `loadGraph()`, before `graph.graphData(...)`:

```javascript
// After (v5.10.9):
const nodeIdSet = new Set(allNodes.map(n => n.id));
const beforeCount = allLinks.length;
allLinks = allLinks.filter(l => {
  const s = (l.source && l.source.id) || l.source;
  const t = (l.target && l.target.id) || l.target;
  return nodeIdSet.has(s) && nodeIdSet.has(t);
});
const dropped = beforeCount - allLinks.length;
if (dropped > 0) {
  console.warn(`[yadgar viz] dropped ${dropped} orphan edges ...`);
}
```

### Apply

No migration required. Static file change — deploy + hard refresh (`Ctrl+Shift+R` or `Cmd+Shift+R`) to clear browser cache.

### Manual smoke procedure

After deploy + hard refresh:

1. Open viz in browser (`http://localhost:5173` or configured port).
2. Open DevTools console (F12).
3. Reload viz via "↺ Reload" button.
4. Verify: **no** `Uncaught Error: node not found:` in console.
5. Verify: engine tick count > 0 visible (status bar should show non-zero).
6. Verify: nodes spread away from origin via force layout (not all clumped at center).
7. Verify: both 2D and 3D modes render (toggle via mode button).
8. Optional: check Prometheus `/metrics` endpoint for `yadgar_graph_api_orphan_edges_dropped_total` — non-zero value confirms the fix fired on real data (expected: equals number of causal edges in DB).

### Rollback

Revert this commit. Returns to v5.10.8 broken state with library crash. Not desirable unless this fix causes a new regression.

## v5.10.8 — Viz physics hang + mesh leak fix (2026-05-30)

Core 5.10.7.3 → 5.10.8. Backend unchanged at 5.4.0. No schema changes. Plan: `docs/PLAN_V5_10_8_VIZ_PHYSICS_AND_MESH_LEAK_FIX.md`.

### Why

v5.10.7.3 LIVE showed 700 graph nodes clumped at origin with all velocities `vx/vy/vz = 0` — force simulation never iterated. Browser DevTools also showed 2297 Mesh children in the scene Group for 700 nodes (3.3× node count accumulating on each filter cycle).

**Bug A root cause:** `onEngineStop` fired with `cooldownTicks=null` / `warmupTicks=0` — engine stopped immediately with zero iterations. The callback pinned `n.fx = n.x = 0, n.fy = n.y = 0` for all nodes. Future simulation restarts couldn't move them.

**Bug B root cause:** `resetLayout` called `graph.graphData({ nodes: [], links: [] })` then `setTimeout(() => graph.graphData(d), 50)`. ForceGraph3D doesn't dispose Three.js Mesh objects on the empty step — orphan meshes accumulated on every call.

### What changed

`yadgar/static/index.html`:

**Bug A — tick-count guard:**
```javascript
// Before (v5.10.7.3):
.onEngineStop(() => {
  for (const n of graph.graphData().nodes) {
    if (n.fx == null) { n.fx = n.x; n.fy = n.y; }
  }
});

// After (v5.10.8):
let _engineTickCount = 0;  // module scope, reset in initGraph
// ...
.onEngineTick(() => { _engineTickCount++; })
.onEngineStop(() => {
  if (_engineTickCount < 50) return;
  for (const n of graph.graphData().nodes) {
    if (n.fx == null) { n.fx = n.x; n.fy = n.y; }
  }
});
```

**Bug B — direct re-data:**
```javascript
// Before (v5.10.7.3) in resetLayout():
graph.graphData({ nodes: [], links: [] });
setTimeout(() => graph.graphData(d), 50);

// After (v5.10.8):
graph.graphData(d);
```

### Apply

```bash
cd /home/max/git/nix && nix-apply
```

### Verify (manual smoke — required post-deploy)

1. Open `http://localhost:42069/` — hard-refresh `Ctrl+Shift+R` to bust browser cache.
2. **3D mode:** nodes must spread across visible volume, NOT clumped at origin. Check mesh count via DevTools console:
   ```javascript
   graph.scene().children.find(c => c.type === 'Group').children.length
   ```
   Expected: ≈ node count (e.g. 700 nodes → ~700 meshes, NOT 2297).
3. **2D mode:** nodes laid out by force simulation — hexagons (wiki) + circles (memory) visible with link lines. NOT all at origin.
4. **Filter cycle:** apply a search term or tag toggle; nodes update. Re-check mesh count — should stay ≈ node count (NOT 2×/3× after multiple filter applications).
5. **Layout reset:** click Reset Layout button; nodes re-scatter and re-settle. Mesh count stable.

### Open questions for main thread

- Threshold of 50 ticks is a safe lower bound; library default cooldown is 15000 ticks typically. Observe in production — adjust if layout still pins prematurely or never pins.
- Bug B: `resetLayout` only — `applyFilters` calls `graph.graphData(d)` directly and was not using the empty-then-restore pattern. If mesh leak persists after filter cycles (not reset cycles), deeper investigation needed (possibly applyFilters path or library internal).
- Filter UX post-Bug-B: only static-asset test verifies the pattern is gone; actual filter rendering correctness requires manual smoke.

### Hard deadlines unchanged

PD-23 `migration_grace` expiry 2026-08-26 still requires v5.11.x handler before then.

---

## v5.10.7.3 — Revert v5.10.7 custom 3D node geometry (2026-05-30)

Core 5.10.7.2 → 5.10.7.3. Backend unchanged at 5.4.0. No schema changes. Plan: `docs/PLAN_V5_10_7_3_VIZ_REVERT_TO_DEFAULTS.md`.

### Why

v5.10.7 introduced `_makeNodeThreeObject` to render custom octahedron (wiki) + sphere (memory) meshes in 3D mode. v5.10.7.1 + v5.10.7.2 attempted material/transparent fixes. All three rendered as fragmented triangle shards per user verification. Reverting to ForceGraph3D's library-managed default solid spheres — last-known-good visual from v5.3.7.

### What changed

`yadgar/static/index.html`:
- Removed `_makeNodeThreeObject` function (was lines 805-824)
- Removed `.nodeThreeObject(_makeNodeThreeObject).nodeThreeObjectExtend(false)` from 3D init
- Simplified `_applySearchHighlight` 3D path: only `.nodeColor()` re-fires (no longer `.nodeThreeObject()` re-call)

`_nodeColorFor` + `.nodeColor()` retained — should apply heat colour to ForceGraph3D's default sphere material. May finally produce 3D heat coloring (which never worked historically with custom mesh in the way).

### Apply

```bash
cd /home/max/git/nix && nix-apply
```

### Verify

- `/health` returns `version=5.10.7.3`
- Open `http://localhost:42069/` in 3D mode + hard-refresh (`Ctrl+Shift+R`)
- Expect: SOLID coloured spheres for all nodes (wiki + memory both spheres now). NO fragments. Last-known-good visual from v5.3.7 restored.
- Bonus check: heat gradient may now be visible on sphere surfaces (was never working historically; restored as side-effect of dropping custom mesh).

### Lost functionality

- S2.2 shape distinction (octahedra for wiki vs spheres for memory in 3D) — gone. User explicitly OK'd uniform shapes.
- If shape distinction is wanted later, requires deeper ForceGraph3D + ThreeJS investigation. Three attempts at custom mesh today failed; deferred to v5.X+ pending further analysis.

### Hard deadlines unchanged

PD-23 `migration_grace` expiry 2026-08-26 still requires v5.11.x handler before then.

---

## v5.10.7.2 — 3D viz transparent flag fix (2026-05-30)

Core 5.10.7.1 → 5.10.7.2. Backend unchanged at 5.4.0. No schema changes. Plan: `docs/PLAN_V5_10_7_2_VIZ_LIGHTING_FIX.md` (originally drafted as separate `MeshLambertMaterial`-only fix; superseded by investigation that revealed deeper `transparent` flag issue).

### Why

v5.10.7.1's Lambert→Basic material swap was necessary (Lambert needs lights ForceGraph3D doesn't provide) but insufficient. Wiki nodes still rendered as fragmented triangle shards. Investigation 2026-05-30 found root cause: `MeshBasicMaterial({transparent: true})` puts mesh in WebGL transparent render pass even at `opacity: 1.0`. Three.js sorts objects back-to-front but does NOT sort triangles within a single mesh — for `OctahedronGeometry` (8 faces) back faces overdraw front → "shards" appearance.

### What changed

`yadgar/static/index.html` `_makeNodeThreeObject` (~line 823):

```javascript
// Before (v5.10.7.1):
new THREE.MeshBasicMaterial({ color, transparent: true, opacity: node.__dimmed ? 0.18 : 1.0 })

// After (v5.10.7.2):
new THREE.MeshBasicMaterial({ color, transparent: !!node.__dimmed, opacity: node.__dimmed ? 0.18 : 1.0 })
```

Non-dimmed nodes → opaque render pass → correct triangle ordering → solid mesh. Dimmed (search miss) nodes → transparent pass → translucent.

### Apply

Standard single-isolated-change cycle (per anchor `yadgar-dev-workflow-single-isolated-change-release-cycle`):

```bash
cd /home/max/git/nix && nix-apply
```

### Verify

- `/health` returns `version=5.10.7.2`
- Open viz at `http://localhost:42069/`, switch to 3D mode (hard refresh `Ctrl+Shift+R` to bust any browser cache)
- Expect: solid octahedra (wiki) + solid spheres (memory). All same color. No fragments.
- Heat-color gradient still NOT working (was never working in 3D historically; tracked as future work).

### Known follow-up

3D heat coloring + per-type shape distinction were the original S2.1/S2.2 intent. v5.10.7 introduced shape distinction (octahedra/sphere) but broke rendering. v5.10.7.1 fixed material. v5.10.7.2 fixed transparent flag. Solid nodes restored. Heat-color gradient never worked historically; new plan to address separately.

---

## v5.10.7.1 — Bundled hotfix: sentinel filter + viz lighting (2026-05-30)

Core 5.10.7 → 5.10.7.1. Backend unchanged at 5.4.0. No schema changes.

### What's new

Two hotfixes shipped together in a single release cycle.

**Fix 1 — SessionEnd sentinel slash-command tag filter:**

The `last_human_turns` field in session_end sentinels was being polluted by Claude Code slash-command output tags (`<local-command-caveat>`, `<local-command-stdout>`, `<local-command-stderr>`, `<command-name>`, `<command-args>`). These are injected into the transcript as user-role messages when slash commands (e.g. `/model`, `/mcp`) run. Effect: `extract_last_session_findings` was returning ~80% noise, ~20% real user-intent signal.

Extended `SKIP_TAGS` in `yadgar/hooks/session-end-capture.py` to cover all seven known slash-command tags. `SKIP_TAGS` is now a module-level `frozenset` referenced by both `_count_human_messages` (gate) and `_parse_user_content` (extraction) — previously only two tags were hardcoded inline.

**Fix 2 — 3D viz node lighting:**

Wiki nodes and memory nodes were rendering as dark fragmented triangle shards in 3D mode post-v5.10.7 deploy. Root cause: `_makeNodeThreeObject` used `THREE.MeshLambertMaterial`, which requires scene lighting (ambient + directional) to render colour. ForceGraph3D does not add scene lights by default. Changed to `THREE.MeshBasicMaterial` (unlit — colour always renders at set value regardless of scene lighting). One-line fix. Transparent/opacity semantics unchanged.

### Required action: none

No server restart, no `install_hooks` re-run, no schema migration. Reload the browser tab after the container image is updated.

### Manual smoke procedure

After deploying 5.10.7.1:

1. **Sentinel quality** (optional — only if you had noisy sentinels from a slash-command-heavy session): run a new session with slash commands, exit normally, start a fresh session and call `project_brief(mode="signals")`. The `last_human_turns` in the recommended action should contain your real prompts only — no `MCP dialog dismissed` or stdout/stderr noise. If you accumulated noisy sentinels under v5.10.7, you can manually `forget()` them using the `sentinel_id` from the action.
2. **3D viz**: open `http://127.0.0.1:42069` in 3D mode. Wiki nodes should render as solid purple octahedra; memory nodes as solid coloured spheres. No dark triangle fragments or near-invisible shapes.

### Files changed

- `yadgar/hooks/session-end-capture.py` — `SKIP_TAGS` constant (lines ~68–80) + both call sites updated
- `yadgar/static/index.html` — line ~818: `MeshLambertMaterial` → `MeshBasicMaterial`
- `yadgar/tests/test_session_end_capture.py` — 6 new sentinel-filter tests (section 11)
- `yadgar/tests/test_viz_static_assets.py` — `TestV510701LightingFix` class (2 tests)

## v5.10.7 — Viz UX fixes (2026-05-30)

Core 5.10.6 → 5.10.7. Backend unchanged at 5.4.0. No schema changes.

### What's new

Four long-standing viz UX bugs fixed (soak-observed since 2026-05-20):

- **S2.1**: 3D mode now colours nodes by heat. Previously all nodes rendered in the library's default colour.
- **S2.2**: Wiki nodes use `OctahedronGeometry` (8-sided, visibly faceted). Memory nodes remain spheres. Previously both looked like spheres at default zoom.
- **S2.3**: Semantic search now works in 3D mode. The search handler called `nodeCanvasObject` (2D-only), causing `TypeError: graph.nodeCanvasObject is not a function` in 3D mode. Fixed by branching on `_graphMode`.
- **S2.4**: Stats overlay now polls `/api/metrics/*` every 5 s while open. Previously heat histogram and consolidation charts were static after initial load.

### Required action: none

Pure frontend change. No server restart, no migration, no `install_hooks` re-run required. Reload the browser tab after the container image is updated.

### Manual smoke procedure

After deploying 5.10.7, open `http://127.0.0.1:42069` and verify:

1. **3D mode (default)**: nodes should show a colour gradient from cool blue (low heat) to warm orange/red (high heat). No uniform yellow/pale dots.
2. **Shape distinction**: wiki nodes are faceted octahedra (angular 8-sided shape). Memory nodes are smooth spheres. Toggle 2D mode (button top-right) to confirm 2D mode still renders hexagons for wiki and circles for memory.
3. **Search**: type a query in the search box and press Enter. Console: no `TypeError`. Matching nodes should be highlighted; non-matching nodes dimmed. "Clear" button appears.
4. **Stats panel**: click the "📊 Stats" button. Watch the heat distribution chart and consolidation chart for 10–15 s; they should refresh automatically every 5 s (count changes if memories were recently added/consolidated). Closing and reopening the panel should restart the refresh cycle.

### Files changed

- `yadgar/static/index.html` — all four fixes
- `yadgar/tests/test_viz_static_assets.py` — 10 new static-asset regression tests

## v5.10.6 — SESSION_END_CAPTURE sentinel-marker pattern (2026-05-30)

Core 5.10.5 → 5.10.6. Backend unchanged at 5.4.0. No schema changes.

### What's new

Session endings are now captured as filesystem sentinel markers in `~/.yadgar/session-ends/`.
On the next session start, the marker is imported into memory and surfaced as an
`extract_last_session_findings` recommended action in `project_brief(mode="signals")`.

### Required action: re-run install_hooks

The SessionEnd hook is new and must be registered in `~/.claude/settings.json`:

```
install_hooks(scope="global")
```

Or if using project scope:

```
install_hooks(project_directory="/your/project", scope="project")
```

This adds `SessionEnd` alongside the existing `Stop` hook in the global settings file.

### New directory (auto-created)

`~/.yadgar/session-ends/` — created automatically by the hook on first run.

Failed imports moved to `~/.yadgar/session-ends/failed/` after 3 retries.

Manual cleanup if needed: `rm ~/.yadgar/session-ends/*.json`

### New env knobs (all optional, defaults shown)

| Knob | Default | Purpose |
|------|---------|---------|
| `SESSION_END_CAPTURE_ENABLED` | `true` | Kill switch for entire feature |
| `SESSION_END_RETENTION_DAYS` | `30` | Auto-prune sentinels older than N days |
| `SESSION_END_SNIPPET_TURNS` | `5` | Last N human turns embedded in sentinel |
| `SESSION_END_MIN_MESSAGES` | `2` | Skip sentinel if session had fewer than N human messages |

### end_reason gating (intentional)

`end_reason=clear` and `end_reason=resume` are intentionally skipped — they are not
true session exits. Only `logout` and `other` write sentinels.

### Consuming a sentinel

When the next session's `project_brief(mode="signals")` emits `extract_last_session_findings`:

1. Read the transcript at `transcript_path` (if it still exists).
2. Synthesize key decisions/findings.
3. Call `memorize(content='...', context=<directory>, tags=['session-finding'])`.
4. Call `forget(memory_id=<sentinel_id>)` to consume the sentinel.

If transcript is gone (file rotated), the `suggested_call` directs `forget(sentinel_id)` only.
The sentinel's `last_human_turns` field still provides partial context.

## v5.10.5 — nightly cycle remaining bugs (2026-05-30)

Core 5.10.4 → 5.10.5. Backend unchanged at 5.4.0. No schema changes.

### Bug 1 — vacuum URL second call site

`v5.10.2` fixed `_log_consolidation_row` to read `YADGAR_DB_URL` from env. Two other
call sites were missed:

- `yadgar/scripts/nightly_cycle.py::main()` — the systemd entry point
- `yadgar/vacuum/__init__.py::cmd_vacuum_impl()` — called from nightly cycle step 4

Both used `getattr(args, "backend_url", "http://127.0.0.1:8080")` which returns the
hard-coded `:8080` literal when invoked without `--backend-url` (systemd unit path).
The backend runs on `:8000`; something else bound to `:8080` returned HTTP 307.

**Fix:** both sites now use `getattr(args, "backend_url", None) or os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8080")`.

**Effect:** `[vacuum] ERROR: backend at http://127.0.0.1:8080 is not reachable: HTTP 307` is eliminated. `YADGAR_DB_URL=http://127.0.0.1:8000` in the systemd unit is now respected end-to-end.

### Bug 2 — prune deletes just-created post snapshot

The nightly cycle creates a post-backup snapshot (`label="nightly-post"`) in step 5,
then prunes in step 6. `shutil.copytree(copy_function=copy2)` propagates the source
DB directory's mtime to the snapshot directory. The DB dir mtime is the last time the
DB was written — with core stopped for the cycle, this can be hours in the past.

`prune_snapshots` sorts by mtime descending; the just-created snapshot sorted as
"oldest" and was pruned immediately, leaving zero post snapshots.

**Fix:** `create_snapshot()` calls `target.touch()` after `copytree()`, stamping the
snapshot directory to the current time. This ensures a just-created snapshot always
sorts as newest regardless of source DB age.

**Retention semantics:** unchanged. `YADGAR_BACKUP_RETENTION` (default 3) still controls how many total snapshots survive. No config changes required.

### No action required for upgrade

Docker image tag: `openfantasy/yadgar:5.10.5`. No DB migrations. No config changes. Restart the core container.

## v5.10.4 — consolidate_now mode parameter (2026-05-30)

Core 5.10.3 → 5.10.4. Backend unchanged at 5.4.0. No schema changes.

### consolidate_now() — mode parameter (BEHAVIOR CHANGE)

`consolidate_now()` now accepts `mode='light'` (default) or `mode='full'`.

**Light mode (default):** runs `force_consolidate()` only — decay, episodes, merge, CLS, causal phases. Typical runtime <30 seconds. Correct for pre-shutdown flushes, debug runs, and queue-fill scenarios.

**Full mode:** `force_consolidate()` + sleep cycle (dream replay, community detection, cluster summaries, re-embedding, compression, auto-narrate) + anchor audit pass (if `ANCHOR_AUDIT_CONSOLIDATION_ENABLED=true`). Typical runtime 5–15 minutes. Use for deliberate maintenance before a multi-day break.

**Breaking behavior change:** previous `consolidate_now()` with no args ran the full sleep cycle every time. After this upgrade, no-arg calls only run consolidation. If you have scripts or workflows that relied on `consolidate_now()` triggering the sleep cycle, update them to `consolidate_now(mode='full')`.

**Timestamp fix:** `mode='full'` now sets `_consolidation._last_sleep_cycle` so the 6-hour nightly gate respects manual full cycles. Previously, calling `consolidate_now()` twice in rapid succession ran the sleep cycle twice.

### hook_runner.py — PreToolUse schema fix

`db-lockdown-check` hook now emits the current Claude Code PreToolUse schema:

```json
{"hookSpecificOutput": {"permissionDecision": "allow"}}
{"hookSpecificOutput": {"permissionDecision": "deny"}, "systemMessage": "..."}
```

Old schema (`{"decision": "allow"|"block"}`) caused `(root): Invalid input` validation noise on every Bash tool call. No behavior change — Claude Code was already failing-open on the allow path.

**Installed copy:** `.claude/hooks/hook_runner.py` was updated in-place. If you re-run `yadgar install_hooks`, it will copy the fixed source from `yadgar/scripts/hook_runner.py`.

### No action required for upgrade

Docker image tag: `openfantasy/yadgar:5.10.4`. No DB migrations. No config changes. Restart the core container.

## v5.10.3 — scan_db_for_secrets.py end-to-end fix (2026-05-29)

Core 5.10.2 → 5.10.3. Backend unchanged at 5.4.0. No schema changes.

### scan_db_for_secrets.py — now functional

The v5.10.2 backfill scan script had two bugs preventing it from running end-to-end:

1. **OTLP hang at exit**: `~/.yadgar/config.yaml` sets `otlp_endpoint: http://host.containers.internal:4318/v1/traces`.
   `yadgar/server/_app.py` calls `setup_tracing()` at module import time, which instantiates a
   `BatchSpanProcessor` that retries failed OTLP exports on exit (~10 s delay with exponential back-off).
   The HITS/Clean output line was printed but scrolled past `| tail -10` before the process exited.
   Fix: `os.environ.setdefault("YADGAR_OTLP_ENDPOINT", "")` at the top of the script before any yadgar import.

2. **Detection failure with --limit N**: DB rows are scanned in ascending ID order. Memory 519107
   (the known `ghp_` 33-char leak) is at position 2994 of 3147 rows. `--limit 200` only fetched
   IDs 1–200, never reaching the leak.
   Fix: `SELECT ... ORDER BY id DESC LIMIT N` — scans newest rows first where recent leaks live.

### Running the scan

```
~/.local/pipx/venvs/yadgar/bin/python scripts/scan_db_for_secrets.py --dry-run
```

- **NOT** system Python — uses pipx venv with mcp/surrealdb packages.
- Auto-sources `~/.config/yadgar/secrets.env` if `YADGAR_DB_USER`/`YADGAR_DB_PASS` not set.
- Auto-sets `YADGAR_DB_URL=http://127.0.0.1:8000` if not set (HTTP mode, avoids file-lock conflict).
- Read-only. Never mutates the DB. Reports to `~/.yadgar/secret-leak-scan-<TS>.txt`.
- Exit 0 = clean. Exit 1 = hits found. Review report; manually `forget(<id>)` for confirmed leaks.

### No action required on upgrade

---

## v5.10.2 — Secret-gate architecture + memorize parity + nightly cycle hotfix (2026-05-29)

Core 5.10.1 → 5.10.2. Backend unchanged at 5.4.0. No schema changes. Additive only.

### Security — Secret-gate architecture (I26)

Two-layer defence against accidental secret persistence:

**Layer 2 — API boundary**: `gate_or_reject(*content_fields)` in `yadgar/secrets.py` called at the top of all write tools (`memorize`, `anchor`, `checkpoint`, `update_active_work`, `bootstrap_project`, `wiki_update`, `agent_prompt_save`). Rejects before any storage or file-queue write.

**Layer 1 — Storage level**: `check_secrets()` called inside `insert_memory()` before the DB write. Raises `SecretLeakBlocked` on hit.

**DLQ handling**: `SecretLeakBlocked` classified as `permanent` in `_classify_error()` — file moves to DLQ after 3 attempts instead of infinite retry.

**Kill switch**: `YADGAR_SECRET_GATE_DISABLED=1` bypasses Layer 1 (not Layer 2). Logs `WARNING` at every boot iteration.

**Pattern thresholds tightened**:

| Pattern | Before | After |
|---|---|---|
| GitHub PAT `ghp_/gho_/ghu_/ghs_/ghr_` | `{36,}` | `{20,}` |
| Anthropic `sk-ant-` | `{32,}` | `{20,}` |
| OpenAI `sk-` | `{30,}` | `{20,}` |

**I26 invariant lint**: `python scripts/check_secret_gate.py` — run manually or via pre-commit. Exits 1 if any write tool is missing a gate.

**Backfill scan**: `python scripts/scan_db_for_secrets.py --dry-run` scans all existing DB rows. Non-destructive. Use `--storage-mock` in CI. Reports to `~/.yadgar/secret-leak-scan-<TS>.txt`.

### memorize() anchor parity (v5.10.x)

`memorize(is_protected=True)` now behaves identically to `anchor()`:

| Behaviour | Before | After |
|---|---|---|
| `tier` when unset | `None` (no expiry logic) | `"conditional"` (90d TTL) |
| `_anchor` in tags | Only in sync path after insert | Injected before insert + via `update_memory_fields` |
| `anchor:{reason}` tag | Never | Added when `reason` arg provided |
| `semantic_immortal` without `reason` | Silently accepted | Rejected when `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=True` |

New kwarg: `memorize(…, reason: str = "")`.

### Bug fixes

**surrealdb dependency**: was `[project.optional-dependencies].dev` only. Fresh `pip install yadgar` would `ImportError` on `StorageEngine.__init__`. Promoted to `[project.dependencies]`.

**vacuum `:8080` literal**: `_log_consolidation_row()` used `http://127.0.0.1:8080` as a hard-coded fallback. Now uses `os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8080")`.

### No action required on upgrade

All changes are backwards-compatible. Existing memories, anchors, and configurations are unaffected.

---

## v5.10.1 — `_active_work` soft warning tier + watchdog timer (2026-05-29)

Core 5.10.0 → 5.10.1. Backend unchanged at 5.4.0. No schema changes. Additive only.

### New recommended_actions

Two new soft-tier action types emitted by `project_brief(mode="signals")`:

| Action | Condition | Meaning |
|---|---|---|
| `consider_refresh_active_work` | `ACTIVE_WORK_WARN_HOURS < age ≤ ACTIVE_WORK_STALE_HOURS` | Proactive nudge — update during natural pause |
| `consider_refresh_checkpoint` | `CHECKPOINT_WARN_HOURS < age ≤ CHECKPOINT_STALE_HOURS` | Proactive nudge |

Soft and hard actions are **mutually exclusive** per row. Hard (`refresh_active_work`, `refresh_checkpoint`) unchanged.

Both soft + hard actions now include a `suggested_call` field with a copy-paste-able MCP call.

### New env knobs (3, three-way registered)

| Env var | yaml key | default | role |
|---|---|---|---|
| `YADGAR_ACTIVE_WORK_WARN_HOURS` | `active_work_warn_hours` | `12.0` | Soft warn threshold (hours) |
| `YADGAR_CHECKPOINT_WARN_HOURS` | `checkpoint_warn_hours` | `12.0` | Soft warn threshold (hours) |
| `YADGAR_AUTO_REFRESH_ACTIVE_WORK` | `auto_refresh_active_work` | `false` | Watchdog opt-in (see below) |

### Active-work directory registry

`update_active_work()` now writes a marker file to `~/.yadgar/active-work-tracked/<sha256(dir)[:12]>/directory.txt`. This registry is purely additive (never auto-pruned).

Manual prune old entries:
```sh
find ~/.yadgar/active-work-tracked -type d -mtime +30 -exec rm -rf {} +
```

### Optional watchdog timer (user-managed, NOT enabled by default)

New systemd-user units at `scripts/systemd-user/`:
- `yadgar-active-work-watchdog.timer` — fires every 6h
- `yadgar-active-work-watchdog.service` — scans registry, POSTs `project_brief(mode="signals")` for each dir

**Install (user-managed only):**
```sh
mkdir -p ~/.config/systemd/user/
cp scripts/systemd-user/yadgar-active-work-watchdog.{timer,service} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now yadgar-active-work-watchdog.timer
```

**Auto-refresh opt-in** (stub `_active_work` written when stale):
```sh
# Add to ~/.config/systemd/user/yadgar-active-work-watchdog.service
# under [Service]:
#   Environment=YADGAR_AUTO_REFRESH_ACTIVE_WORK=true
```

Auto-refresh dilutes user-curated `_active_work` content with a stub. Default OFF.

### Token budget for signals mode

Token budget for `signals` mode raised from 100 → 350 (configurable via `SIGNALS_TOKEN_BUDGET_SOFT`). The 100-token budget still applies to the empty-dir/minimal case; 350 covers real-world payloads with 2 soft actions + `suggested_call` fields. Real overhead at 350 per fire is <1% of typical context window. Tune via yaml (`signals_token_budget_soft`) or env (`YADGAR_SIGNALS_TOKEN_BUDGET_SOFT`).

---

## backend v5.4.0 — Recall hot-path caching: CE score cache + embedding vector cache (2026-05-29)

Core unchanged at 5.10.0. Backend 5.3.1 → 5.4.0.

### What changed

Two LRU caches added to the backend container:

| Cache | Hit-path | Key | Default cap |
|---|---|---|---|
| CE score cache | `/rerank?mode=ce` — per-text lookup before ML inference | `query_sha16:text_sha16:ckpt_sha16` | 100K entries (~100MB) |
| Embedding vector cache | `/embed` — per-text lookup before model encode | `text_sha16:mode:ckpt_sha16` | 100K entries (~150MB) |

Cache format: in-memory `OrderedDict` LRU + periodic msgpack snapshot.
Snapshot files: `/data/cache/ce.snap` and `/data/cache/embed.snap`.
Restore on startup: checkpoint-hash mismatch → discard (new model = empty cache).

#### New env knobs (6, three-way registered: Settings + FIELD_META + registry)

| Env var | yaml key | default | role |
|---|---|---|---|
| `YADGAR_CE_CACHE_ENABLED` | `ce_cache_enabled` | `true` | CE cache kill switch |
| `YADGAR_EMBED_CACHE_ENABLED` | `embed_cache_enabled` | `true` | Embed cache kill switch |
| `YADGAR_CE_CACHE_MAX_ENTRIES` | `ce_cache_max_entries` | `100000` | CE cache entry cap (0 = disabled) |
| `YADGAR_EMBED_CACHE_MAX_ENTRIES` | `embed_cache_max_entries` | `100000` | Embed cache entry cap |
| `YADGAR_CACHE_SNAPSHOT_INTERVAL_SEC` | `cache_snapshot_interval_sec` | `600` | Snapshot cadence (seconds) |
| `YADGAR_CACHE_SNAPSHOT_DIR` | `cache_snapshot_dir` | `/data/cache` | Snapshot directory |

#### New Prometheus metrics (10 series)

```
yadgar_embed_ce_cache_hits_total         counter
yadgar_embed_ce_cache_misses_total       counter
yadgar_embed_ce_cache_evictions_total    counter
yadgar_embed_ce_cache_size_entries       gauge
yadgar_embed_ce_cache_size_bytes         gauge
yadgar_embed_embed_cache_hits_total      counter
yadgar_embed_embed_cache_misses_total    counter
yadgar_embed_embed_cache_evictions_total counter
yadgar_embed_embed_cache_size_entries    gauge
yadgar_embed_embed_cache_size_bytes      gauge
yadgar_embed_cache_snapshot_age_seconds{cache} gauge
```

### Files changed

- `yadgar/cache.py` — new `LRUCache` class + msgpack snapshot (NEW FILE)
- `yadgar/embed_service.py` — `_ce_cache`, `_embed_cache` module-level instances; `_score_ce_with_cache()` partial-hit helper; embed cache in `_encode_all()`; lifespan restore + snapshot task
- `yadgar/embed_service_metrics.py` — 10 new metric declarations + `cache_snapshot_age_seconds{cache}` gauge
- `yadgar/config.py` — 6 new Settings fields (`CE_CACHE_ENABLED` etc.)
- `yadgar/config_yaml.py` — 6 new FIELD_META entries (`backend_hot_path_cache` section)
- `yadgar/config_registry.py` — 6 new `ConfigEntry` entries
- `pyproject.toml` — `msgpack>=1.0` dependency added
- `server.json` — `backend_version` 5.3.1 → 5.4.0
- `docker-compose.yml` — backend image tag 5.3.1 → 5.4.0

### Deploy steps

1. Rebuild backend image: `docker build -t docker.io/openfantasy/yadgar-backend:5.4.0 -f Dockerfile.backend .`
2. Mount writable directory for cache snapshots. The backend container mounts `/data:ro` by default — snapshot writes to `/data/cache` are silently skipped unless a writable volume is added. Options:
   ```yaml
   # Option A: named volume (recommended for persistence across restarts)
   volumes:
     - yadgar-cache-data:/data/cache
   # Option B: tmpfs (in-memory only — cache lost on restart, no disk benefit)
   tmpfs:
     - /data/cache
   ```
   Or override to a writable path: `YADGAR_CACHE_SNAPSHOT_DIR=/tmp/cache`
3. Bump `yadger_backend_version` in `~/git/nix/modules/home/yadgar.nix` to 5.4.0.
4. Restart backend container.

### Rollback

```bash
# Disable both caches (pre-v5.4.0 behaviour, zero overhead):
YADGAR_CE_CACHE_ENABLED=false
YADGAR_EMBED_CACHE_ENABLED=false
# OR rollback image to 5.3.1:
docker run ... openfantasy/yadgar-backend:5.3.1
```

### Verification

After backend restart with traffic:
```bash
# Check hit + miss counters both > 0 within 60s of first recall
curl -s http://127.0.0.1:8001/metrics | grep yadgar_embed_ce_cache
# → yadgar_embed_ce_cache_hits_total 0 (initially)
# → yadgar_embed_ce_cache_misses_total N (rising)
# After second recall with same query:
# → yadgar_embed_ce_cache_hits_total > 0

# After CACHE_SNAPSHOT_INTERVAL_SEC elapses:
ls -la /data/cache/  # ce.snap + embed.snap present
```

### Expected impact

- CE cache: 30-70% hit-rate at steady state (same session re-querying related contexts). Each hit saves ~400ms CE inference on CPU.
- Embed cache: 50-80% hit-rate (same texts recur frequently across recall calls). Each hit saves 20-50ms encode time.
- Baseline target: ≥30% CE hit-rate after 24h soak. Tune `CE_CACHE_MAX_ENTRIES` down if RSS > 250MB.

### Open questions (post-deploy tuning)

- After 24h soak: measure actual CE + embed hit rates. Adjust cap if hit-rate cliffs below 50K entries.
- Snapshot latency during heavy traffic: add Tempo span around `save_snapshot` if `/rerank` p99 shows anomaly at snapshot interval.
- Eviction rate > 100/min sustained → cap too low; increase `CE_CACHE_MAX_ENTRIES`.

---

## v5.10.0 — Test Harness Hardening: orphan reap + port determinism + session isolation (2026-05-29)

Core 5.9.0 → 5.10.0. Backend unchanged.

### What changed

#### 1. `pytest-timeout` default now 300s (was 120s via addopts)

The `--timeout=120` flag was removed from `addopts`; a `timeout = 300` key is
set in `[tool.pytest.ini_options]` instead. Per-test overrides work via:

```python
@pytest.mark.timeout(60)
def test_slow_thing(): ...
```

#### 2. SurrealDB subprocess spawn centralized in `yadgar/tests/_surreal_helpers.py`

All `surreal start` spawns go through `spawn_surreal(port, data_dir)`, which
registers PIDs in a module-level list. An `atexit` handler calls
`kill_all_spawned_surreal()` on process exit — including SIGINT and
pytest-timeout unwind. `pytest_sessionfinish` hook in `conftest.py` also calls
it as a last-resort cleanup pass.

No action required. Existing tests use the `surreal_server` session fixture
unchanged.

#### 3. Deterministic xdist port allocation

Port formula: `YADGAR_TEST_PORT_BASE + worker_index * 100 + n`

Default base: 12000. Env knob is **TEST-ONLY** — not in production yadgar config.

Multi-session usage (2+ concurrent agent sessions):

```bash
YADGAR_TEST_PORT_BASE=13000 uv run pytest yadgar/tests/
```

Up to 10 retries with linear 100ms backoff on EADDRINUSE before raising.

#### 4. Multi-agent tmp dir isolation

```bash
YADGAR_TEST_NAMESPACE=agent-42 uv run pytest yadgar/tests/
```

Redirects `TMPDIR` to `/tmp/pytest-agent-42/` so concurrent sessions don't
collide on `/tmp/pytest-of-max/`.

Default (no env var): unchanged behaviour, `/tmp/pytest-of-max/` as before.

#### 5. Optional watchdog systemd-user timer (user-managed, not auto-installed)

Unit files are in `scripts/systemd-user/`. Install once:

```bash
mkdir -p ~/.config/systemd/user
cp scripts/systemd-user/yadgar-test-orphan-cleanup.{service,timer} ~/.config/systemd/user/
systemctl --user enable --now yadgar-test-orphan-cleanup.timer
```

Fires every 5 minutes. Kills any `surreal start` process whose args contain
`pytest-of-max` — does NOT match production `~/.yadgar/surreal_db/` paths.

Adjust `pytest-of-max` in the `.service` file if your username differs.

To disable: `systemctl --user disable --now yadgar-test-orphan-cleanup.timer`

#### 6. Perf test re-enablement (deferred — separate PR)

`test_merge_duplicates_under_5s_at_500_memories_with_embeddings` remains as-is.
Per-test `@pytest.mark.timeout(120)` mechanism is now available for re-enablement
in a follow-up PR.

---

## v5.9.0 — Anchor Audit: audit_anchors() tool + consolidate_now anchor pass (2026-05-28)

Core 5.8.0 → 5.9.0. Backend unchanged at 5.3.1.

### What changed

#### 1. New MCP tool: `audit_anchors(directory, dry_run=True, cosine_threshold=None, include_global=False)`

```python
result = audit_anchors(
    directory="/home/max/git/yadgar",
    dry_run=True,                # default: report-only, no mutations
    cosine_threshold=None,        # default: ANCHOR_REDUNDANCY_COSINE (0.92)
    include_global=False,         # default: skip directory_context="global"
)
# Returns: {
#   "scanned": int,
#   "actions": [
#     {"action": "forget_expired", "id": Y, "expired_at": "...", "rationale": "..."},
#     {"action": "merge", "ids": [a, b], "similarity": 0.94, "rationale": "..."},
#     {"action": "promote", "id": X, "draft": {...}, "next_step": "..."},
#   ],
#   "dry_run": bool,
#   "applied": [...],  # populated when dry_run=False
# }
```

**Behavior:**
- `dry_run=True` (default): returns recommendations, no mutations.
- `dry_run=False`: applies SAFE mutations only.
  - `forget_expired`: `valid_until < now() AND migration_grace=False`. Safe to auto-apply.
  - `merge`: keeps survivor with higher `last_accessed + access_count` rank; forgets lower.
  - `promote`: NEVER auto-applied — always returns draft dict. Caller decides + calls `wiki_add` + `forget`.
- NEVER auto-mutated: `tier="semantic_immortal"` rows, `is_protected=True` legacy rows.
- All mutations logged to `action_log` with `source=audit_anchors` tag + before/after row snapshots.
- Idempotent: second call on same state returns empty `applied` list.

#### 2. Extended `consolidate_now()` with anchor pass

Runs as final step (after write-gate replay → embedding refresh → heat decay → SR cogmap). Per-directory dry-run audit. Writes `_audit_anchors` sentinel memory (latest-wins single row per directory; matches `_active_work` pattern).

Surfaced in next `project_brief(mode="signals")` as audit history reference.

Gate: `ANCHOR_AUDIT_CONSOLIDATION_ENABLED` (default `true`). Set false to disable.

#### 3. Promote-to-wiki draft generator

When `audit_anchors` flags a `promote` candidate, returns:

```python
{
    "action": "promote",
    "id": 257,
    "draft": {
        "suggested_slug": "yadgar-workflow-rule-build-and-push",
        "suggested_title": "Yadgar Workflow Rule — Build and Push",
        "suggested_category": "convention",
        "suggested_tags": ["yadgar", "workflow", "build", "deploy", "_anchor"],
        "body": "<verbatim anchor content>",
        "rationale": "word_count=623 AND headers=3 AND tags ∩ {workflow}",
    },
    "next_step": "Call wiki_add(title, content, tags, category) with these values, then forget(257).",
}
```

Caller copy-pastes the `wiki_add` call. NO auto-`wiki_add` (would reproduce hygiene debt one layer down — dirty slugs, inconsistent tags, no approval workflow).

#### 4. `recommended_actions` enhancement

v5.8 emitted `audit_anchors` action with `action` + `reason`. v5.9 adds `suggested_call`:

```python
{
    "action": "audit_anchors",
    "reason": "count=70 > threshold=15",
    "suggested_call": "audit_anchors(directory='/home/max/git/yadgar', dry_run=True)",
}
```

Caller copy-pastes literal string. Same pattern as v5.7.12 `restore` hint.

#### 5. New config knobs (3 total, three-way registered per I25)

| Knob | Default | Type | Purpose |
|---|---|---|---|
| `ANCHOR_AUDIT_CONSOLIDATION_ENABLED` | true | bool | Toggle anchor pass inside `consolidate_now()` |
| `ANCHOR_AUDIT_MAX_ACTIONS_PER_RUN` | 20 | int | Hard cap on actions returned per audit (token budget) |
| `ANCHOR_AUDIT_HISTORY_RETENTION_DAYS` | 30 | int | How long `_audit_anchors` sentinel snapshots retained |

### Deploy

```bash
podman build --arch amd64 -t docker.io/openfantasy/yadgar:5.9.0 -f Dockerfile .

# Bump nix (user-managed)
# Edit ~/git/nix/modules/home/yadgar.nix:
#   yadger_core_version = "5.9.0"
cd ~/git/nix && nix-update
```

### Rollback

Pure code addition — no schema change, no breaking API. Rollback to 5.8.0 simply loses the `audit_anchors()` tool surface; sentinel memories remain harmless. Safe.

### What does NOT ship in v5.9.0 (deferred to v5.11.0)

- Cross-project anchor dedup (`scope=both` aware) — see `docs/PLAN_V5_11_ANCHOR_CROSS_PROJECT.md`.
- Optional Jira MCP integration.
- `is_protected` flag repurpose as verified-by-3-clean-audits.

v5.10.0 is the **test harness hardening** train (pytest-timeout + SurrealDB fixture atexit + xdist port determinism + watchdog systemd timer), ships BEFORE v5.11.

---

## v5.8.0 — Anchor Hygiene Foundation: tier + valid_until + signals (2026-05-28)

Core 5.7.12 → 5.8.0. Backend unchanged at 5.3.1 (SurrealDB is schemaless; all new fields added via `DEFINE FIELD IF NOT EXISTS` at yadgar-core layer).

### What changed

#### 1. New fields on `memorize()` and `anchor()`

```python
memorize(content, context, tags, *, is_protected=False,
         tier=None,                # "semantic_immortal" | "conditional" | "ephemeral"
         valid_until=None,         # ISO-8601 UTC datetime, exclusive expiry
         ttl_days=None,            # shorthand: now + ttl_days
         provenance_agent=None)

anchor(content, context, reason="", *,
       tier="conditional",         # default
       valid_until=None,
       ttl_days=None)
```

- `tier="semantic_immortal"`: truly cross-session forever (account IDs, hard rules). Requires non-empty `reason` (gated by `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=true` by default).
- `tier="conditional"` (default): currently true but may stale. `valid_until` defaults to `now() + ANCHOR_CONDITIONAL_TTL_DAYS` (90d).
- `tier="ephemeral"`: in-flight work (ticket state, dry-run plans). `valid_until` defaults to `now() + ANCHOR_EPHEMERAL_TTL_DAYS` (14d).
- `valid_until < now()` → row excluded from `restore()`, hot ranking, `project_brief(restore)` top_anchors.

#### 2. New `signals` mode fields

| Field | Type | Computation |
|---|---|---|
| `anchor_count_project` | int | Project-scope anchors not expired. |
| `anchor_redundancy_candidates` | `list[[id_a, id_b, sim]]` | **Compact tuple encoding.** Same `directory_context`, cosine ≥ `ANCHOR_REDUNDANCY_COSINE`. Capped at 3. |
| `anchor_promote_candidates` | `list[int]` | IDs of oversized anchors (word_count > `ANCHOR_PROMOTE_WORDS` AND markdown_header_count ≥ `ANCHOR_PROMOTE_HEADERS` AND tags ∩ rule/pattern/convention/playbook/workflow/recipe). Capped at 3. |
| `_truncated` | bool | True when candidate lists truncated. |

Token budget `signals` mode ≤100 maintained via K=3 hard cap + compact tuple encoding (pathological case ~147 tokens total payload; candidate-only overhead ~37 tokens).

#### 3. New `recommended_actions` action types

| action | trigger |
|---|---|
| `audit_anchors` | `anchor_count_project > ANCHOR_AUDIT_THRESHOLD` (default 15) |
| `merge_redundant_anchors` | `len(anchor_redundancy_candidates) ≥ 1` |
| `promote_anchor_to_wiki` | `len(anchor_promote_candidates) ≥ 1` |
| `forget_expired_anchors` | exists row with `valid_until < now()` AND `migration_grace=False` |

Stop hook from v5.7.12 iterates these unchanged — caller maps action to tool call.

#### 4. New config knobs (7 total, three-way registered per I25)

| Knob | Default | Type |
|---|---|---|
| `ANCHOR_CONDITIONAL_TTL_DAYS` | 90 | int |
| `ANCHOR_EPHEMERAL_TTL_DAYS` | 14 | int |
| `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON` | true | bool |
| `ANCHOR_REDUNDANCY_COSINE` | 0.92 | float |
| `ANCHOR_PROMOTE_WORDS` | 500 | int |
| `ANCHOR_PROMOTE_HEADERS` | 2 | int |
| `ANCHOR_AUDIT_THRESHOLD` | 15 | int |

#### 5. Schema migration `migration_008`

Adds `tier`, `valid_until`, `migration_grace` columns to `memory` table via `DEFINE FIELD IF NOT EXISTS`. Idempotent. Runs on first startup post-v5.8.0 — gated by sentinel `_anchor_migration_v5_8_completed=True` memory.

Backfill: all pre-v5.8 `_anchor`-tagged rows → `tier="conditional", valid_until=now()+90d, migration_grace=True`. The `migration_grace` flag protects them from `forget_expired_anchors` action while the user audits. Clear the flag manually or via v5.9 `audit_anchors()` tool.

### Deploy

```bash
# Build image
podman build --arch amd64 -t docker.io/openfantasy/yadgar:5.8.0 -f Dockerfile .

# Bump nix (user-managed)
# Edit ~/git/nix/modules/home/yadgar.nix:
#   yadger_core_version = "5.8.0"
cd ~/git/nix && nix-update

# First-startup will run migration_008; verify:
journalctl --user -u yadgar.service --since "5 minutes ago" | grep migration_008
```

### Rollback

Schemaless SurrealDB means rolling back to 5.7.12 is safe — the new `tier`/`valid_until`/`migration_grace` columns will simply be ignored by 5.7.12 code paths. No data loss.

### What does NOT ship in v5.8.0 (deferred to v5.9.0)

- `audit_anchors(directory, dry_run=True)` MCP tool — see `docs/PLAN_V5_9_ANCHOR_AUDIT.md`.
- Extend `consolidate_now()` with anchor pass.
- Promote-to-wiki draft generator.

---

## v5.7.12 — project_brief two-audience split + signals/restore modes (2026-05-27)

Core 5.7.11 → 5.7.12. Backend unchanged at 5.3.1.

### What changed

`project_brief()` gained two new modes and three new config knobs.

**New modes:**

| Mode | Audience | Budget | Notes |
|---|---|---|---|
| `signals` | Stop hook | <100 tokens | Binary flags + age numerics + `recommended_actions`. No anchors, no _render. |
| `restore` | Post-/clear, post-/compact | <800 tokens | `top_anchors` (scope-tagged), `hot_memories`, `checkpoint`, `key_wiki_pages`. No signal flags, no _render. |
| `catalog` | (deprecated, back-compat) | ~500 tokens | Current shape unchanged. Marked deprecated in docstring. Remove in v5.8. |
| `full` | Power user / debug | ~1050 tokens | Superset of catalog + inlined content. |

**New `signals` payload fields:**
- `stale_checkpoint_hours` — float|null. Age of latest checkpoint row.
- `active_work_age_hours` — float|null. Age of `_active_work` row.
- `init_memory_age_hours` — float|null. Age of `_project_init` row.
- `recommended_actions` — pre-computed list: `refresh_active_work`, `refresh_checkpoint`, `bootstrap_project` based on configurable thresholds.

**New `restore` payload:**
- `top_anchors` — merged single list (no global/project split). Each entry has `scope: "global"|"project"|"both"`.
- Truncated at `PROJECT_BRIEF_MAX_ANCHORS` (default 12).

**Bug fixes:**
- `hot_memories` now excludes anchored entries in all modes (`'_anchor' NOTINSIDE tags`). Anchors were previously appearing at top of hot_memories list by virtue of heat=1.0.

**New config knobs (yaml-backed, three-way registered):**

| Knob | Default | Description |
|---|---|---|
| `YADGAR_ACTIVE_WORK_STALE_HOURS` | `24.0` | Hours before active_work triggers `refresh_active_work` action |
| `YADGAR_CHECKPOINT_STALE_HOURS` | `24.0` | Hours before checkpoint triggers `refresh_checkpoint` action |
| `YADGAR_PROJECT_BRIEF_MAX_ANCHORS` | `12` | Max anchors in `restore` mode `top_anchors` list |

Yaml keys (lowercase): `active_work_stale_hours`, `checkpoint_stale_hours`, `project_brief_max_anchors`.

### Files changed

- `yadgar/server/tools/project.py` — new helpers `_compute_row_age_hours`, `_get_max_anchors`, `_build_recommended_actions`; mode branching in `project_brief()`; hot_memories anchor filter; restore mode anchor merge with scope field.
- `yadgar/config.py` — 3 new Settings fields.
- `yadgar/config_yaml.py` — 3 new FIELD_META entries, new `project_brief` section.
- `yadgar/config_registry.py` — 3 new `ConfigEntry` registrations.
- `yadgar/tests/test_project_brief_modes.py` — 38 new tests.
- `pyproject.toml` 5.7.11 → 5.7.12; `server.json` core version; `docker-compose.yml` core tag; `uv.lock` yadgar version.

### Deploy steps

1. Rebuild `docker.io/openfantasy/yadgar:5.7.12` image.
2. Update `yadger_core_version` in `~/git/nix/modules/home/yadgar.nix` to `5.7.12`.
3. Optional: extend `~/.yadgar/config.yaml` with the 3 new knobs (defaults are fine for most users):
   ```yaml
   active_work_stale_hours: 24.0
   checkpoint_stale_hours: 24.0
   project_brief_max_anchors: 12
   ```
4. `cd ~/git/nix && nix-update`.

### Verification

```
podman exec yadgar python -c "from yadgar.config import get_settings; s=get_settings(); print('ACTIVE_WORK_STALE_HOURS:', s.ACTIVE_WORK_STALE_HOURS)"
# → ACTIVE_WORK_STALE_HOURS: 24.0

podman exec yadgar python -c "from yadgar import server; server.init_engines(); r=server.project_brief('/repo', mode='signals'); print(r.keys())"
# → dict_keys(['_resolved_directory', '_mode', 'init_memory_present', 'active_work_present', 'stale_wiki_count', 'stale_checkpoint_hours', 'active_work_age_hours', 'init_memory_age_hours', 'recommended_actions'])
```

### Note on stop hook + restore() tool

The stop hook script (`~/.claude/hooks/yadgar-stop-memory-checkpoint.py`) and `restore()` MCP tool are NOT updated in this release — that is the main thread's follow-up task. Until updated:
- Stop hook continues to call `mode="catalog"` (back-compat guaranteed).
- `restore()` continues to call `mode="catalog"` (back-compat guaranteed).

To take advantage of new modes, update call sites to `mode="signals"` / `mode="restore"`.

---

## v5.7.11 + backend v5.3.1 — Yamlify OTLP + DBSIZE knobs, drop dead LOG_LEVEL (2026-05-27)

Core 5.7.10 → 5.7.11. Backend 5.3.0 → 5.3.1.

### What changed

5 env-only knobs migrated through `Settings` to yaml-overridable:

| Knob | Old read site | New |
|---|---|---|
| `OTLP_ENDPOINT` | `tracing.py` `os.environ` | `Settings.OTLP_ENDPOINT` (yaml: `otlp_endpoint`) |
| `OTLP_HEADERS` | same | `Settings.OTLP_HEADERS` (yaml: `otlp_headers`) |
| `OTLP_TIMEOUT_SEC` | same | `Settings.OTLP_TIMEOUT_SEC` (yaml: `otlp_timeout_sec`) |
| `OTLP_INSECURE` | same | `Settings.OTLP_INSECURE` (yaml: `otlp_insecure`) |
| `DBSIZE_CACHE_TTL_SEC` (backend) | `embed_service.py::_dbsize_cache_ttl()` `os.environ` | `Settings.DBSIZE_CACHE_TTL_SEC` (yaml: `dbsize_cache_ttl_sec`) |

LOG_LEVEL investigation confirmed it was DEAD env — only declaration in `config_registry.py`, zero code reads. Registry entry dropped. Nix `-e YADGAR_LOG_LEVEL=INFO` dropped.

`YADGAR_*` env still beats yaml in pydantic-settings precedence.

### Files changed

- `yadgar/config.py` — 5 new Settings fields.
- `yadgar/tracing.py` — `_build_otlp_exporter()` + `_parse_otlp_headers()` refactored to Settings reads.
- `yadgar/embed_service.py` — `_dbsize_cache_ttl()` refactored to Settings read.
- `yadgar/config_yaml.py` — 5 new FIELD_META entries, 2 new SECTION_TITLES (`observability`, `backend_cache`).
- `yadgar/config_registry.py` — `YADGAR_LOG_LEVEL` declaration removed.
- `yadgar/tests/test_otlp_exporter.py` — new `TestYamlOverride` (3 tests) + `reset_otel` fixture now clears Settings cache (was masking pre-existing test bug).
- `yadgar/tests/test_embed_service_v530.py` — new `test_yaml_ttl_override` (A5).
- `pyproject.toml` 5.7.10 → 5.7.11; `server.json` core + backend bumps; `docker-compose.yml` both tags bump; `uv.lock` yadgar version bump.

Nix-side (`~/git/nix/modules/home/yadgar.nix`):
- Core ExecStart drops `-e YADGAR_OTLP_ENDPOINT`, `-e YADGAR_LOG_LEVEL=INFO`. Net: 6 -e flags (was 8 post-v5.7.10).
- Backend ExecStart drops `-e YADGAR_DBSIZE_CACHE_TTL_SEC=600`. Net: 8 -e flags (was 9 post-v5.7.10).
- `yadger_core_version` 5.7.10 → 5.7.11.
- `yadger_backend_version` 5.3.0 → 5.3.1.

Host `~/.yadgar/config.yaml` extended with the 5 new keys at prior nix-default values:
```
otlp_endpoint: http://host.containers.internal:4318/v1/traces
otlp_headers: ""
otlp_timeout_sec: 10
otlp_insecure: true
dbsize_cache_ttl_sec: 600
```

### Deploy steps

1. Images already rebuilt as `docker.io/openfantasy/yadgar:5.7.11` + `docker.io/openfantasy/yadgar-backend:5.3.1`.
2. Bumps in `~/git/nix/modules/home/yadgar.nix` already done.
3. Host `~/.yadgar/config.yaml` already extended.
4. `cd ~/git/nix && nix-update`.

### Verification

```
podman exec yadgar python -c "from yadgar.config import get_settings; s=get_settings(); print('OTLP:', s.OTLP_ENDPOINT)"
# → OTLP: http://host.containers.internal:4318/v1/traces (from yaml)
podman exec yadgar-backend python -c "from yadgar.config import get_settings; print(get_settings().DBSIZE_CACHE_TTL_SEC)"
# → 600
curl -s http://127.0.0.1:3200/api/search 2>&1 | grep -c yadgar-core
# → spans still arriving via OTLP (yaml-sourced endpoint)
```

I25 invariant test still green; grandfather list unchanged (these knobs were never on it — they had no Settings field before, so I25 had nothing to flag).

---

## v5.7.10 — Container yaml loading + I25 invariant + nix -e cleanup (2026-05-27)

Core 5.7.9 → 5.7.10. Backend unchanged (5.3.0).

### What changed

**Step A — Container yaml loading.** `yadgar/config_yaml.py::get_config_path()`
now honors `YADGAR_CONFIG_FILE` env override. Set in nix ExecStart to
`/data/config.yaml`. Container app now reads `/data/config.yaml` (= host
`~/.yadgar/config.yaml` via existing bind mount). Pre-v5.7.10 the hardcoded
`~/.yadgar/config.yaml` resolved to `/root/.yadgar/config.yaml` inside container,
which didn't exist → yaml silently ignored → ALL config came from env.

**Step B core — I25 invariant + three-way-sync TDD test.** New
`tests/test_config_three_way_sync.py` enforces that every `Settings` field
must be EITHER triple-registered (`config.py` + `config_yaml.py` +
`config_registry.py`) OR explicitly allowlisted as intentional env-only in
`yadgar/tests/config_env_only_allowlist.txt`. Wired into pre-commit + CI.

The allowlist landed with TWO tiers:
- **Tier 1** (8 entries): genuine env-only by design — secrets, infra paths,
  container flags.
- **Tier 2** (181 entries): GRANDFATHERED backlog (66 yaml-gap +
  115 registry-gap pre-v5.7.10 knobs). Invariant catches NEW drift; legacy
  cleanup is incremental.

Invariant **I25** added to `docs/ARCHITECTURE_INVARIANTS.md`.

**Step C — Nix `-e` flag cleanup.** Four operational knobs moved from nix
ExecStart `-e` flags into `~/.yadgar/config.yaml`:

| Knob | Old (nix -e) | New (yaml key) |
|---|---|---|
| HOST | `YADGAR_HOST=0.0.0.0` | `host: 0.0.0.0` |
| PORT | `YADGAR_PORT=8765` | `port: 8765` |
| WIKI_SLUG_PREFIX | `YADGAR_WIKI_SLUG_PREFIX=yadgar` | `wiki_slug_prefix: yadgar` |
| CORE_LOG_LEVEL | `YADGAR_CORE_LOG_LEVEL=INFO` | `core_log_level: info` |

Stayed env (intentional):
- Secrets (`DB_USER`, `DB_PASS`, `MCP_AUTH_TOKEN`)
- Infra wiring (`DB_URL`, `EMBED_URL`, `DATA_DIR`)
- Deployment flag (`IN_CONTAINER=1`)
- OTLP endpoint (`YADGAR_OTLP_ENDPOINT` — no Settings field, reads via os.environ in tracing.py)
- `YADGAR_LOG_LEVEL` (no Settings field — defensive keep)

Backend ExecStart gains `-e YADGAR_CONFIG_FILE=/data/config.yaml` for
consistency but no other knobs moved (DBSIZE_CACHE_TTL has no Settings
field; remains env-only).

End state: yadgar core ExecStart has 8 `-e` flags (was 12). Backend
has 9 (was 8 — the YADGAR_CONFIG_FILE addition for uniform yaml loading).

### Files changed

- `yadgar/config_yaml.py` — `get_config_path()` env override.
- `yadgar/config.py` — `YamlConfigSource._load()` delegates to the helper.
- `yadgar/config_registry.py` — `YADGAR_CONFIG_FILE` entry.
- `yadgar/tests/test_config_yaml_container_path.py` — 7 tests for Step A.
- `yadgar/tests/test_config_three_way_sync.py` — I25 enforcement.
- `yadgar/tests/config_env_only_allowlist.txt` — Tier-1 + Tier-2 lists.
- `.pre-commit-config.yaml` + `.forgejo/workflows/ci.yaml` — I25 hook + CI step.
- `docs/ARCHITECTURE_INVARIANTS.md` — I25 section.
- `~/git/nix/modules/home/yadgar.nix` — yadgar core + backend ExecStart
  flag changes. `yadger_core_version` 5.7.9 → 5.7.10.
- `~/.yadgar/config.yaml` — NEW host file with the 4 moved keys + log level.

### Deploy steps

**Required before nix-update:**

```
mkdir -p ~/.yadgar
cat > ~/.yadgar/config.yaml <<'EOF'
host: 0.0.0.0
port: 8765
wiki_slug_prefix: yadgar
core_log_level: info
backend_log_level: info
EOF
chmod 0600 ~/.yadgar/config.yaml
```

Then:

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.10`.
2. `yadger_core_version` bump 5.7.9 → 5.7.10 (already done).
3. `cd ~/git/nix && nix-update`.

### Verification

After restart, container should still listen on 8765 + announce
`yadgar-core/5.7.10` etc. To confirm yaml IS being read:

```
podman exec yadgar python -c "import os; print(os.environ['YADGAR_CONFIG_FILE'])"
# → /data/config.yaml
podman exec yadgar ls -la /data/config.yaml
# → exists, ~556 bytes
```

If something binds wrong (e.g. 127.0.0.1 instead of 0.0.0.0), yaml
loading is broken — rollback by adding `-e YADGAR_HOST=0.0.0.0` back to
nix ExecStart and reporting the issue.

### v5.7.11 follow-up

- Full Step B backfill: incrementally drain the 66+115 Tier-2 grandfather
  list. I25 catches NEW drift; this trims the historical backlog.
- I26 invariant: lint nix ExecStart for `-e <KNOB>=<value>` pairs where
  KNOB has a Settings field + yaml entry (i.e. should be in yaml not env).

---

## Backend v5.3.0 — dbsize cache + restart attribution (2026-05-27)

Backend 5.2.2 → 5.3.0. Core unchanged (5.7.9).

### What changed

**1. `/admin/dbsize` 60s in-memory cache.** Core polls this endpoint every 5s
for the viz daemon. Previously every poll walked `/data/surreal_db` via
`os.walk` — O(n) over the entire DB. Now: cached for `YADGAR_DBSIZE_CACHE_TTL_SEC`
seconds (default 60). 12× reduction in walk frequency. Response gains a
`cache_age_seconds` field; clients can ignore it for back-compat.

**2. Restart cause attribution.** New counter
`yadgar_embed_restart_reason_total{reason}` with labels: `clean` (graceful
SIGTERM, shutdown marker present at startup), `crash` (no marker — process
died unexpectedly), `first_boot` (no marker AND no surreal_db dir).
Shutdown event writes marker at `YADGAR_SHUTDOWN_MARKER_PATH`
(default `/data/.shutdown_clean`). Startup increments counter + removes
marker. Forensic-time savings on mystery restarts.

Bonus: pre-existing I13 HARD nesting violations in `rerank` + `admin_dbsize`
fixed via extracted helpers `_annotate_span` + `_walk_db_sizes`. No
behavior change.

### New env knobs

- `YADGAR_DBSIZE_CACHE_TTL_SEC` (int, default 60). Set to 0 to disable.
- `YADGAR_SHUTDOWN_MARKER_PATH` (string, default `/data/.shutdown_clean`).

Both registered in `config_registry.py`.

### New metrics (backend registry)

- `yadgar_embed_dbsize_cache_hits_total`
- `yadgar_embed_dbsize_cache_misses_total`
- `yadgar_embed_restart_reason_total{reason}`

### Files changed

- `yadgar/embed_service.py` — cache + restart attribution + helpers + lifespan extension.
- `yadgar/embed_service_metrics.py` — 3 new counters.
- `yadgar/config_registry.py` — 2 new env knob entries.
- `server.json` — backend_version 5.2.2 → 5.3.0.
- `docker-compose.yml` — backend image tag 5.2.2 → 5.3.0.
- `yadgar/tests/test_embed_service_v530.py` — 8 TDD tests.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar-backend:5.3.0`.
2. Bump `yadger_backend_version` 5.2.2 → 5.3.0 (already done in `~/git/nix/modules/home/yadgar.nix`).
3. `cd ~/git/nix && nix-update`.

### Verification

After backend restart:

```
curl -sS http://127.0.0.1:8765/metrics | grep yadgar_embed_restart_reason_total
```

Should show `reason="clean"` (post-graceful-restart) or `reason="crash"`
(if backend went down hard).

dbsize cache:
```
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8001/admin/dbsize | jq .cache_age_seconds
```

Second request within 60s should return a `cache_age_seconds` value > 0.

---

## v5.7.9 — Source-aware SessionStart response (2026-05-27)

Core 5.7.8 → 5.7.9. Backend unchanged (5.2.2).

### What changed

`SessionStart` hook handler now branches on the `source` field
(`startup` / `resume` / `clear` / `compact`). Compact auto-restore was
ALREADY shipped via a separate `matcher: "compact"` hook (routes to
`post-compact-rehydrate` → `replay.restore()`). The general handler was
emitting a redundant restore hint on top. v5.7.9 suppresses the hint
on `compact` to eliminate the duplicate and adds source-tailored copy
for the other three sources.

### Behavior matrix

| source | hint emitted? | copy |
|---|---|---|
| `startup` | yes | "Session starting — call restore(directory) to pick up where you left off" |
| `resume` | yes | "Resuming session — checkpoint loaded externally if available" |
| `clear` | yes | "Session cleared — call restore(directory) if needed" |
| `compact` | NO (post-compact-rehydrate handles auto-restore) | — |
| missing | yes (treated as startup) | startup copy |

### Files changed

- `yadgar/scripts/hook_runner.py` — `hook_session_start_context` reads stdin `source` and passes as query param.
- `yadgar/server/http.py` — `hook_session_context` reads `source` query, branches.
- `yadgar/tests/test_v579_smart_sessionstart.py` — 15 new TDD tests.
- `yadgar/tests/test_session_context_endpoint.py` — one assertion updated (exact equality → `in`).

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.9`.
2. Bump `yadger_core_version` 5.7.8 → 5.7.9 (already done).
3. `cd ~/git/nix && nix-update`.

---

## v5.7.8 — /mcp trace_id wiring (2026-05-27)

Core 5.7.7 → 5.7.8. Backend unchanged (5.2.2).

### What changed

Closed Bug 4 residual. `POST /mcp` log lines previously lacked `trace_id`
because FastAPIInstrumentor's span closed when the inner app returned, so
`RequestLoggingMiddleware.finally` saw `None`. Added `MCPTraceSpanMiddleware`
ABOVE `RequestLoggingMiddleware` in both `_cors_wrapped_http_app` and
`_auth_wrapped_sse_app`. The outer span outlives the inner-app call, so the
finally block reads a valid trace_id.

### Files changed

- `yadgar/server/_app.py` — new `MCPTraceSpanMiddleware` (~60 LOC); wired into both wrappers.
- `yadgar/tests/test_mcp_trace_middleware.py` — 2 new tests.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.8`.
2. Bump `yadger_core_version` 5.7.7 → 5.7.8 (already done).
3. `cd ~/git/nix && nix-update`.

### Verification

After restart: `journalctl --user -u yadgar | grep 'POST /mcp' | jq .trace_id`
should return non-null values (was null before this release).

---

## v5.7.7 — VIZ_HEALTH_REFRESH_SEC env knob (2026-05-27)

Core 5.7.6 → 5.7.7. Backend unchanged (5.2.2).

### What changed

The viz daemon's health-scraper interval (hardcoded 5.0s since V1c) is
now configurable via `YADGAR_VIZ_HEALTH_REFRESH_SEC`. Live-reloaded per
iteration — no daemon restart needed when the env value is updated.
Default 5.0 preserves prior behavior.

### Files changed

- `yadgar/config.py` — `VIZ_HEALTH_REFRESH_SEC: float = 5.0` field.
- `yadgar/config_registry.py` — registered for `/admin/config` + gauge.
- `yadgar/viz_daemon_health.py` — `run_health_scraper` reads `get_settings().VIZ_HEALTH_REFRESH_SEC` per iteration; hardcoded constant + TODO removed.
- `yadgar/tests/test_viz_daemon_health.py` — 2 new tests (default + env override).

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.7`.
2. Bump `yadger_core_version` 5.7.6 → 5.7.7 (already done).
3. `cd ~/git/nix && nix-update`.

---

## v5.7.6 — OTLP/HTTP span exporter to Tempo (2026-05-27)

Core 5.7.5 → 5.7.6. Backend unchanged (5.2.2).

### What changed

Added an OTLP/HTTP span exporter alongside the existing `LogSpanProcessor`.
When `YADGAR_OTLP_ENDPOINT` is set, spans ship directly to Tempo (or any
OTLP receiver). When unset (default), behavior is unchanged — spans
remain in JSON log lines for journal-jq.

New env knobs (all 4 registered in `config_registry.py`):

- `YADGAR_OTLP_ENDPOINT` — empty by default. Example: `http://tempo:4318/v1/traces`.
- `YADGAR_OTLP_HEADERS` — comma-separated `k=v` pairs, default empty.
- `YADGAR_OTLP_TIMEOUT_SEC` — default 10.
- `YADGAR_OTLP_INSECURE` — advisory; actual TLS is determined by URL scheme.

New dep: `opentelemetry-exporter-otlp-proto-http>=1.30,<2`.

### Files changed

- `yadgar/tracing.py` — `_parse_otlp_headers`, `_build_otlp_exporter`, conditional wiring in `setup_tracing()`.
- `yadgar/config_registry.py` — 4 new env-knob entries.
- `pyproject.toml` + `uv.lock` — new OTLP exporter dep + 5 transitive packages.
- `yadgar/tests/test_otlp_exporter.py` — 14 new tests.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.6`.
2. Bump `yadger_core_version` 5.7.5 → 5.7.6 (already done).
3. `cd ~/git/nix && nix-update`.
4. To activate Tempo ingestion (separate nix work, not in this release):
   set `YADGAR_OTLP_ENDPOINT=http://tempo:4318/v1/traces` (or wherever
   your Tempo receiver listens) in the yadgar systemd unit's
   `Environment=` block, then restart yadgar.

### Verification

After restart with `YADGAR_OTLP_ENDPOINT` set: spans appear in the
Tempo "Search" Grafana panel within seconds. With endpoint unset:
no behavior change vs v5.7.5.

---

## v5.7.5 — I24 @trace_span AST lint (2026-05-27)

Core 5.7.4 → 5.7.5. Backend unchanged (5.2.2).

### What changed

Added `scripts/check_trace_spans.py` (stdlib-only AST scanner), pre-commit
hook + Forgejo CI step, and new invariant **I24** ("declared public HTTP
handlers MUST carry `@trace_span`"). Mirrors I23 (PR-L)'s metric-writer
pattern. Scoped narrowly to `yadgar/server/http.py` top-level async
functions — storage / retrieval / consolidation subpackages have too many
public helpers without spans for a one-shot enforcement.

### Files changed

- `scripts/check_trace_spans.py` — new (208 LOC).
- `yadgar/tests/test_check_trace_spans.py` — new (7 tests).
- `yadgar/server/http.py` — 13 `@trace_span` decorators added on previously
  un-spanned public handlers: `hook.metrics`, `hook.pre_compact`,
  `hook.post_compact`, `hook.session_context`, `api.stats`,
  `api.graph_stats`, `api.graph_neighborhood`, `api.system`,
  `api.heat_histogram`, `api.consolidation_log`, `api.graph_events`,
  `api.wiki_read`, `api.graph_view`.
- `.pre-commit-config.yaml` — new `check-trace-spans` hook.
- `.forgejo/workflows/ci.yaml` — new I24 step after I23.
- `docs/ARCHITECTURE_INVARIANTS.md` — I24 entry + decision-log note.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.5`.
2. Bump `yadger_core_version` 5.7.4 → 5.7.5 (already done).
3. `cd ~/git/nix && nix-update`

### Verification

After restart, the 13 new `span="api.*"` / `span="hook.*"` series should
appear in Tempo / journal-jq trace lookup. `python scripts/check_trace_spans.py`
exits 0 on master.

---

## v5.7.4 — Hook observability extension (2026-05-27)

Core 5.7.3 → 5.7.4. Backend unchanged (5.2.2).

### What changed

Added `@trace_span` + duration histogram + failure counter wrapper on the
two HTTP hook handlers PR-K missed: `/hooks/auto-capture` and
`/hooks/prompt-recall`. Post-v5.6.7 analytics flagged these two as ~93%
of hook traffic with zero coverage — they're the busiest handlers in the
codebase. Pattern matches PR-K's `_hook_observe` + `_hook_observe_response`
envelope: only `>= 500` counts as failure (400 bad-JSON and 429
rate-limited are not failures); 503 storage-uninit IS.

### Files changed

- `yadgar/server/http.py` — `hook_auto_capture` + `hook_prompt_recall` get
  `@trace_span`, `_t0`/`_caught_exc`/`finally` envelope, early-return
  `_hook_observe_response` calls.
- `yadgar/tests/test_hook_handler_spans.py` — 4 new tests.
- `.complexity-baseline.json` — `http.py` LOC baseline updated (1245→1269).

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.4`.
2. Bump `yadger_core_version` 5.7.3 → 5.7.4 (already done).
3. `cd ~/git/nix && nix-update`

### Verification

After restart, `yadgar_hook_execution_duration_ms_count{hook="auto_capture"}`
and `{hook="prompt_recall"}` should start incrementing on every hook
invocation. Both labels were absent from `/metrics` before this change.

---

## v5.7.3 — DB metric dedup (2026-05-27)

Core 5.7.2 → 5.7.3. Backend unchanged (5.2.2).

### What changed

Dropped duplicate metric `yadgar_db_query_duration_seconds`. Same writer
site, same semantics as the canonical `yadgar_surrealdb_query_duration_ms{op}`
(labeled by operation). Single writer in `storage/client.py::_observe_query_metrics`
emitted both — now emits only the labeled `_ms` variant.

### Files changed

- `yadgar/metrics.py` — declaration removed.
- `yadgar/storage/client.py` — `.observe(elapsed_s)` call + import removed.
- `yadgar/tests/test_storage_db_metrics.py` — `TestDbQueryDurationSeconds` deleted.
- `.complexity-baseline.json` — line-number keys shifted.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.3`.
2. Bump `yadger_core_version` 5.7.2 → 5.7.3 (already done).
3. `cd ~/git/nix && nix-update`

### Dashboard impact

None — no Grafana panels referenced the dropped metric.

---

## v5.7.2 — CROSS_ENCODER_TOP_K cut 20 → 10 (2026-05-27)

Core 5.7.1 → 5.7.2. Backend unchanged (5.2.2).

### What changed

`CROSS_ENCODER_TOP_K` default cut from 20 to 10. Cross-encoder rerank
dominates explicit-recall latency (post-v5.6.7 analytics: 19s p50 vs
<250ms for BM25+HNSW+embed combined). CE call cost scales roughly
linearly with candidate count, so halving TOP_K should roughly halve
rerank stage time.

Override at runtime via `YADGAR_CROSS_ENCODER_TOP_K=20` (env var) if
the wider candidate pool turns out to be necessary for recall quality
on your corpus.

### Files changed

- `yadgar/config.py` — `CROSS_ENCODER_TOP_K: int = 10` (was 20).

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.2`.
2. Bump `yadger_core_version` 5.7.1 → 5.7.2 in `~/git/nix/modules/home/yadgar.nix` (already done).
3. `cd ~/git/nix && nix-update`

No pipx re-install needed (no new console-script entries).

### Verification

Compare `yadgar_recall_duration_ms` p50 / p95 before vs after. Expect
roughly 50% drop on the rerank stage; smaller drop on total recall
(BM25 + HNSW + embed unchanged).

---

## v5.7.1 — Consolidation systemctl container fix (2026-05-27)

Core 5.7.0 → 5.7.1. Backend unchanged (5.2.2).

### What changed

Removed the broken `systemctl --user is-active yadgar-vacuum.service` pre-check
inside `ConsolidationScheduler._maybe_auto_vacuum`. In container deploys
`systemctl` does not exist on `PATH`; `subprocess.check_output` raised
`FileNotFoundError`, the except clause returned early, and the threshold
backstop never fired. With v5.7.0 PR-4 the underlying `_fire_vacuum_service()`
became an atomic trigger-file write — the pre-check guard against double-start
is now both unnecessary and broken.

### Files changed

- `yadgar/consolidation/__init__.py` — removed `import subprocess as _subprocess`
  + the 16-line pre-check block in `_maybe_auto_vacuum`.
- `yadgar/tests/test_vacuum_auto_trigger.py` — dropped 2 tests asserting the
  broken pre-check behavior; stripped 3 dead `_subprocess` patches from
  existing tests; added `test_auto_trigger_fires_when_systemctl_missing_filenotfound`.

### Deploy steps

1. Image already rebuilt as `docker.io/openfantasy/yadgar:5.7.1`.
2. Bump `yadger_core_version` 5.7.0 → 5.7.1 in `~/git/nix/modules/home/yadgar.nix` (already done).
3. `cd ~/git/nix && nix-update`
4. The pipx re-install from v5.7.0 (item 4 of that section) still applies if
   you haven't run it yet — `yadgar-nightly-cycle` entry point still needs
   first-time registration in `~/.local/bin/`.

---

## v5.7.0 — Nightly Cycle Redesign (2026-05-26)

Core 5.6.7 → 5.7.0. Backend unchanged (still 5.2.2).

### What changed

Replaces the daemon's removed 30-minute consolidation trigger with a single nightly
heavy cycle at 19:00 UTC. The cycle runs `backup → consolidation → vacuum → backup`
once per day via a host-side systemd timer that invokes the new
`yadgar-nightly-cycle` console script.

### PRs in this train (in order)

- **PR-0** Remove daemon 30-min consolidation trigger (`bac9540`).
- **PR-2** Vacuum exit-code warn-only on post-restart `check_invariants` 404 (`3b84af4`).
- **PR-3** 30s API readiness wait before `check_invariants` (`d41b63d`).
- **PR-4** Trigger-file pattern for MCP `vacuum_now()` (`e0e9ee0`).
- **PR-5** Documented `VACUUM_AUTO_THRESHOLD_BYTES` as emergency backstop (`bc653de`).
- **PR-6** `create_snapshot` + `prune_snapshots` helpers in `yadgar/backup.py` (`e6857b2`).
- **PR-1a** `yadgar/scripts/nightly_cycle.py` orchestrator (`4fbca8d` + `a7344cb`).
- **PR-7** Backup snapshot round-trip integrity tests (`b9c0026`).
- **PR-1b** Nix systemd timer/service + path-watch unit (in `~/git/nix/modules/home/yadgar.nix`).

### Deploy steps

1. **Rebuild yadgar core image** at the 5.7.0 tag (amd64-only per workflow rule):

   ```
   docker build -t docker.io/openfantasy/yadgar:5.7.0 .
   ```

2. **Bump `yadger_core_version`** in `~/git/nix/modules/home/yadgar.nix` from `5.6.7`
   to `5.7.0`. Backend version (`yadger_backend_version`) stays at `5.2.2`.

3. **Apply nix changes** (`~/git/nix` already contains the new systemd units for
   nightly cycle + vacuum trigger path-watch via PR-1b):

   ```
   cd ~/git/nix && nix-update
   ```

   This activates:
   - `systemd.user.services.yadgar-nightly-cycle` — runs the nightly cycle script.
   - `systemd.user.timers.yadgar-nightly-cycle` — `OnCalendar=*-*-* 19:00:00 UTC`,
     `Persistent=true`.
   - `systemd.user.paths.yadgar-vacuum-trigger` — watches
     `~/.yadgar/triggers/vacuum_requested` (host side of the container's
     `/data/triggers/vacuum_requested`).
   - `systemd.user.services.yadgar-vacuum-trigger` — removes the trigger file
     and starts `yadgar-vacuum.service`.
   - `home.activation.yadgarTriggerDir` — ensures the triggers dir exists.

4. **Re-run the pipx editable install** so `yadgar-nightly-cycle` console-script
   entry registers in `~/.local/bin/`:

   ```
   ~/.local/pipx/venvs/yadgar/bin/python -m pip install -e ~/git/yadgar
   # or: rm ~/.local/pipx/venvs/yadgar/.editable-installed && nix-update
   ```

   Confirm: `which yadgar-nightly-cycle` resolves to `~/.local/bin/yadgar-nightly-cycle`.

### Verification

After deploy:

- `systemctl --user list-timers yadgar-nightly-cycle` — shows the next 19:00 UTC fire.
- `systemctl --user list-timers yadgar-vacuum` — weekly Sunday 04:00 still wired
  (NOT replaced; runs alongside the nightly as the emergency-only legacy backstop).
- `systemctl --user status yadgar-vacuum-trigger.path` — `Active: active (waiting)`.
- Trigger flow smoke test:
  ```
  touch ~/.yadgar/triggers/vacuum_requested
  systemctl --user status yadgar-vacuum-trigger.service
  # should show oneshot ran; trigger file should be gone; yadgar-vacuum.service running
  ```
- Daemon idle eviction soak (from v5.6.7): `yadgar_embed_model_loaded{ce,nli,pair}`
  should stay at 1 once first request loads them (24h soak ends ~2026-05-27 11:41).

### Behavior change

- Consolidation no longer auto-fires every 30 minutes from the daemon. It runs
  ONLY when:
  1. The nightly cycle (19:00 UTC) fires.
  2. MCP `consolidate_now()` is called explicitly.
- `VACUUM_AUTO_THRESHOLD_BYTES` (default 2 GiB) remains as an emergency backstop
  invoked from `ConsolidationScheduler._maybe_auto_vacuum()`. Documented as
  emergency-only in `README.md` and `yadgar/config.py`.

### Known gaps / followups

- `yadgar/consolidation/__init__.py:218-231` still has a `systemctl is-active`
  pre-check that returns early on `FileNotFoundError` — auto-trigger path
  remains broken in containers. Flagged for v5.7.x.
- Consolidation auto-trigger from threshold path uses the trigger-file pattern
  via `_fire_vacuum_service` (PR-4) — works correctly in containers now.
- Pre-existing test failures inherited from v5.6.7 still red on master:
  `test_branch_auto_capture::test_checkpoint_passes_branch_to_replay`,
  `test_check_invariants`, `test_ml_client`, `test_session_context_endpoint`,
  `test_structured_logging`, `test_transport`, `test_v546_parity`. Hotfix
  candidates for v5.7.x.
- 18s yadgar-core downtime during the nightly cycle is expected (same as
  the existing weekly vacuum). Active Claude sessions reconnect via `/mcp`.

---

## v5.6.7 PR-M — Optional log-dir relocation (2026-05-25)

Core 5.6.6 → 5.6.7. Backend unchanged.

### Why

PLT stack's Grafana Alloy log shipper runs as a `DynamicUser` and cannot traverse the
user's home directory (`mode 700`). Moving logs to a world-traversable system path (e.g.
`/var/log/yadgar`) lets Alloy read them without privilege escalation or `SupplementaryGroups`
hacks. The change is OS-agnostic. **File logging is now opt-in via `YADGAR_LOG_DIR`**: when the
env var is unset, yadgar continues stdout-only (the effective default for bare-metal installs
and Docker containers before v5.5.1 applied). Container entrypoints default to
`YADGAR_LOG_DIR=/data/logs` explicitly — no change for operators running containers.

### Files changed

- `yadgar/log_config.py` — new `_resolve_log_dir()` helper; `_resolve_log_file_path()`
  derives path from dir knob; `_install_file_handler` requires at least one of
  `YADGAR_LOG_DIR`, `YADGAR_LOG_FILE_PATH`, or `YADGAR_BACKEND_LOG_FILE_PATH` to be set
  (per-file vars still take priority for test-rig compatibility).
- `entrypoint.sh` + `entrypoint-backend.sh` — default `YADGAR_LOG_DIR=/data/logs` for
  containers; log resolved value to stderr at startup; mkdir + chmod 0750 with fallback.
- `docker-compose.yml` — comment block explaining dev (named volume) vs production
  (host bind-mount) log-dir options.
- `yadgar/tests/test_log_dir_env.py` — 6 new TDD tests.

### For NixOS hosts running Alloy

Set `services.yadgar.logDir = "/var/log/yadgar";` in the yadgar NixOS module
(assuming the module exposes this option — flagged for nix-repo follow-up).

**Manual commands (do NOT auto-apply — run as root or with sudo):**

```bash
mkdir -p /var/log/yadgar
chmod 0750 /var/log/yadgar
chown <yadgar-service-user>:users /var/log/yadgar
```

Then set `YADGAR_LOG_DIR=/var/log/yadgar` in the yadgar systemd `EnvironmentFile`
or via the NixOS module's `environment` attribute.

### Alloy config follow-up (separate nix-repo commit)

Update the Alloy pipeline's `__path__` glob from:

```
~/.yadgar/logs/*.log
```

to:

```
/var/log/yadgar/*.log
```

The `{job="yadgar"}` label and all Loki dashboard queries are unchanged.

### Migration of existing logs

Optional. The new path starts fresh on first write. Archive or leave old logs
in `~/.yadgar/logs/` — yadgar will not touch them after the env var is set.

### Backwards compatibility

**Container operators:** no change — entrypoints set `YADGAR_LOG_DIR=/data/logs` by default.

**Bare-metal/non-container operators who want file logging:** explicitly set `YADGAR_LOG_DIR`
(e.g. `YADGAR_LOG_DIR=$HOME/.yadgar/logs` to replicate the old implicit path, or
`YADGAR_LOG_DIR=/var/log/yadgar` for Alloy integration).

When `YADGAR_LOG_DIR` and per-file vars are all unset, yadgar is stdout-only (same as
before v5.5.1 introduced Sink B). No restart-time shims needed; yadgar picks up the env
on next deploy/restart.

---

## v5.6.1 — V1c bug fixes (2026-05-22)

Core 5.6.0 → 5.6.1. Backend unchanged (5.1.2).

### Changes

- `yadgar/viz_daemon_health.py` — Bug 1: backend URL now resolved via `_get_backend_metrics_url()` (`YADGAR_EMBED_URL` → `http://yadgar-backend:8001/metrics`; override via `YADGAR_BACKEND_METRICS_URL`). Bug 2: `parse_core_metrics` uses new `_parse_core_process()` that reads `yadgar_process_rss_bytes` / `yadgar_process_open_fds` / `yadgar_process_cpu_percent` from core's isolated registry.
- `pyproject.toml`, `server.json`, `docker-compose.yml` — version 5.6.0 → 5.6.1.
- No new deps, no schema changes, no env-var changes (existing `YADGAR_EMBED_URL` re-used).

### Deploy (core only)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.6.1 .
podman push docker.io/openfantasy/yadgar:5.6.1
# Bump nix yadgar_core_version=5.6.1 + home-manager switch
systemctl --user restart yadgar
```

### Verify

```bash
curl -sS http://127.0.0.1:8765/api/daemon-health | python3 -m json.tool | head -20
# Expected: core.process.rss_bytes non-null; backend reachable (not unavailable)
```

---

## v5.6.0 — V1c viz daemon sidebar (2026-05-22)

Core 5.5.3 → 5.6.0. Backend unchanged (5.1.2).

### Changes

- `yadgar/viz_daemon_health.py` — new module: background scraper + `/api/daemon-health` endpoint. Scrapes core metrics (local `generate_latest()`) + backend metrics (HTTP to `http://127.0.0.1:8001/metrics`) every 5s. Caches JSON. Always HTTP 200; `backend.unavailable=True` when unreachable.
- `yadgar/server/http.py` — SSE channel extended: emits `daemon_health` event (JSON) every 5s from `_make_event_stream`. New route `/api/daemon-health` → `api_daemon_health_route`.
- `yadgar/static/index.html` — 480px collapsible sidebar (`#dh-panel`) with Core + Backend cards: process/queue/log/CB/rerank/models. Toggle button in topbar. SSE `daemon_health` event handler wired. REST fallback `_dhFetchOnce()` on panel open.
- Version: `5.5.3 → 5.6.0` in `pyproject.toml`, `server.json`, `docker-compose.yml`.
- No new Python or JS dependencies.

### Deploy (core only — backend unchanged)

```bash
# Build new core image
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.6.0 .

# Bump nix yadgar_core_version=5.6.0 + home-manager switch
# (edit nix config to reference 5.6.0 image, then switch)
systemctl --user restart yadgar
```

### Verify

```bash
# 1. Daemon health endpoint
curl -sS http://127.0.0.1:8765/api/daemon-health | python3 -m json.tool | head -30
# Expected: JSON with core.circuit_breakers, core.queue, backend.process keys

# 2. Load viz UI in browser
# http://localhost:42069/ → click "⬡ Daemons" button in topbar
# Panel should open; core stats populate within 5s; backend may show "unreachable" if not running

# 3. SSE stream carries daemon_health
curl -sS -N http://127.0.0.1:8765/api/graph/events | grep daemon_health | head -1
```

---

## v5.5.3 — V1b CB-1 state gauge (2026-05-22)

Core 5.5.2 → 5.5.3. Backend unchanged (5.1.2).

### Changes

- `yadgar_circuit_breaker_state{endpoint}` now updates inline on every CB state transition.
- Removes dead polling function `_collect_circuit_breaker_states()` from `metrics.py` (looked for `_cb_ce`/`_cb_nli`/`_cb_pair` attrs that never existed — gauge was emitting nothing since introduction).
- `_CircuitBreaker` accepts `metrics_module=None` DI kwarg (default: `yadgar.metrics`).
- Label format: `endpoint="/rerank/ce"` (full path, matching CB log field). V1c panel must use this form.

### Deploy (core only)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.5.3 .
# Bump nix yadger_core_version=5.5.3 + home-manager switch
systemctl --user restart yadgar
```

### Verify

```bash
curl -sS http://127.0.0.1:8765/metrics | grep yadgar_circuit_breaker_state
# Expected: 3 lines with endpoint="/rerank/{ce,nli,pair}" 0.0 (or higher on open)
```

---

## v5.5.2 — backend log metric wiring fix (2026-05-22)

### What changed

- **Bug fix:** backend `yadgar_log_file_size_bytes`, `yadgar_log_file_rotations_total`, and `yadgar_log_dropped_total` metrics now update correctly in the backend's own Prometheus registry.
- No new env vars, no public API changes, no schema changes.
- Core version: **5.5.1 → 5.5.2**. Backend version: **5.1.1 → 5.1.2**.

### Build + restart

```bash
# Core (5.5.2)
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.5.2 .
podman push docker.io/openfantasy/yadgar:5.5.2

# Backend (5.1.2)
podman build --arch amd64 -f Dockerfile.backend -t docker.io/openfantasy/yadgar-backend:5.1.2 .
podman push docker.io/openfantasy/yadgar-backend:5.1.2
```

Update nix config — bump versions:
- Core image tag: `5.5.1` → `5.5.2`
- Backend image tag: `5.1.1` → `5.1.2`

```bash
systemctl --user restart yadgar yadgar-backend
```

## v5.5.1 — log rotation + rate limiter (2026-05-22)

### What changed

- **Dual-sink logging:** `configure_logging()` now installs a `RotatingJSONLFileHandler` (Sink B) alongside the existing stdout handler. Both sinks emit the same I14-conformant JSONL.
- **Rotation defaults:** 100 MB max per file × 5 backups = **500 MB cap per daemon**. Core: `/data/logs/yadgar.log`. Backend: `/data/logs/backend.log`. Both paths map through the existing `-v ~/.yadgar:/data` bind mount.
- **Rate limiter:** token-bucket filter at 10 records/sec burst 50 installed by default on all loggers. Drops increment `yadgar_log_dropped_total`. Disable via `YADGAR_LOG_RATE_LIMIT_ENABLED=0`.
- **3 new metrics:** `yadgar_log_file_rotations_total{logger}`, `yadgar_log_file_size_bytes{logger}`, `yadgar_log_dropped_total{logger,level,reason}` — exposed on both core `/metrics` and backend `/metrics`.
- Core version: **5.5.0 → 5.5.1**. Backend version: **5.1.0 → 5.1.1**.

### Pre-deploy operator action (REQUIRED)

```bash
mkdir -p ~/.yadgar/logs
```

Without this, the log dir is missing → file handler skipped → graceful stdout-only fallback (no crash, just no file sink).

### Env var reference

All vars apply to both core and backend unless noted. Backend prefers `YADGAR_BACKEND_LOG_*` over `YADGAR_LOG_*`; falls back to shared var; then to hardcoded default.

| Env var | Backend override | Default | Description |
|---------|-----------------|---------|-------------|
| `YADGAR_LOG_FILE_PATH` | `YADGAR_BACKEND_LOG_FILE_PATH` | `/data/logs/yadgar.log` (core) / `/data/logs/backend.log` (backend) | Active log file path. Set to `""` to disable file logging entirely. |
| `YADGAR_LOG_FILE_MAX_BYTES` | `YADGAR_BACKEND_LOG_FILE_MAX_BYTES` | `100000000` (100 MB) | Rotate when file exceeds this size. |
| `YADGAR_LOG_FILE_BACKUP_COUNT` | `YADGAR_BACKEND_LOG_FILE_BACKUP_COUNT` | `5` | Backup files to keep. Total cap = MAX_BYTES × BACKUP_COUNT. |
| `YADGAR_LOG_RATE_LIMIT_ENABLED` | — | `1` (enabled) | Set `0` or `""` to disable rate limiter. |
| `YADGAR_LOG_RATE_LIMIT_TOKENS_PER_SEC` | — | `10.0` | Token refill rate per (logger, level) bucket. |
| `YADGAR_LOG_RATE_LIMIT_BURST` | — | `50` | Burst capacity (tokens at start). |

### Disk budget

| Scenario | Core | Backend | Total |
|---------|------|---------|-------|
| Default (500 MB cap each) | ≤500 MB | ≤500 MB | **≤1 GB** |
| Reduced (3 × 50 MB each) | ≤150 MB | ≤150 MB | ≤300 MB |

Journald still accumulates separately. Recommended defense-in-depth cap:

```ini
# ~/.config/systemd/user/journald.conf.d/yadgar.conf
[Journal]
SystemMaxUse=2G
MaxRetentionSec=14day
```

### 1. Pre-deploy: create log directory

```bash
mkdir -p ~/.yadgar/logs
```

### 2. Rebuild images (user runs manually)

```bash
# Core 5.5.1
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.5.1 .
podman push docker.io/openfantasy/yadgar:5.5.1

# Backend 5.1.1
podman build --arch amd64 -f Dockerfile.backend -t docker.io/openfantasy/yadgar-backend:5.1.1 .
podman push docker.io/openfantasy/yadgar-backend:5.1.1
```

### 3. Nix bump (operator)

After images are pushed, bump version pins in `~/git/nix/modules/home/yadgar.nix`:
- Core image tag: `5.5.0` → `5.5.1`
- Backend image tag: `5.1.0` → `5.1.1`

No new bind mounts needed — `/data/logs/` is under the existing `-v ~/.yadgar:/data` mount.
Optionally add `systemd.user.tmpfiles.rules = [ "d %h/.yadgar/logs 0750 - - -" ]` to automate `mkdir -p` at activation.

---

## v5.4.8 — middleware request-log visibility fix (2026-05-22)

### What changed

- `configure_logging()` now installs a dedicated always-INFO `StreamHandler` on `yadgar.requests` (propagate=False). Request log lines now emit regardless of root log level.
- Root cause was `CORE_LOG_LEVEL` defaulting to `"warn"` → root handler at WARNING → `yadgar.requests` INFO records silently dropped.
- Secondary finding: `YADGAR_LOG_LEVEL` is NOT a valid Settings env var. The correct var is `YADGAR_CORE_LOG_LEVEL` (maps to `Settings.CORE_LOG_LEVEL`). Neither `yadgar.service` nor `docker-compose.yml` set it. Add it if full INFO visibility on all `yadgar.*` loggers is needed.
- Backend version **unchanged** (5.0.3) — no backend rebuild needed.

### Operator action (recommended)

Add `YADGAR_CORE_LOG_LEVEL=info` to your deployment to enable INFO logging on all yadgar.* loggers (not just yadgar.requests):

**systemd** (`~/.config/systemd/user/yadgar.service`):
```ini
Environment=YADGAR_CORE_LOG_LEVEL=info
```

**docker-compose** (`docker-compose.yml`, under `core.environment`):
```yaml
YADGAR_CORE_LOG_LEVEL: info
```

### 1. Rebuild core image (user runs manually)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.4.8 .
podman push docker.io/openfantasy/yadgar:5.4.8
```

### 2. Bump nix module

In `modules/home/yadgar.nix` (or equivalent):
```nix
yadgar_core_version = "5.4.8";
```

### 3. Restart core

```bash
systemctl --user restart yadgar.service
```

---

## v5.4.7 — I14 ratchet cleanup (2026-05-22)

### What changed

- `RequestLoggingMiddleware` migrated to I14 schema:
  - `duration_ms` → `latency_ms` (**BREAKING RENAME** — update Loki/Grafana queries)
  - `status` → `http_status`
  - Added: `component="http_server"`, `action="request"`, `outcome` ("ok"/"error"/"degraded")
  - Kept: `request_id`, `tool_name`, `trace_id`
- `ContentRedactor` denylist tightened (two-tier):
  - Exact-match only: `content`, `auth`, `token`, `secret`, `bearer`
  - Substring match: `password`, `api_key`, `authorization`, `access_token`, `refresh_token`, `client_secret`, `private_key`
  - `content_type`, `content_length` no longer falsely redacted
- `_outcome_from_status` helper: `2xx/3xx→"ok"`, `"cancelled"→"degraded"`, all else→`"error"`
- Backend version **unchanged** (5.0.3) — no backend rebuild needed.

### Dashboard migration (BREAKING — action required)

If you have Loki/Grafana queries reading `duration_ms` from `yadgar.requests` logs, update to `latency_ms`:

```logql
# Before
{job="yadgar"} | json | __error__="" | duration_ms > 1000

# After
{job="yadgar"} | json | __error__="" | latency_ms > 1000
```

### 1. Rebuild core image (user runs manually)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.4.7 .
podman push docker.io/openfantasy/yadgar:5.4.7
```

### 2. Bump nix module

In `modules/home/yadgar.nix` (or equivalent):
```nix
yadgar_core_version = "5.4.7";
```

### 3. Restart core

```bash
systemctl --user restart yadgar.service
```

---

## v5.4.6 — LOW-risk complexity refactor batch (2026-05-22)

### What changed

- 4 functions decomposed per P12 complexity audit (all LOW-risk):
  - `insert_typed_relationship` — params 9→4 via `RelationshipMeta` dataclass
  - `insert_new_memory` — params 12→4 via `NewMemorySpec` dataclass
  - `create_checkpoint` — params 9→3 via `CheckpointContext` dataclass
  - `cmd_config` — nesting 6→1, cyclo 7→3 via dispatch dict
- `pyproject.toml` per-file-ignores: 2 files fully removed, 1 partial (PLR0913 only)
- `.complexity-baseline.json` regenerated (4667 → 4819 entries)
- Backend version **unchanged** (5.0.3) — no backend rebuild needed.

### 1. Rebuild core image (user runs manually)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.4.6 .
podman push docker.io/openfantasy/yadgar:5.4.6
```

### 2. Bump nix module

In `modules/home/yadgar.nix` (or equivalent):
```nix
yadgar_core_version = "5.4.6";
```

### 3. Restart core

```bash
systemctl --user restart yadgar.service
```

---

## v5.3.9 — Crash hotfix (2026-05-20)

These commands run **manually** during v5.3.9 deploy. Per HARD RULE — No Apply / Import, the repo cannot apply them.

### 1. systemd cascade decouple (CRITICAL — fixes 2026-05-20 OOM cascade)

**Root cause:** `~/.config/systemd/user/yadgar.service` has `BindsTo=yadgar-backend.service` + `Requires=yadgar-backend.service`. When backend OOMKilled at 19:57:53, `BindsTo` forced core to stop. Core didn't auto-restart because the SIGKILL exit (143) bypassed `Restart=on-failure` semantics.

**Fix lives in `~/git/nix/modules/home/yadgar.nix`** (the systemd unit for the user yadgar service):

```nix
# Before
systemd.user.services.yadgar = {
  Unit = {
    After = "yadgar-backend.service";
    BindsTo = "yadgar-backend.service";     # ← remove
    Requires = "yadgar-backend.service";    # ← remove (or weaken to Wants)
  };
};

# After
systemd.user.services.yadgar = {
  Unit = {
    After = "yadgar-backend.service";
    Wants = "yadgar-backend.service";       # loose dependency, ordering only
  };
};
```

Result: backend death no longer forces core stop. Core rides out backend restarts (degraded reads via v5.4 N4 circuit breaker).

**Apply** (user runs manually):

```bash
cd ~/git/nix
git checkout -b fix/yadgar-systemd-decouple master
# edit modules/home/yadgar.nix per the diff above
git add modules/home/yadgar.nix
git commit -m "fix(yadgar): decouple core from backend lifetime — remove BindsTo+Requires (v5.3.9 N0)"
home-manager switch
systemctl --user daemon-reload

# Verify
systemctl --user cat yadgar.service | grep -E "BindsTo|Requires|Wants"
# Expected: Wants=yadgar-backend.service. No BindsTo, no Requires.
```

**Verification test:**

```bash
systemctl --user stop yadgar-backend
sleep 5
systemctl --user is-active yadgar  # should print "active"
systemctl --user start yadgar-backend
```

If `yadgar` shows `inactive`/`failed` after stopping backend → fix didn't apply correctly.

### 2. DLQ cleanup — 16 stale wiki_add entries

16 `wiki_add` entries stuck in DLQ since 2026-05-18 with `schema_version_too_old: got None, require >= 2`. Pre-v5.0 payloads from before schema migration #004 enforced `wiki_schema_version=2`.

**Decision:** explicit drop (entries 3 days old; content likely re-captured by subsequent writes).

```bash
# List
yadgar dlq-list

# Drop matching entries
yadgar dlq-drop --filter 'op_type=wiki_add,last_error~schema_version_too_old'
```

If `yadgar dlq-drop` doesn't exist yet, fall back: list filenames via MCP `dlq_inspect`, remove from `~/.yadgar/dlq/` manually.

### 3. Image versions

- Core: build `docker.io/openfantasy/yadgar:5.3.9` locally (amd64). Do NOT push per WORKFLOW RULE 2026-05-19.
- Backend: `docker.io/openfantasy/yadgar-backend:5.0.2` (unchanged).
- Nix bump: `modules/home/yadgar.nix` → `yadger_core_version = "5.3.9"` (alongside the BindsTo decouple).

### 4. Acid test

```bash
# Smoke
curl -sS -H "Authorization: Bearer $YADGAR_TOKEN" http://127.0.0.1:8765/health
# Expected: 200

# Cascade
systemctl --user stop yadgar-backend
sleep 5
systemctl --user is-active yadgar          # active
# Recall fails fast (≤5s timeout per N1, not 30s)
time curl -sS -H "Authorization: Bearer $YADGAR_TOKEN" \
  -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"recall","arguments":{"query":"test"}}}' \
  | head -c 200
# Expected: ≤10s, error indicating backend unavailable

systemctl --user start yadgar-backend
sleep 10
# Recall succeeds again
```

### 5. Rollback

```bash
cd ~/git/nix
git revert <commit-from-§1>
home-manager switch
systemctl --user restart yadgar yadgar-backend
```

Pin yadgar core image tag to v5.3.7 in nix until rollback resolved.

---

## v4.8 backup retention

Replace the old "keep 7 newest by mtime" policy with three-cap retention
(age + count + size). The nix module edit is staged in `/home/max/git/nix`
but not applied — the user runs `nix-update`.

### Steps

**1. One-shot manual cleanup of the 28 GB stuck pre-rebuild snapshots:**

~~~bash
find ~/.backups/yadgar/db -maxdepth 1 -name 'surreal_db_*' -type d -mtime +1 -size +1G -exec rm -rf {} +
~~~

This removes any snapshot older than 1 day that is larger than 1 GB —
targeting the five 5.5 GB pre-rebuild bloated snapshots from 2026-05-12
while leaving the two post-rebuild snapshots (137 MB, 479 MB) untouched.

**2. Apply the nix module edit to install the new retention logic in
`yadgar-backend.service:ExecStartPre`:**

~~~bash
home-manager switch
~~~

The edit is at `/home/max/git/nix/modules/home/yadgar.nix`. It:
- Adds `options.programs.yadgar.backup.{maxAgeDays,maxCount,maxGiB}` with defaults 7/7/10.
- Replaces the `tail -n +8 | xargs rm -rf` retention with the three-cap
  logic (age → count → size). Values are baked into the unit at `home-manager switch` time.

To override defaults, add to your home configuration before switching:

~~~nix
programs.yadgar.backup = {
  maxAgeDays = 14;   # keep up to 14 days
  maxCount   = 5;    # keep at most 5 snapshots
  maxGiB     = 20;   # allow up to 20 GiB
};
~~~

**3. Verify on next service restart:**

~~~bash
journalctl --user -u yadgar-backend.service -b | grep -i 'snapshot\|cleanup'
~~~

### Helper script

`scripts/cleanup-backups.sh` in this repo is the single source of truth for
the retention logic. It accepts env vars `YADGAR_BACKUP_DIR`,
`YADGAR_BACKUP_MAX_AGE_DAYS`, `YADGAR_BACKUP_MAX_COUNT`, `YADGAR_BACKUP_MAX_GIB`
and a `--dry-run` flag. Callable with a different `YADGAR_BACKUP_DIR` so the
new `cmd_vacuum` reuses it for pre-vacuum snapshot retention.

---

## v4.8 weekly SurrealKV vacuum

A new `yadgar-vacuum.service` + `.timer` runs the rewritten `yadgar vacuum`
on the proven export → swap → reimport flow every Sunday at 04:00 local
time (with a 30-minute random delay).

### Steps

**1. Apply the nix module edit:**

~~~bash
home-manager switch
~~~

This installs `yadgar-vacuum.service` (oneshot) + `yadgar-vacuum.timer`
(weekly). The vacuum binary stops/starts both `yadgar` and `yadgar-backend`
itself, so the timer unit does not declare `Conflicts=` or `PartOf=` on
the daemons.

**2. (Optional) Trigger a one-off vacuum on the current bloated DB:**

~~~bash
systemctl --user start yadgar-vacuum.service
journalctl --user -u yadgar-vacuum.service -f
~~~

Expected log lines: `Phase 1: export...`, `Phase 2: snapshot + drop...`,
`Phase 3: restart + import...`, `Phase 4: finalize...`, then
`Vacuum complete. before=N MB after=M MB saved=N-M (X%)`.

**3. Verify timer schedule:**

~~~bash
systemctl --user list-timers yadgar-vacuum.timer
~~~

**4. Rollback (if reimport fails):**

The bloated dir is retained at `~/.yadgar/surreal_db.bloated-<ts>` and
`yadgar` is NOT restarted. To recover the old state:

~~~bash
systemctl --user stop yadgar yadgar-backend
mv ~/.yadgar/surreal_db.pre-vacuum-<ts> ~/.yadgar/surreal_db
systemctl --user start yadgar-backend yadgar
~~~

**5. Disable the timer entirely:**

~~~bash
systemctl --user disable --now yadgar-vacuum.timer
~~~

---

## v4.8 log-level config (from v4.7.0, repeated for reference)

After deploying v4.8, the `[ml]` extra is now bundled in the core image
so the NLI reranker (`sentence_transformers`) works out of the box. No
action required — the WARN `Reranker disabled: install yadgar[ml] to
enable` should no longer appear in logs.

---

## v4.8 consolidation cooldown

`CONSOLIDATION_COOLDOWN_SECONDS` defaults to `1800` (30 min). Idle-triggered
consolidation cycles fire at most once per cooldown window. The daily 18:30
UTC cycle and `force_consolidate()` ignore the cooldown.

To override, edit `~/.yadgar/config.yaml`:

~~~yaml
CONSOLIDATION_COOLDOWN_SECONDS: 3600   # 1 hour instead of 30 min
# CONSOLIDATION_COOLDOWN_SECONDS: 0    # restore legacy back-to-back behaviour
~~~

Then restart: `systemctl --user restart yadgar`.

---

## v5.0 Stage 1 — Credentials Hardening + MCP Auth

**CRITICAL: Complete ALL steps before deploying the new image.**
REQUIRE_AUTH defaults to `True` in v5.0. Operators MUST provision
YADGAR_MCP_AUTH_TOKEN in /etc/yadgar/secrets.env BEFORE upgrading.
To stage the upgrade (token provisioned later), set `YADGAR_REQUIRE_AUTH=0`
in env for the initial deploy, then unset after token rollout.

### 1. Generate YADGAR_MCP_AUTH_TOKEN

~~~bash
# Option A: Python (no deps)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

# Option B: 1Password CLI
op item create --vault Private --title "yadgar/MCP_AUTH_TOKEN" \
  --category "API Credential" --field-type concealed --generate-password=true
# Read back:
op read op://Private/yadgar/MCP_AUTH_TOKEN
~~~

### 2. Create /etc/yadgar/secrets.env (chmod 600, root-owned)

~~~bash
sudo mkdir -p /etc/yadgar
sudo tee /etc/yadgar/secrets.env > /dev/null <<EOF
SURREAL_USER=<root_username>
SURREAL_PASS=<root_password>
YADGAR_RW_USER=yadgar-rw
YADGAR_RW_PASS=<rw_password>
YADGAR_RO_USER=yadgar-ro
YADGAR_RO_PASS=<ro_password>
YADGAR_DB_USER=yadgar-rw
YADGAR_DB_PASS=<rw_password>
YADGAR_MCP_AUTH_TOKEN=<token_from_step_1>
EOF
sudo chmod 600 /etc/yadgar/secrets.env
sudo chown root:root /etc/yadgar/secrets.env
~~~

### 3. Update systemd units to use EnvironmentFile

Units generated by `scripts/setup.sh` now use `EnvironmentFile=/etc/yadgar/secrets.env`.
If you have existing units that inline `-e SURREAL_PASS=...`:

~~~bash
systemctl --user edit yadgar-backend.service --force
# Under [Service], add:
# EnvironmentFile=/etc/yadgar/secrets.env
# Remove: -e SURREAL_USER=... -e SURREAL_PASS=... from ExecStart
systemctl --user daemon-reload
~~~

### 4. Re-run install_hooks to inject bearer token

~~~bash
export YADGAR_MCP_AUTH_TOKEN="$(grep YADGAR_MCP_AUTH_TOKEN /etc/yadgar/secrets.env | cut -d= -f2-)"
~~~

Then from a Claude session: call `install_hooks()`.

This writes `hook_runner.py` (replaces old python3 -c strings) and injects
`YADGAR_MCP_AUTH_TOKEN` env block into every hook entry in settings.json.

### 5. Restart services

After steps 1–4 (token is in secrets.env, hooks updated):

~~~bash
systemctl --user restart yadgar-backend.service yadgar.service
~~~

Auth is enabled by default (REQUIRE_AUTH=True). No extra flag needed once
the token is provisioned. If you staged with YADGAR_REQUIRE_AUTH=0, remove
that override before restarting.

### 6. Verify

~~~bash
# Health — always unauthenticated
curl -s http://127.0.0.1:8765/health

# API with auth enabled — should get 401 without token
curl -s http://127.0.0.1:8765/api/graph

# API with token — should work
curl -s -H "Authorization: Bearer $YADGAR_MCP_AUTH_TOKEN" http://127.0.0.1:8765/api/graph
~~~

### Rollback

If something breaks after the upgrade:

~~~bash
# Temporarily disable auth while debugging:
echo "YADGAR_REQUIRE_AUTH=0" | sudo tee -a /etc/yadgar/secrets.env
# Then restart:
systemctl --user restart yadgar.service
# Once resolved, remove the override line and restart again.
~~~

---

## v5.0.1 — durable MCP token wiring in nix module

**Context:** v5.0.1 deploy crashlooped 74× — H-7 startup validator demands
`YADGAR_MCP_AUTH_TOKEN` when `REQUIRE_AUTH=1` (default). Nix module did not
wire token at all (op-inject template missing it, ExecStart missing `-e`).

**Also:** Claude Code v2.1.x silently ignores the `headers` field on HTTP MCP
servers loaded via `programs.mcp` (the home-manager plugin path that produces
`~/.config/mcp/mcp.json` entries labelled `plugin:claude-code-home-manager:*`).
It tries OAuth discovery instead, which 404s. The fix is to register Yadgar
directly in `~/.claude.json` under `mcpServers` (where `headers` *is*
honoured) and drop `programs.mcp.servers.yadgar` from the nix module.

**Current state:** ephemeral fix in place — manual `YADGAR_MCP_AUTH_TOKEN=`
line in `~/.config/yadgar/secrets.env` + systemd drop-in at
`~/.config/systemd/user/yadgar.service.d/mcp-token.conf` that re-defines
ExecStart with `-e YADGAR_MCP_AUTH_TOKEN` appended. Server is up on v5.0.1.

**Risk if not fixed:** next `nix-update` regenerates secrets.env via op-inject
template (which lacks token line) → wipes the manual line. Container restart
crashes again.

**1Password item** (already created by user, 2026-05-16):
`Private/yadgar-mcp`, field `password`, length 32.

### Patch `~/git/nix/modules/home/yadgar.nix`

**1. Op-inject template — add token line (file ~line 158, inside `writeText`):**

~~~nix
${pkgs.writeText "yadgar-secrets.env.tpl" ''
  SURREAL_USER=op://Private/yadgar-root/username
  SURREAL_PASS=op://Private/yadgar-root/password
  YADGAR_RW_USER=op://Private/yadgar-rw/username
  YADGAR_RW_PASS=op://Private/yadgar-rw/password
  YADGAR_RO_USER=op://Private/yadgar-ro/username
  YADGAR_RO_PASS=op://Private/yadgar-ro/password
  YADGAR_DB_USER=op://Private/yadgar-rw/username
  YADGAR_DB_PASS=op://Private/yadgar-rw/password
  YADGAR_MCP_AUTH_TOKEN=op://Private/yadgar-mcp/password
''}
~~~

**2. systemd.user.services.yadgar ExecStart — add token passthrough:**

After `-e YADGAR_DB_PASS`, insert `-e YADGAR_MCP_AUTH_TOKEN` (no value — read
from EnvironmentFile).

### Apply

~~~bash
cd ~/git/nix
# edit modules/home/yadgar.nix per above
nix-update
~~~

### Cleanup ephemeral fix (only after `nix-update` succeeds and health is OK)

~~~bash
rm ~/.config/systemd/user/yadgar.service.d/mcp-token.conf
rmdir ~/.config/systemd/user/yadgar.service.d 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user restart yadgar
curl -sf http://127.0.0.1:8765/health   # expect status=ok, version=5.0.1
~~~

### Verify token survives op-inject

~~~bash
grep YADGAR_MCP_AUTH_TOKEN ~/.config/yadgar/secrets.env | cut -d= -f1
# Expect: YADGAR_MCP_AUTH_TOKEN
~~~

### Rollback

If `nix-update` fails or secrets.env loses the token, restore manually:

~~~bash
TOK=$(op item get yadgar-mcp --vault Private --fields password --format json | jq -r '.value')
grep -v '^YADGAR_MCP_AUTH_TOKEN=' ~/.config/yadgar/secrets.env > /tmp/se.new
printf 'YADGAR_MCP_AUTH_TOKEN=%s\n' "$TOK" >> /tmp/se.new
mv /tmp/se.new ~/.config/yadgar/secrets.env
chmod 600 ~/.config/yadgar/secrets.env
systemctl --user restart yadgar
~~~

## v5.1 — yadgar-vacuum.service ExecStart fix (A2)

The systemd unit has been failing exit 127 on every scheduled trigger
since v4.8.3. Root cause: ExecStart passed `vacuum --service-mode=systemd`
as the container CMD; the image entrypoint is not the bare `yadgar`
binary, so crun tried to exec a binary literally named `vacuum` and
failed `executable file not found in $PATH`.

Companion fix to v5.1 A1 (`storage.get_db_size()` bearer-token client,
commit `bc22f0b`). The vacuum subprocess calls `get_db_size()` internally
during phase logging, so it now also requires `YADGAR_MCP_AUTH_TOKEN`
in its container env. The SurrealDB export+reimport phases require
`YADGAR_DB_USER/PASS` (already in `secrets.env`).

Nix module edit is committed at `~/git/nix` (master, commit `7068449`)
but not applied — the user runs `nix-update`.

### Apply

~~~bash
cd ~/git/nix
nix-update
~~~

### Validate

~~~bash
# Trigger manually — should now exit 0 + log before/after byte counts
systemctl --user start yadgar-vacuum.service
journalctl --user -u yadgar-vacuum.service -n 80 --no-pager

# Confirm next scheduled trigger
systemctl --user list-timers yadgar-vacuum.timer --no-pager
~~~

### Expected log markers on success

- `vacuum.py` phase markers: `export → strip → snapshot → swap → import`
- `before_bytes=NNN, after_bytes=MMM` with a measurable reduction
- exit 0, container `--rm` cleaned up

### Rollback

If the vacuum service fails for a new reason post-apply:

~~~bash
# Revert the nix commit
cd ~/git/nix && git revert 7068449 && nix-update

# Or: stop the timer until the underlying issue is resolved
systemctl --user stop yadgar-vacuum.timer
systemctl --user disable yadgar-vacuum.timer
~~~

A1 client-side fix is on branch `v5.1/a1-dbsize-instrumentation` in the
yadgar repo (commit `bc22f0b`). It does NOT need migration — it will
land via the next yadgar image release (`5.1.0`). Until then, the
vacuum service runs against the `5.0.1` image, where `get_db_size()`
inside vacuum still returns 0 in log output (cosmetic — the export+
import phases use `YADGAR_DB_USER/PASS` not the bearer token, so the
core compaction logic works).

---

## v5.4.3 — I14 framework-logger coverage + ruff grandfathering (2026-05-22)

### What changed

- `configure_logging()` now covers ALL framework loggers (uvicorn, mcp, fastmcp, httpx, starlette) via root-logger approach. Core daemon plain-text log lines are gone.
- 31 pre-existing C901/PLR0913 ruff violators grandfathered in `pyproject.toml` per-file-ignores (b27d218 gap). Refactor target: v5.4.4.
- Backend version **unchanged** (5.0.3) — no backend rebuild needed.

### 1. Rebuild core image (user runs manually)

```bash
podman build --arch amd64 -f Dockerfile -t docker.io/openfantasy/yadgar:5.4.3 .
podman push docker.io/openfantasy/yadgar:5.4.3
```

### 2. Bump nix module

In `modules/home/yadgar.nix` (or equivalent):
```nix
yadgar_core_version = "5.4.3";
```

### 3. Restart core

```bash
systemctl --user restart yadgar.service
```

### 4. Verify JSON logging covers framework lines

```bash
journalctl --user -u yadgar -n 50 --output=cat | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try: json.loads(line); print('OK:', line[:80])
    except: print('NOT JSON:', line[:80])
"
```

All lines (including uvicorn.access, mcp, fastmcp) should parse as JSON.

### 5. New env var: YADGAR_LOG_FORMAT=human

For local dev (non-JSON output from ALL loggers):
```bash
YADGAR_LOG_FORMAT=human python -m yadgar --transport streamable-http
```

## v5.4.2 — CB-1 probe fixes + F5-A saturation fix (2026-05-22)

### 1. Image builds (user runs manually)

```bash
# Build core (5.4.2) — CB-1 changes are in core image
cd ~/git/yadgar
podman build -f Dockerfile -t docker.io/openfantasy/yadgar:5.4.2 .

# Build backend (5.0.3) — F5-A semaphore is in backend image
podman build -f Dockerfile.backend -t docker.io/openfantasy/yadgar-backend:5.0.3 .
```

### 2. Nix bump (in ~/git/nix)

Update `modules/home/yadgar.nix`:

```nix
# Core image version
yadgar_core_version = "5.4.2";  # was 5.4.1

# Backend image version
yadgar_backend_version = "5.0.3";  # was 5.0.2
```

Then apply:

```bash
cd ~/git/nix
git checkout -b chore/v5.4.2-image-bump master
# edit modules/home/yadgar.nix per above
git add modules/home/yadgar.nix
git commit -m "chore(yadgar): bump core 5.4.1→5.4.2 + backend 5.0.2→5.0.3"
home-manager switch
systemctl --user restart yadgar yadgar-backend
```

### 3. Optional: F5-C cgroup bump (if saturation persists after F5-A)

If CPU fan spin-up resumes after the F5-A semaphore deploy, consider bumping backend container resources in `modules/home/yadgar.nix`:

```nix
# Before
--cpus 2 --memory 4g

# After (F5-C)
--cpus 4 --memory 6g
```

F5-A (semaphore N=1) should be sufficient because it ensures probes fast-fail instead of piling on. F5-C is the escape hatch if the model's normal inference load (non-probe) still saturates 2 CPUs.

### 4. New env vars (optional overrides)

All have sensible defaults — no config change required for standard deploy.

| Env var | Default | Notes |
|---|---|---|
| `YADGAR_CIRCUIT_BREAKER_PROBE_TIMEOUT_SEC` | 2.0 | Core — probe HTTP read timeout |
| `YADGAR_CIRCUIT_BREAKER_MAX_OPEN_DURATION_SEC` | 600 | Core — backoff ceiling |
| `YADGAR_CIRCUIT_BREAKER_BACKOFF_FACTOR` | 2.0 | Core — cooldown multiplier per failed probe |
| `YADGAR_RERANK_MAX_CONCURRENCY` | 1 | Backend — semaphore slots per mode |
| `YADGAR_RERANK_SEMAPHORE_ACQUIRE_TIMEOUT_SEC` | 2.0 | Backend — acquire wait before 503 |

### 5. Verification

```bash
# Core health
curl -sS -H "Authorization: Bearer $YADGAR_TOKEN" http://127.0.0.1:8765/health

# Backend semaphore: confirm /rerank returns 503 when concurrency exceeded
# (requires manually saturating the slot — normal usage is fine)
# Expected normal behavior: /rerank returns 200 in <5s

# CB-1 backoff: check logs for "circuit breaker ... → OPEN ... backoff"
journalctl --user -u yadgar -n 50 | grep -i "circuit breaker"
```

### 6. I14 — Structured logging (new in v5.4.2)

**Breaking: default log format changed `human` → `json`.**

Log output is now I14-conformant JSON lines by default. If any Loki/Grafana dashboards or `journalctl | grep` scripts depend on the old plain-text format or old field names (`timestamp`, `logger`, `message`), update them.

| Change | Detail |
|---|---|
| Old field `timestamp` | Now `ts` (ISO 8601 with `+00:00` timezone) |
| Old field `message` | Now `event` |
| Old field `logger` | Removed (use `component` extra field instead) |
| New required caller fields | `component`, `action`, `outcome` in `extra={}` |

**New env var:**

| Env var | Default | Notes |
|---|---|---|
| `YADGAR_LOG_FORMAT` | `json` | `json` = I14 structured (Loki/Grafana ingest); `text` = human-readable for local dev |

**Local dev:** set `YADGAR_LOG_FORMAT=text` in your `.env` or shell to restore human-readable logs.

**Observability:** Loki / Grafana can now ingest yadgar logs as structured records. Parse on `ts`, `level`, `component`, `action`, `outcome` labels.

**Redaction:** `ContentRedactor` strips any log field whose name contains (case-insensitive): `content`, `password`, `token`, `secret`, `auth`, `authorization`, `api_key`, `bearer`. Known sharp edge: substring match also redacts `content_type`/`content_length` if passed as extra fields. Full-conformance round (v5.6) will tighten the denylist.

### 7. Release-readiness check — image size ratchet (P9, new in v5.4.2)

**Optional but recommended** — run after each `podman build` to verify the backend image stays within the 2.0 GB cap.

```bash
# After building the backend image:
pre-commit run check-image-size-backend --hook-stage manual

# After building the core image:
pre-commit run check-image-size-core --hook-stage manual
```

**Manual invocation (no pre-commit):**

```bash
# Resolves version from server.json automatically:
python scripts/check_image_size.py --image-type backend
python scripts/check_image_size.py --image-type core

# Or pass a specific image ref directly:
python scripts/check_image_size.py \
  --image docker.io/openfantasy/yadgar-backend:5.0.3

# Override cap if needed:
python scripts/check_image_size.py \
  --image docker.io/openfantasy/yadgar-backend:5.0.3 \
  --max-size-gb 2.0 \
  --warn-layer-mb 500
```

Exit 0 = within cap (warnings for individual layers >500 MB). Exit 1 = over budget.

**Caps:** backend ≤2.0 GB, core ≤0.8 GB (auto-detected from image name; override with `--max-size-gb`).

**Context:** F0 achieved 6.78 GB → 1.63 GB incidentally during v5.4.2 backend rebuild. This hook prevents regression on future dep bumps. See `docs/ARCHITECTURE_INVARIANTS.md` P9 section.

---

## v5.5.0 — V1a backend /metrics endpoint (2026-05-22)

**What ships:** `yadgar/embed_service_metrics.py` + GET `/metrics` on `yadgar.embed_service:app`.
Core version 5.4.5 → 5.5.0. Backend version 5.0.3 → 5.1.0.
`prometheus_client` was already a main dep — no new package install needed.

### 1. Build both images

```bash
# Backend (5.1.0) — embed_service_metrics.py is part of shared package
podman build -f Dockerfile.backend -t openfantasy/yadgar-backend:5.1.0 .

# Core (5.5.0)
podman build -f Dockerfile -t openfantasy/yadgar:5.5.0 .
```

### 2. Nix bump — update both versions in nix config

In `~/git/nix` (adjust paths to match your nix module):

```bash
# Bump yadgar version 5.4.5 → 5.5.0
# Bump yadgar-backend version 5.0.3 → 5.1.0
# Then:
home-manager switch
```

### 3. Restart services

```bash
systemctl --user restart yadgar-backend.service
systemctl --user restart yadgar.service
```

### 4. Verify /metrics endpoint

```bash
# Should return 200 with Prometheus text output
curl -s http://127.0.0.1:8001/metrics | grep yadgar_embed

# Expected families:
# yadgar_embed_rerank_requests_total{mode="ce|nli|pair"}
# yadgar_embed_rerank_503_total{mode="ce|nli|pair"}
# yadgar_embed_rerank_duration_seconds{mode="ce|nli|pair"}
# yadgar_embed_rerank_semaphore_held{mode="ce|nli|pair"}
# yadgar_embed_model_loaded{model="ce|nli|pair|embedding"}
# process_* (RSS, CPU seconds, open FDs)
# python_info
```

### 5. Prometheus scraper config (example)

`/metrics` is **unauthenticated** on port 8001 (loopback only — `127.0.0.1:8001:8001`).
Prometheus scrapers on localhost need no bearer token.

```yaml
# prometheus.yml scrape config — add to scrape_configs:
- job_name: yadgar-backend
  static_configs:
    - targets: ['127.0.0.1:8001']
  metrics_path: /metrics
  scrape_interval: 15s
```

**Security note:** port 8001 is bound to loopback (`127.0.0.1`) only, per `docker-compose.yml`.
External hosts cannot reach it without an explicit port forward. No auth needed on loopback-only endpoints.

### 6. Image size check (post-build)

`prometheus_client` adds ~100 KB to the backend image. Backend should remain well under the 2.0 GB cap.

```bash
python scripts/check_image_size.py --image-type backend
```

---

## v5.7.0 PR-4 — vacuum_now() trigger-file pattern (2026-05-26)

### Why

The previous `vacuum_now()` MCP tool called `systemctl --user start --no-block yadgar-vacuum.service`
directly from inside the yadgar process.  When yadgar runs in a container, this systemctl call cannot
reach the host's systemd session (no dbus socket mounted into the container) — the trigger silently
failed or raised a RuntimeError.

The fix is a clean container ↔ host separation: yadgar writes a trigger file; a host-side systemd
path-watch unit picks it up and starts `yadgar-vacuum.service`.

### What changed in yadgar

- `yadgar/ops.py` — `_fire_vacuum_service()` now writes an atomic trigger file instead of calling
  systemctl.  The file path is controlled by `YADGAR_VACUUM_TRIGGER_PATH` (default:
  `/data/triggers/vacuum_requested`).  The file contains one-line JSON:
  `{"requested_at": "<ISO8601>", "source": "vacuum_now"}`.  Write is atomic (`*.tmp` then
  `os.replace()`).  Parent directory is created if missing.  Returns the `Path` written; raises
  `RuntimeError` on I/O failure.
- `yadgar/server/tools/admin_vacuum.py` — vacuum_now() MCP tool simplified: removed
  `detect_service_mode()` check, `is-active` subprocess check, and all `host_command` /
  `service_unit` / `shell_command` fields.  New response field: `trigger_path` (str path written,
  or `None` when skipped).
- `yadgar/config_registry.py` — new knob `YADGAR_VACUUM_TRIGGER_PATH` (default
  `/data/triggers/vacuum_requested`, kind=string) registered; appears in `/admin/config`.

### Host-side requirement (separate nix-repo change)

A systemd path-watch unit pair is needed on the host to bridge the trigger file to the service.
This is tracked as a follow-up in `~/git/nix/modules/home/yadgar.nix`.

Example unit fragments (do NOT apply manually — add via the nix module):

**`yadgar-vacuum-trigger.path`**:
```ini
[Unit]
Description=Watch for yadgar vacuum trigger file
After=yadgar.service

[Path]
PathExists=/data/triggers/vacuum_requested
Unit=yadgar-vacuum-trigger.service

[Install]
WantedBy=default.target
```

**`yadgar-vacuum-trigger.service`** (one-shot, removes trigger file then starts vacuum):
```ini
[Unit]
Description=Dispatch yadgar vacuum on trigger-file appearance

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'rm -f /data/triggers/vacuum_requested && systemctl --user start yadgar-vacuum.service'
```

Enable the path unit:
```bash
systemctl --user daemon-reload
systemctl --user enable --now yadgar-vacuum-trigger.path
```

### Graceful degradation

Until the path-watch unit is deployed, `vacuum_now()` still writes the trigger file and returns
`started=True` — the file simply accumulates until the watcher is wired.  No error is surfaced to
the caller; the only observable effect is that vacuum doesn't actually start.  Operators can check
for a stale trigger file at `/data/triggers/vacuum_requested` as a diagnostic signal.

### Known gap: auto-trigger in containers

`consolidation/__init__.py:218-231` contains its own `systemctl is-active` pre-check that returns
early on `FileNotFoundError`.  This means the auto-vacuum trigger (via `ConsolidationScheduler`)
is still skipped in containerized deploys — it calls `_fire_vacuum_service` but only after a
systemctl check that fails first.  Fixing the consolidation pre-check is out of scope for PR-4.

---

## v5.7.0 PR-5 — VACUUM_AUTO_THRESHOLD_BYTES is an emergency backstop (docs-only) (2026-05-26)

### Mental model change

Before v5.7.0 `VACUUM_AUTO_THRESHOLD_BYTES` was the **primary** vacuum trigger: vacuum fired when
the DB grew beyond the threshold inside the configured time window.  This was also the only
scheduled path; `vacuum_now()` existed for manual one-offs.

From v5.7.0, a **nightly cron at 19:00 UTC** (`yadgar-vacuum.timer`, shipping in PR-1a/1b) becomes
the primary trigger.  It runs unconditionally — DB size does not matter.

The threshold-driven path in `ConsolidationScheduler._maybe_auto_vacuum()` now plays a **narrower,
emergency-only role**: it catches runaway DB growth that happens between nightly cron cycles (e.g.
a bulk import that pushes the DB past 2 GiB at 22:00).  The per-day cooldown in that function
prevents double-fires on days when both cron and the threshold would trigger.

### Trigger precedence summary (v5.7.0+)

1. **Primary** — nightly cron at 19:00 UTC (`yadgar-vacuum.timer`).  Unconditional.
2. **Emergency backstop** — `VACUUM_AUTO_THRESHOLD_BYTES` (default 2 GiB) + window guard in
   `_maybe_auto_vacuum()`.  Fires only between cron cycles when DB exceeds the threshold.
3. **Manual** — `vacuum_now()` MCP tool writes a trigger file (see PR-4 above).

### No config migration required

All four `VACUUM_AUTO_*` knobs remain unchanged and functional:

| Knob | Default | Role |
|---|---|---|
| `VACUUM_AUTO_ENABLED` | `True` | Enable/disable the backstop path entirely. |
| `VACUUM_AUTO_THRESHOLD_BYTES` | `2147483648` (2 GiB) | DB size that arms the backstop. |
| `VACUUM_AUTO_WINDOW_START` | `19:00` | Backstop fires only after this local time. |
| `VACUUM_AUTO_WINDOW_END` | `23:00` | Backstop fires only before this local time. |

Existing deployments keep working without any env-var changes.  The only observable difference is
that vacuum will also fire at 19:00 UTC once `yadgar-vacuum.timer` is deployed (PR-1a/1b).
