# C0 — wiki_add MCP gate fix

## Goal

Make `wiki_add(project="m-agahi/yadgar", ...)` succeed over MCP transport when `directory=None` is not supplied. The current call returns `{"stored": false, "ok": false, "error": "unresolved_project"}` because `_check_wiki_add_context` rejects on `directory=None` BEFORE the resolver sees the project override. C0 unblocks the c1–c10 planning agents that need to write plan docs via `wiki_add`.

## Pre-conditions

- on `master` at HEAD `6aa4bce9` (clean)
- `pytest` available; venv has `dev` + `test` extras
- no live MCP write attempts until this lands

## Root cause

The task brief stated: "FastMCP transport strips keyword-only kwargs before binding, so the Python body sees `project=None`, the strict 4-tier resolver raises `UnresolvedProjectError` at tier 4."

That premise is **wrong**. Source-verified via memory 7776 (`guards-and-tooling-in-this-repo-that-do-not-check-what-their-nam`): mcp 1.27.0's `mcp.server.mcpserver` passes kwargs through to the registered handler based on the PUBLISHED SCHEMA; it drops only args that are NOT in the schema. `project` IS in the schema for `wiki_add` (it is a declared parameter at `yadgar/core/server/tools/wiki.py:428`), so FastMCP forwards it. The async wrapper `_instrumented_async` at `yadgar/core/server/_app.py:685` accepts `**kwargs`, pops only `ctx`/`context` (L703, L718), and forwards the rest via `run_offloaded(traced_func, *args, **kwargs)` at L734 — keyword-only kwargs are NOT stripped.

The actual mechanism is order-of-operations in `wiki_add`:

1. `yadgar/core/server/tools/wiki.py:534` — `_decision = _check_wiki_add_context(directory)`
2. `yadgar/core/server/tools/wiki.py:95–96` — if `directory` is empty/None, returns `{"stored": False, "ok": False, **UnresolvedProjectError("wiki_add").payload}` IMMEDIATELY. The payload's `error` field is `"unresolved_project"` (`yadgar/_shared/errors.py:77`).
3. `yadgar/core/server/tools/wiki.py:548` — the `resolve_effective_project(project=project, ...)` call that WOULD have used the supplied project override NEVER EXECUTES. The function already returned.

So `project=` is irrelevant when `directory=None`. The directory gate short-circuits before the resolver runs, and the gate has no knowledge of the project override. The error envelope reuses the resolver's `unresolved_project` shape (per C5 / C13 — the same `UnresolvedProjectError` payload is the documented answer at every boundary), which is why the symptom reads as a resolver failure.

`_resolve_wiki_read_project` / `_resolve_wiki_query_project` at `yadgar/core/server/tools/wiki.py:728, 708` "accept" project because they call the resolver directly without an upstream directory gate — they have no pre-resolver `_check_*_context` reject step to short-circuit on.

## Step-by-step

1. **Thread `project` into `_check_wiki_add_context`.** Change the signature from `_check_wiki_add_context(directory: str | None) -> dict` to `_check_wiki_add_context(directory: str | None, *, project: str | None = None) -> dict`. The `project` arg stays keyword-only. File: `yadgar/core/server/tools/wiki.py:62–97`.

2. **Resolve-before-gate.** In `_check_wiki_add_context`, when `(directory or "").strip()` is empty AND `project` is a non-empty string, attempt `resolve_effective_project(project=project, directory=None, session_project=None, tool="wiki_add")` and return `{}` on success. Catch `InvalidProjectOverrideError` and re-raise via the same `UnresolvedProjectError` envelope so a malformed `project=` still fails clean (one shape of answer — the C13 invariant). File: `yadgar/core/server/tools/wiki.py:95–96`.

