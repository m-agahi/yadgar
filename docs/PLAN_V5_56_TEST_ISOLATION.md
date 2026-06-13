# PLAN v5.56 — Test-suite xdist isolation paydown (umbrella)

Status: **PLANNED 2026-06-13.** Surfaced by v5.54.5, the first full CI run after the
CI skip-gate was lifted. Under `pytest -n auto --reruns 2`, the `test` job fails with
~14 errors that **pass in isolation** — cross-worker / cross-test global-state pollution
accumulated invisibly over many releases while CI was skipped.

This BLOCKS releases: `.forgejo/workflows/ci.yaml` has `publish: needs: test`, so a red
`test` job hard-blocks the PyPI publish on tag. Greening `test` is a prerequisite for
any tagged release, including v5.54.5.

Not to be confused with **v5.55** (complexity governance) or **v5.90** (grandfathered
complexity refactor) — this is purely test-harness hygiene.

## Observed failures (one `-n auto` run, 2026-06-13, branch fix/v5.54.5-ci-green)
14 failed / 5454 passed / 28 rerun. The victim set is **NON-DETERMINISTIC** between runs
(an earlier run showed a different set of 8). Roots:

### Root A — MCP server-singleton reload pollution (~8 failures)
A test reloads `yadgar.server` (or a submodule), rebinding `mcp_server` to a fresh
FastMCP instance whose `@mcp_server.custom_route` / tool decorators (registered as an
import side-effect of `yadgar/server/http.py` etc.) never re-run. Later tests on the
same worker that read `server.mcp_server` see an EMPTY route/tool registry. Symptoms:
- `test_server::test_mcp_server_has_tools` — tool set `set()`.
- `test_logs_api::test_route_module_self_registers` — `/api/logs` routes `set()`.
- `test_viz_config_endpoint`, `test_viz_legend` ×2 — `GET /api/viz/config` → 404.
- `test_v579_smart_sessionstart` ×2 — `GET /hooks/session-context` → 404.
- `test_transport::test_health_endpoint_on_both_transports` — `/health` missing.
- `test_log_rotation::test_unwritable_path_no_raise` — stray file handler (logging variant).

### Root B — consolidate-anchor sentinel leak (×3)
`test_consolidate_anchor_pass` sentinels come back `[]` under load. The v5.54.5
`_restore_logging_state` fixture did NOT fully close this — there is residual shared
state beyond `logging.disable` (a consolidation singleton or settings cache).

### Root C — logging framework-logger leak (~2)
`test_structured_logging::test_uvicorn_access_emits_json` empty output. Same family as
Root B — the single `logging.disable` restore was necessary but not sufficient under
full `-n auto`.

## Key diagnostic insight (do NOT repeat the v5.54.5 mistake)
- **Pair-repro can mislead.** `test_security_headers + test_server::test_mcp_server_has_tools`
  PASSED together — so security_headers is NOT (alone) the polluter. The real polluter
  is elsewhere / needs a specific co-set. Confirm a polluter→victim pair DETERMINISTICALLY
  (`-p no:randomly`, victim fails after polluter, passes alone) before fixing.
- **Victim-quarantine does NOT work.** Reload pollution has nondeterministic victims;
  skipping or serial-laning the *named* victims just shifts pollution to new victims next
  run. The fix must target the POLLUTER, not the victim.

## Fix strategy (preferred: source-fix the polluters)
1. **Enumerate polluters.** `grep -rn "importlib.reload\|sys.modules\[" yadgar/tests/`.
   Filter to those that reload `yadgar.server` / `yadgar.server.http` / any module whose
   import registers routes/tools or installs logging handlers.
2. **Restore in `finally`** — the pattern `test_admin_invariants_module.py:149-161`
   already uses correctly: snapshot the mutated ATTRIBUTE (`saved = srv.mcp_server`) at
   setup, restore it in teardown. NOTE: restoring `sys.modules[...]` does NOT undo a
   reload (reload mutates the same module object in place) — you must restore the
   rebound attribute, or re-run the route/tool registration.
3. **Consider a defensive autouse fixture** as a backstop ONLY if source-fixes leave
   residual leakage: snapshot `server.mcp_server` (+ root logging handlers) at setup,
   restore at teardown. Cheaper than chasing every polluter, but masks rather than fixes
   — use as belt, not the primary fix.
4. **Roots B/C:** find the consolidation/settings singleton that leaks; extend the
   teardown the way `_restore_logging_state` did for `logging.disable`.

## Fallback (if source-fix proves a tar-pit): quarantine the POLLUTERS, not victims
Mark the reload-heavy tests `@pytest.mark.serial`, exclude from the `-n auto` lane
(`-m 'not serial'`), run them in a second serial CI step (`-p no:xdist`). Removes
polluters from the parallel lane so victims stay clean. Document every quarantined test
(no silent caps). Less honest than a root fix — a campaign tail item, not the goal.

## Definition of done
- `pytest yadgar/tests/ -n auto --reruns 2` green across 3 consecutive runs (the
  non-determinism means one green run is not proof).
- No `@pytest.mark.serial` quarantines left without a tracking entry here.
- `publish: needs: test` can be satisfied → releases unblocked.

## Dependency on v5.55 — RE-AUDIT before/after, do not freeze this plan
v5.55 (complexity paydown) **moves and splits a large amount of code** — Tier-3 file
splits (`server/http.py`, `server/tools/project.py`, `wiki.py`, etc.), function
extractions, changed import side-effects. That directly perturbs the import-time route/
tool registration and module-reload behavior this plan is about. So the polluter/victim
map below **will change** as v5.55 lands.

Therefore v5.56 is NOT a fixed list — it is a re-audit loop:
1. **Re-run the full audit** (`pytest yadgar/tests/ -n auto --reruns 2`, ≥3 runs to beat
   non-determinism) at the START of v5.56 work AND after each significant v5.55 wave.
2. **Re-derive the polluter set** from scratch each time — `grep importlib.reload /
   sys.modules` + deterministic polluter→victim pairing. Do not trust the failure list in
   this doc; it is a 2026-06-13 snapshot that v5.55 will invalidate.
3. **Update this plan** (the failure taxonomy in "Observed failures") to match the current
   reality before fixing — stale targets waste cycles.
4. Some pollution may **resolve for free** when v5.55 splits a monolith into better-isolated
   modules; some may **shift** to new victims. Re-scan, never assume.

Coordinate sequencing with v5.55: if a Tier-3 split is imminent for a file implicated in
Root A, prefer landing that split first, then re-audit, rather than fixing a polluter that
the split will relocate.

## Sequencing
Gates every tagged release (`publish: needs: test`), so land EARLY — but re-audit first
(above). Each polluter fix is a small PR; batch by root (A, then B, then C). Verify
deterministically per fix; reserve the 44-min `-n auto` run for the final 3-run gate only.