3. **Update call site.** Change `yadgar/core/server/tools/wiki.py:534` to pass `project=project` into `_check_wiki_add_context`. The existing `resolve_effective_project` call at L548 stays — it now becomes a redundant no-op (the gate already resolved) but its presence guards the `assert_project_registered_for_create` registry check, which `_check_wiki_add_context` does NOT perform. To avoid double-resolution churn, change L548 to use the resolved id returned from the gate (the gate's call result is discarded otherwise — restructure so the gate RETURNS the resolved project_id on the success path, and L548 picks it up).

4. **Plumb the gate's return.** New signature: `_check_wiki_add_context(directory: str | None, *, project: str | None = None) -> tuple[dict, str | None]`. Tuple `(envelope, resolved_project_id)`. On success: `({}, resolved_project_id)`. On reject: `({"stored": False, ...}, None)`. Call site L534 unpacks both. L548 then uses the already-resolved id (or skips re-resolution if id is non-None).

5. **Regression test.** New file `yadgar/tests/core/test_c0_wiki_add_project_override.py`. Two tests:
   - `test_wiki_add_project_override_satisfies_directory_gate` — call `wiki_add(title=..., content=..., project="m-agahi/yadgar", directory=None)` via direct (sync) call (the test bypasses MCP transport and exercises the function body — equivalent to the post-binding state). Asserts the call does NOT return the `unresolved_project` envelope. Pre-fix: returns the envelope (red). Post-fix: reaches the secret-gate or downstream step (green).
   - `test_wiki_add_directory_None_without_project_still_rejects` — same call without `project=`. Asserts the envelope is still returned with `error == "unresolved_project"`. (Pre-fix and post-fix both green — guards against the fix accidentally opening the gate.)

6. **Run targeted tests.** `pytest yadgar/tests/core/test_c0_wiki_add_project_override.py -v`. Then `pytest yadgar/tests/core/test_car5_project_id_create_enforcement.py -v` (proves no regression on the registry gate). Then the wiki suite: `pytest yadgar/tests/core/ -k wiki -q`.

## Verification

The red→green transition on `test_wiki_add_project_override_satisfies_directory_gate` is the load-bearing assertion. Pre-fix it returns the `unresolved_project` envelope; post-fix it reaches the secret-gate (`gate_or_reject` at wiki.py:517) or the size gate (L512). Whichever downstream gate fires, the absence of the `unresolved_project` envelope is the success criterion.

Acceptance: live `mcp__yadgar__wiki_add(project="m-agahi/yadgar", title="probe-c0", content="...", category="reference")` over MCP returns `{"stored": True, "queued": True, ...}` (or a `wait=False` success envelope), NOT `{"stored": False, "error": "unresolved_project"}`.

## Risks / rollback

- **Risk:** Double-resolution (gate + L548) is a perf wart, not a correctness bug. Mitigation is step 4 (gate returns the resolved id). If step 4 is skipped, the gate's resolver call is wasted but the L548 call uses the same kwargs and reaches the same id.
- **Risk:** Opening the gate to project override could allow a `wiki_add(project="anything", directory=None)` that previously was rejected. Per Car 5, the registry check (`assert_project_registered_for_create` at L559) still runs at L548 — so an unregistered `project=` is rejected at the registry, NOT at the directory gate. Net security: unchanged. An unregistered project gets a different error envelope (`UnknownProjectError`), still fail-loud.
- **Risk:** `resolve_effective_project` raised inside `_check_wiki_add_context` could fire `_identity_or_skip` warnings on the sentinel paths. None of the three sentinels (`global` / `unresolved` / `system`) pass as a `project=` value — `_reject_sentinel` at L185 raises `InvalidProjectOverrideError`. Mitigation: catch and re-raise via the same envelope.
- **Rollback:** revert the commit. The fix is local to `wiki.py` (one helper signature + one call-site change). The regression test is in its own file. No migration or schema change.

## Approx LOC + risk class

- Source diff: ~25 lines in `yadgar/core/server/tools/wiki.py` (signature + body + call site)
- New test file: ~60 lines
- Risk class: **LOW** — the change is local, the new error envelope is structurally identical to the old one, and the registry check still gates unregistered project_ids.

## Source evidence

- `yadgar/core/server/tools/wiki.py:411–430` — `wiki_add` signature; `project` declared at L428 after `*,` (keyword-only)
- `yadgar/core/server/tools/wiki.py:62–97` — `_check_wiki_add_context`; rejects on empty `directory` at L95–96 with `UnresolvedProjectError("wiki_add").payload`
- `yadgar/core/server/tools/wiki.py:534` — pre-resolver gate call (the short-circuit site)
- `yadgar/core/server/tools/wiki.py:543–568` — `resolve_effective_project` + `assert_project_registered_for_create` block; never reached when `directory=None`
- `yadgar/core/server/tools/wiki.py:707–752` — `_resolve_wiki_query_project` / `_resolve_wiki_read_project` (no upstream gate; that's why read tools "accept" project)
- `yadgar/core/server/tools/_project_param.py:144–261` — `resolve_effective_project`; tier 4 raise at L255; sentinel skip at L185 vs L210/223/245
- `yadgar/_shared/errors.py:67–94` — `UnresolvedProjectError`; `error_code = "unresolved_project"` at L77; payload shape at L80–86
- `yadgar/core/server/_app.py:684–734` — `_instrumented_async` wrapper; kwargs forwarded at L734; `ctx`/`context` popped at L703/L718 (no other kwargs stripped)
- memory `guards-and-tooling-in-this-repo-that-do-not-check-what-their-nam` (wiki slug) — proves FastMCP drops only unknown/non-schema kwargs, not keyword-only schema kwargs
- `yadgar/tests/core/test_car5_project_id_create_enforcement.py:81–182` — `TestSentinelsRejectedAtResolution`; reference for assertion style
- `yadgar/tests/core/test_car5_project_id_create_enforcement.py:188–234` — `TestRegistryCheck`; proves registry check still fires after fix
