# Hook-Install Hygiene — FIXES-train Car #64

**Status:** DRAFT — awaiting audit
**Date:** 2026-07-13
**Scope:** core-only (`yadgar/core/install/install_hooks_lib.py`, `yadgar/tests/conftest.py`, `yadgar/tests/hooks/`)
**Target version:** core 5.134.0 → next patch (5.134.1 or 5.135.0)

---

## BLUF

Two defects in the hook-install path, verified against observed state on 2026-07-13:

1. **Test-HOME leak (primary, user's explicit ask).** No global test fixture forces `HOME` to a tmp dir. Isolation is per-test and scattered (`monkeypatch.setenv("HOME", …)` in some files, `monkeypatch.setattr(Path, "home", …)` in others, `home_dir=tmp_path` in most `install_hooks_impl` callers). Any NEW test that calls the MCP/server wrapper `install_hooks()` (which hardcodes `home_dir=Path.home()`, `misc.py:451`) or `sync_instructions()` without a HOME patch will write/unlink inside the REAL `~/.claude/hooks/`. This session, a real-HOME install deleted `~/.claude/hooks/db-lockdown` ~5× and blocked Bash. **Fix: prevention-by-construction** — a session + function autouse fixture in the top-level `conftest.py` that redirects `HOME` for the WHOLE suite, plus a belt-and-suspenders sentinel assertion on `~/.claude/hooks/`.

2. **Orphan / duplicate hook scripts in `~/.claude/hooks/`.** `_copy_scope_scripts` (install_hooks_lib.py:523-548) copies **non-prefixed** script names (`file-changed.py`, `post-tool-capture.py`, `prompt-recall.py`, `session-start-context.py`, `subagent-{start,stop}.py`, `instructions-loaded.py`, `pre-compact-drain.sh`, `post-compact-rehydrate.sh`) into `hooks_dir`. For `scope="global"`, `hooks_dir == global_hooks_dir == ~/.claude/hooks` (verified `_resolve_scope_paths`:517-518), so a global install writes BOTH the non-prefixed copy AND the `yadgar-`-prefixed managed copy of the same logic. The non-prefixed copies are **referenced nowhere** — settings.json points every core hook at `hook_runner.py <hook-type>`, and `hook_runner.py` dispatches to an internal `_HOOKS` handler dict (hook_runner.py:275+), never execs a sibling script. They are genuinely vestigial. **Fix: root-cause (stop emitting non-prefixed names for global scope) + legacy-sweep (remove orphans left by prior installs, provenance-guarded).** The existing `yadgar-db-lockdown-check.py` orphan unlink (install_hooks_lib.py:403-409) is extended into a general sweep.

Router install is otherwise correct (verified — see Part 3).

---

## The incident (verified)

- **Real-HOME deletion.** During this session, hook installs targeting the real `~/.claude` deleted `~/.claude/hooks/db-lockdown-*` ~5×, intermittently blocking the Bash tool (its PreToolUse guard vanished). Root cause: a code path resolving `home_dir=Path.home()` ran against the developer's live home rather than a tmp dir. The MCP wrapper `install_hooks()` at `misc.py:451` passes `home_dir=Path.home()` unconditionally; there is no test-suite-wide HOME redirect, so a wrapper-level test (or the tool fired in-session) touches real `~/.claude`.
- **Orphan dupes — live `~/.claude/hooks/` listing (2026-07-13):**
  - Non-prefixed vestigial copies present alongside `yadgar-`-prefixed managed copies:
    `file-changed.py`, `instructions-loaded.py`, `post-tool-capture.py`, `prompt-recall.py`, `session-start-context.py`, `subagent-start.py`, `subagent-stop.py`, `post-compact-rehydrate.sh`, `pre-compact-drain.sh`.
  - Orphan `yadgar-db-lockdown-check.py` (1151 bytes, non-executable) — the router (`yadgar-pretooluse-router.py`) subsumed it (HOOKS Car 1 / G4); PreToolUse now routes to the router. The install lib already best-effort-unlinks this one file (install_hooks_lib.py:403-409) but it was reintroduced by an install predating that unlink, or a manual copy — the general sweep supersedes the single-file unlink.

**Vestigial-status proof (BLOCKER-1 resolved).** `hook_runner.py` reads `argv[1]` as the hook type, looks it up in the module-level `_HOOKS` dict, and calls the bound handler function (`hook_post_tool_capture`, `hook_session_start_context`, `hook_prompt_recall`, …) defined IN the runner and importing from the `yadgar` package. It does NOT read, import, or exec the non-prefixed sibling `.py` files in `~/.claude/hooks/`. Deleting them cannot break any wired hook. (hook_runner.py `main()` at :287-301, `_HOOKS` at :275+.) The one filesystem-looking literal in the runner — `"/hooks/prompt-recall"` (hook_runner.py:187) — is confirmed benign: it is an HTTP route string passed to `_http_get(...)` (the runner uses `urllib.request` to call the daemon), NOT a filesystem path or a sibling-script reference. No suffix, no `open()`/`Path()`/exec around it.

---

## Part 1 — Global test-HOME-isolation guard (design)

### Home resolution in the tested code (verified file:line)

| Call site | Resolution | Covered by `setenv("HOME")`? |
|---|---|---|
| `install_hooks_impl(home_dir, …)` | explicit param | n/a — callers pass `tmp_path` |
| `_stable_python(home_dir=None)` fallback → `Path.home()` | install_hooks_lib.py:184 | yes (POSIX `Path.home()` honors `$HOME`) |
| `_pipx_python(home_dir)` | param, threaded from `home_dir` | n/a |
| MCP wrapper `install_hooks()` → `home_dir=Path.home()` | **misc.py:451** | yes |
| CLI `cmd_install_hooks()` → `home_dir=Path.home()` | **core/cli/install_hooks.py:21** | yes |
| `sync_instructions()` → `Path.home() / ".claude" / "CLAUDE.md"` | **misc.py:468** | yes |
| `platform_paths.py` claude-dir helpers → `Path.home()` | platform_paths.py:33/38/40 | yes |

**"Locate the ONE non-isolated test" — conclusion (verified).** There is NO currently-live leaking test. Every wrapper/CLI caller is safe by one of three means: passes `home_dir=tmp_path` explicitly (most `install_hooks_impl` callers, `test_session_end_capture.py`, `test_instructions_loaded_hook.py`, `test_file_changed_hook.py`, `test_install_hooks_sweep.py`); patches HOME (`test_blocks_hooks_phase4.py:286/310`, `test_install_hooks_router.py:18/72`, `test_server.py`, `test_install_hooks_injection.py`) or `Path.home` (`test_atomic_config_writes.py:129`); or **mocks** `install_hooks_impl` so no real write occurs (`test_cli_install_hooks_module.py` — `_make_args` defaults `dry_run=False` but every `cmd_install_hooks` call is under `patch("…install_hooks_impl")`, so the CLI's hardcoded `home_dir=Path.home()` at core/cli/install_hooks.py:21 is never exercised against the real filesystem). **Both** the MCP wrapper (misc.py:451) and the CLI (core/cli/install_hooks.py:21) hardcode `home_dir=Path.home()`; the leak is therefore **latent, not live** — any FUTURE wrapper/CLI test that neither mocks `install_hooks_impl` nor patches HOME will write to real `~/.claude`. The global guard is prevention-by-construction closing that latent path; the sentinel (below) is the tripwire that fails such a test loudly instead of silently mutating the developer's home.

**Resolution fact for the auditor:** on POSIX, `pathlib.Path.home()` and `os.path.expanduser("~")` both read the `$HOME` environment variable (falling back to `pwd` only when `$HOME` is unset). So a single `monkeypatch.setenv("HOME", <tmp>)` redirects EVERY `Path.home()` / `expanduser("~")` call in the tested code — including the wrapper's hardcoded `home_dir=Path.home()` at misc.py:451. No monkeypatching of `Path.home` itself is required, and env-patch is strictly broader than patching one bound name.

**Known interaction:** a handful of tests do `monkeypatch.setattr(Path, "home", lambda: tmp_path)` (e.g. `test_atomic_config_writes.py:129`). Function-scoped `monkeypatch` applies AFTER (and is torn down before) the autouse guard's function layer, and every such test points `home` at its own `tmp_path` — never at real home. The guard therefore never conflicts with them; it only closes the gap for tests that patch NEITHER.

### Fixture placement + what it patches

Mirror the existing dual-scope pattern already in `conftest.py` (`_isolate_yadgar_paths_session` session + `isolate_yadgar_paths` function):

1. **Session layer** — `_isolate_home_session(tmp_path_factory)`, `scope="session", autouse=True`. Uses `_pytest.monkeypatch.MonkeyPatch()` (function `monkeypatch` is unavailable at session scope — ScopeMismatch). Creates `<session_root>/home`, `mkdir` a `.claude/hooks` inside it, and `mp.setenv("HOME", str(session_home))`. This precedes any module-scoped fixture (`_engines`, `module_storage`) that might construct paths off `Path.home()` at module setup — same rationale the existing session path-guard documents.
2. **Function layer** — extend the existing autouse `isolate_yadgar_paths(tmp_path, monkeypatch)` (or add a sibling autouse `isolate_home`) to also `monkeypatch.setenv("HOME", str(tmp_path / "home"))` and `mkdir(parents=True)` the `home/.claude/hooks` subtree. Function scope wins, so each test gets a fresh per-test HOME on top of the session default (matching how the XDG dirs already work).

Both layers set only `HOME`. (Do NOT also patch `Path.home` globally — a broad `setattr` would defeat the tests that legitimately patch it to their own tmp, and env-patch already covers the resolution.)

### Belt-and-suspenders sentinel assertion

The incident was **in-place deletion one level below `~/.claude`** (inside `~/.claude/hooks/`). A dir-mtime check on `~/.claude` will NOT catch a child-file overwrite/unlink. Therefore snapshot the real `~/.claude/hooks/` recursively:

- A `scope="session", autouse=True` fixture computes, at the TOP of the session (before the HOME redirect takes hold for the real path — read via the process's original `$HOME` captured at conftest import), a sentinel: the set of `(name, st_mtime_ns, st_size)` tuples for every entry directly under the real `~/.claude/hooks/` (or a single sentinel file's mtime if the dir is large). Store it in a module global.
- On `pytest_sessionfinish` (or the fixture's teardown), recompute against the real `~/.claude/hooks/` and assert the snapshot is unchanged. Any drift → the guard leaked; fail the session loudly with the offending entries.
- Guard the sentinel with `if real_hooks_dir.exists()` so CI (no `~/.claude`) is a no-op. Never mutate the real dir from the sentinel — read-only stat.

This is a tripwire: if a future test bypasses the HOME guard, the sentinel converts a silent real-HOME mutation into a red session.

---

## Part 2 — Orphan-sweep design

### Root cause first (defensible, per audit refinement)

The dupes are **install-created**, not merely install-tolerated: `_copy_scope_scripts` re-emits the 9 non-prefixed names on every install. A sweep-after-copy in the same run is write-then-delete whose convergence depends on call order. The primary fix is to **stop creating them**:

- For `scope="global"` (where `hooks_dir == global_hooks_dir`), the non-prefixed `_copy_scope_scripts` copies are pure vestige — nothing dispatches to them. **Drop `_copy_scope_scripts` for global scope entirely**, OR narrow its `_files` set to the scripts that are still legitimately consumed by a non-prefixed path (audit finds NONE — hook_runner dispatches internally; the four append-hooks are copied under `yadgar-` names by `_install_append_hooks`; stop/session-end/router are copied under `yadgar-` names by `_install_global_scripts`). Recommended: **remove `_copy_scope_scripts` from the global-scope install path**; keep it only if a project-scope consumer is proven to read non-prefixed names (none found — verify once more at implementation time before deleting).
- Keep a **legacy-sweep** to clean orphans left by PRIOR installs (the current live state). Root-cause alone does not remove already-present files.

### Sweep predicate (provenance-guarded — prefix guarantee does NOT carry over)

The orphans are **non-prefixed generic** names (`post-tool-capture.py`) — a user could plausibly own a file with that name. Car3's allowlist was safe only because it scoped to `yadgar-`-prefixed basenames; that guarantee is absent here. Safe predicate:

- Maintain a `_MANAGED_NONPREFIXED` allowlist = the exact 9 basenames `_copy_scope_scripts` used to emit (`file-changed.py`, `post-tool-capture.py`, `prompt-recall.py`, `session-start-context.py`, `subagent-start.py`, `subagent-stop.py`, `instructions-loaded.py`, `pre-compact-drain.sh`, `post-compact-rehydrate.sh`).
- On install, for each `name` in `_MANAGED_NONPREFIXED`, unlink `global_hooks_dir / name` **only when its `yadgar-`-prefixed managed sibling exists in the same dir** (`(global_hooks_dir / f"yadgar-{name}").exists()`) — presence of the prefixed sibling proves the non-prefixed copy is OUR vestige, not a coincidental user file. As a stronger alternative, gate on content-hash equality against the packaged source (`package_hooks / name`); the sibling-existence check is simpler and sufficient given the sibling is always installed in the same pass.
- Extend the existing single-file unlink to also remove `yadgar-db-lockdown-check.py` (already done at install_hooks_lib.py:403-409; fold it into the same sweep helper for one code path). db-lockdown removal is unconditional — the router subsumed it and settings never references it again.
- `try/except OSError: pass` per unlink (best-effort; a missing file or perms error must never fail an install), matching the existing orphan-unlink style.
- Never touch names outside the allowlist; never touch `hook_runner.py`, any `yadgar-*` managed script, or `yadgar-hook-exceptions.json`.

Place the sweep in a new helper `_sweep_stale_hook_scripts(global_hooks_dir, dry_run)` called from `_install_global_scripts` (or `install_hooks_impl` right after global scripts are copied), replacing the inline db-lockdown unlink. No-op on `dry_run`.

---

## nix-convergence note (out-of-tree)

The user's nix dotfiles deploy hook scripts independently to `~/.claude/hooks/` from `dotfiles/common/yadgar-hooks/` via `packages/common/llm.nix` (per MIGRATION_NOTES.md:9-17 and wiki `yadgar-claude-code-hooks-dotfiles-common-yadgar-hooks`). Those nix-managed files are **non-prefixed** (`session-start-context.py`, `prompt-recall.py`, `post-tool-capture.py`, `pre-compact-drain.sh`, `post-compact-rehydrate.sh`). This is a SECOND, independent source of non-prefixed names.

**Convergence risk:** if the core sweep deletes non-prefixed names, and nix re-deploys them on the next `home-manager switch`, the two installers fight. Recommendation (nix-repo change, out-of-tree — hand to user via `MIGRATION_NOTES.md`, do not attempt here):

- Preferred: **retire the nix `yadgar-hooks` copy** entirely and let `yadgar install-hooks --scope=global` be the single installer (it already writes the `yadgar-`-prefixed set + settings.json). The nix module becomes a no-op or just ensures `yadgar` is on PATH.
- If nix must keep deploying: rename the nix targets to the `yadgar-`-prefixed convention AND have nix write the settings.json entries, so both installers converge on ONE naming scheme and the core sweep's allowlist can exclude nix-owned names. This is a larger nix change.

Flag explicitly: the core sweep's provenance guard (delete non-prefixed only when `yadgar-`-prefixed sibling exists) means a nix-only deployment WITHOUT the core install present will NOT be swept — the guard is conservative by design. The fight only occurs when BOTH installers have run. Document this interaction for the user; the core-side change is safe in isolation.

---

## Acceptance criteria

### Unit (must pass)

1. **Guard — un-patched wrapper/CLI call resolves to tmp HOME, not real home.** A test that calls the MCP/server `install_hooks(project_directory=<tmp>, scope="global")` (NOT mocked, NO per-test HOME patch) asserts the settings.json and hook scripts land under the guard's tmp HOME, and that `Path.home()` observed inside the test is the tmp HOME. This is the regression pin for the latent leak: without the guard this test WOULD write to real `~/.claude`; with it, the write is redirected. Pair with a sentinel check (see #1b) that the real `~/.claude/hooks/` is untouched.
   - **#1b (sentinel):** the session sentinel fixture (Part 1) asserts the real `~/.claude/hooks/` snapshot is byte-identical at session end — the suite-wide tripwire, guarded on `real_hooks_dir.exists()` so CI is a no-op.
2. **Guard — default `install_hooks_impl` with no explicit home never resolves to real home.** A test asserts `Path.home()` inside a test process equals the guard's tmp HOME, not the developer's real home.
3. **Sweep removes non-prefixed dupes.** Seed `global_hooks_dir` with all 9 non-prefixed names AND their `yadgar-`-prefixed siblings; run a global install; assert every non-prefixed name in `_MANAGED_NONPREFIXED` is gone and every `yadgar-`-prefixed managed script remains.
4. **Sweep preserves foreign hooks.** Seed a NON-managed file named e.g. `post-tool-capture.py`-lookalike that is NOT accompanied by a `yadgar-`-prefixed sibling (i.e. a user's own `my-custom.py`, and separately a non-prefixed name whose prefixed sibling is absent); assert it survives. This discriminates the provenance guard from a naive name-allowlist.
5. **db-lockdown orphan removed.** Seed `yadgar-db-lockdown-check.py`; run install; assert it is unlinked (extends existing `test_install_hooks_router.py::test_orphan_db_lockdown_unlinked`).
6. **Re-install converges (idempotent).** Two consecutive global installs leave exactly ONE clean set: `yadgar-`-prefixed managed scripts + `hook_runner.py` + `yadgar-hook-exceptions.json`, zero non-prefixed dupes, zero db-lockdown orphan.
7. **Root-cause: global install no longer emits non-prefixed names.** After a single global install into a clean tmp HOME, assert `global_hooks_dir` contains NO name from `_MANAGED_NONPREFIXED` (the non-prefixed copies were never written, independent of the sweep).

### Regression

8. Existing hook-install suite stays green: `test_install_hooks_sweep.py`, `test_install_hooks_router.py`, `test_install_hooks_stable_python.py`, `test_install_hooks_shebang.py`, `test_install_hooks_injection.py`, `test_install_hooks_host_vs_container.py`, `test_install_hooks_lib_module.py`, `test_session_end_capture.py`, `test_blocks_hooks_phase4.py`, `test_atomic_config_writes.py`, `test_server.py` install-hooks cases.
9. `test_manifest_references_all_install_intended_scripts` still passes (removing `_copy_scope_scripts`'s `_files` dict changes the manifest reference set — update the test's `_IMPORTED_ONLY` / manifest-scan expectations accordingly; the non-prefixed names, once un-emitted, are no longer install-intended for global scope).

---

## Test plan

1. **RED first (TDD):** add acceptance tests #1, #3, #4, #7 — they fail against current `install_hooks_lib.py` (non-prefixed copies present, no global HOME guard). #1 fails ONLY if a test currently leaks; write it to prove the guard, then add the guard.
2. Add the conftest guard (Part 1). Re-run #1/#2 → green. Run the FULL suite once to confirm the suite-wide HOME redirect breaks nothing (some tests assert real-home-derived paths — `test_paths.py` uses `Path.home()`; verify those still pass because they compute EXPECTED via the same `Path.home()`, so they track the redirect).
3. Add root-cause + sweep (Part 2). Re-run #3–#7 → green.
4. Run `test_install_hooks_sweep.py` + full `yadgar/tests/hooks/` + `yadgar/tests/server/test_server.py` + `yadgar/tests/core/test_session_end_capture.py`. Loop until clean.
5. Update `test_manifest_references_all_install_intended_scripts` if the manifest reference set changed (#9).

---

## Risks

- **`test_paths.py` and other real-home-derived assertions.** These compute expected paths via `Path.home()` at test time, so the redirect moves both actual and expected together — they should stay green. VERIFY explicitly during step 2; if any test hardcodes a real-home substring, it must switch to the redirected home.
- **Session-scoped fixture ordering.** The HOME session-guard must fire before `_isolate_yadgar_paths_session` and before module-scoped `_engines`. Pytest orders session-autouse fixtures by definition/dependency; make the HOME guard have no dependency on the others and place it so it applies first, OR fold `setenv("HOME")` into the existing `_isolate_yadgar_paths_session` (single fixture, guaranteed order). Folding is lower-risk.
- **Over-deletion.** The provenance guard (delete non-prefixed only when `yadgar-`-prefixed sibling present) is conservative; the residual risk is a user who has BOTH a `yadgar-file-changed.py` and their own `file-changed.py` — extremely unlikely, and the `yadgar-` sibling is ours by definition. Content-hash gate is available if stricter proof is wanted.
- **nix fight (out-of-tree).** Covered above; core change is safe in isolation, but document the nix interaction so the user doesn't see dupes reappear after `home-manager switch`.
- **Sentinel false-positive in CI.** Guard the sentinel on `real_hooks_dir.exists()`; CI has no `~/.claude` → no-op.

---

## Scope

- **In:** `yadgar/core/install/install_hooks_lib.py` (root-cause + sweep helper), `yadgar/tests/conftest.py` (HOME guard + sentinel), new/extended tests under `yadgar/tests/hooks/`.
- **Out:** nix dotfiles repo (`dotfiles/common/yadgar-hooks/`, `packages/common/llm.nix`) — recommendation only, handed via `MIGRATION_NOTES.md`. The hyphen/underscore two-file split in `yadgar/core/hooks/` is DELIBERATE (verified 2026-07-13) — NOT part of this sweep; do not touch it.
- **No** code changes, commit, or install performed by this plan. No SurrealDB, terraform, or container exec.

---

## Version

Core-only change. Current core 5.134.0 → target next patch/minor (5.134.1 or 5.135.0 at implementation time). Backend unaffected.

---

## AUDIT (2026-07-13)

**Status: NEEDS-REWORK** — gated on a single defect (sweep predicate, Focus #3). The Part-1 HOME-guard half is AUDITED-ready; the Part-2 sweep half has a predicate that cannot match 5 of the 9 orphans and an acceptance test (#3) that fabricates state production never creates, masking the gap. Fix the predicate + two acceptance tests and this ships.

### Per-claim verification (file:line)

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| C1 | Dual-scope conftest isolation pattern (`_isolate_yadgar_paths_session` session + `isolate_yadgar_paths` function, both autouse) exists to mirror | **VERIFIED** | conftest.py:184 (`scope="session", autouse=True`, uses `_pytest.monkeypatch.MonkeyPatch()` at :206-208), conftest.py:233 (`@pytest.fixture(autouse=True)` function-scoped, `monkeypatch.setenv` at :249+) |
| C2 | `Path.home()` AND `os.path.expanduser("~")` both read `$HOME` on POSIX → a single `setenv("HOME")` covers the hardcoded `home_dir=Path.home()` sites | **VERIFIED (empirical)** | `HOME=/tmp/xyzhometest python3 -c ...` → both return `/tmp/xyzhometest` |
| C3 | MCP wrapper hardcodes `home_dir=Path.home()` at misc.py:451 | **VERIFIED** | `install_hooks_impl(home_dir=Path.home(), …)` in `misc.py` install_hooks wrapper (return block ~450-455); file is `yadgar/core/server/tools/misc.py` (plan says `misc.py:451` — matches) |
| C4 | CLI hardcodes `home_dir=Path.home()` at cli/install_hooks.py:21 | **VERIFIED** | `yadgar/core/cli/install_hooks.py:21` = `home_dir=Path.home(),` |
| C5 | `_copy_scope_scripts` (install_hooks_lib.py:523-548) emits the 9 non-prefixed names | **VERIFIED** | `_files` dict at :530-540 = exactly the 9 basenames; copy loop :543-548 |
| C6 | `scope="global"` → `hooks_dir == global_hooks_dir == ~/.claude/hooks` | **VERIFIED** | `_resolve_scope_paths` :517-518 returns `(…, global_hooks_dir, global_hooks_dir, …)` for scope=="global" |
| C7 | hook_runner dispatches via internal `_HOOKS` dict + `argv[1]`, never execs sibling scripts | **VERIFIED** | `yadgar/core/scripts/hook_runner.py` `_HOOKS` at :274, `main()` reads `sys.argv[1]` :293, `handler = _HOOKS.get(hook_type)` :294. grep for `os.exec/subprocess/Popen/runpy/import_module` in runner → **zero hits**. Deleting siblings cannot break a wired hook. |
| C8 | `"/hooks/prompt-recall"` (runner) is an HTTP route, not a filesystem/sibling ref | **VERIFIED** | hook_runner.py:186-187 = `_http_get("/hooks/prompt-recall", …)`; `_http_get` at :43 uses urllib. Benign. |
| C9 | db-lockdown single-file unlink at install_hooks_lib.py:403-409 | **VERIFIED** | `orphan = global_hooks_dir / "yadgar-db-lockdown-check.py"; try: orphan.unlink() except OSError: pass` at :405-409 |
| C10 | The 4 append hooks copied under `yadgar-` prefix (`_install_append_hooks`) | **VERIFIED** | `_append_specs` :467-470 maps `(src.py → yadgar-src.py)`; `_copy_hook` writes prefixed dst :475 |
| C11 | stop/session-end/router copied under `yadgar-` prefix (`_install_global_scripts`) | **VERIFIED** | :374/:377/:384 = `yadgar-stop-memory-checkpoint.py` / `yadgar-session-end-capture.py` / `yadgar-pretooluse-router.py` |
| C12 | "Latent, not live" — NO current test leaks to real HOME | **VERIFIED** | e2e/conftest.py reads `Path.home()` only for real-data-dir skip logic (:32-33), drives no installer. `test_install_hooks_host_vs_container.py` patches `HOME=tmp_path` in every write test (:37/:81/:105/:129/:162), INCLUDING `test_mcp_tool_works_on_host` (:125-146) which calls the real unmocked MCP wrapper and is safe ONLY because `Path.home()` honors the patched `$HOME`. `test_cli_install_hooks_module.py` mocks `install_hooks_impl`. No airtight-path miss found. |
| C13 | `test_manifest_references_all_install_intended_scripts` needs update (#9) | **VERIFIED + UNDER-SPECIFIED** | Lives in `test_install_hooks_sweep.py:166`, scans lib source for `"*.py"/"*.sh"` string literals (:161). See Defect D2 — the fix is NOT `_IMPORTED_ONLY`; it's the loss of the sole literal for 5 core names. |
| C14 | `test_paths.py` real-home assertions stay green under redirect | **VERIFIED** | `_shared/test_paths.py:25/:42` compute expected via `Path.home()` → actual+expected track the redirect together. |

### Focus #1 — Global HOME-guard crux: **SOUND**

- The env-patch approach is correct and provably sufficient (C2). No `setattr(Path, "home", …)` needed; env-patch is strictly broader. The plan's "resolution fact for the auditor" (line 50) is accurate.
- **Does patching HOME break git-config/ssh tests?** No material risk found. Yadgar's DB/config isolation already routes through XDG_* + YADGAR_* env (conftest.py:215-226), not HOME. Tests needing real git identity run against `tmp_path` repos with explicit `git config`. If any test shells out to a tool that reads `~/.gitconfig`/`~/.ssh` for real credentials, HOME redirect would blank it — but the FULL-suite green run mandated in Test-plan step 2 is the correct gate to catch that. **User-decision: accept the full-suite run as the sufficiency proof; do not attempt to pre-enumerate HOME-sensitive tests.**
- **Sentinel catches in-place deletion?** Yes — snapshotting `(name, st_mtime_ns, st_size)` per child directly under `~/.claude/hooks/` (plan line 67) catches a child unlink/overwrite (the actual db-lockdown incident), which a bare `~/.claude` dir-mtime would miss. Design is correct.
- **`exists()` guard right for CI?** Yes — CI has no `~/.claude`, sentinel no-ops (line 69). Correct.
- **Nit (non-blocking, xdist):** the plan frames the sentinel snapshot as a single "module global." The conftest is heavily xdist-parallel; under xdist the module global is **per-worker**, so the tripwire fires per-worker. Still catches leaks correctly (any worker that mutates real HOME trips its own snapshot), but the "one global snapshot" framing is slightly off. Recommend wording the fixture as per-worker-safe (read at worker session start, assert at that worker's sessionfinish). Does not change the verdict.

### Focus #2 — "Latent not live": **RECONCILED — claim holds**

The plan's central "no live leak" claim survives audit (C12). Reconciliation of "if no test leaks, what deleted `db-lockdown` ~5×?": the deletions came from an **in-session MCP/CLI `install_hooks` invocation against real HOME** (the tool fired during dev work, resolving `home_dir=Path.home()` → real `~/.claude`), NOT from the test suite. The suite is airtight today; the guard is prevention-by-construction for the FUTURE test that forgets its HOME patch, and the sentinel is the tripwire. This is the correct framing. The two focus-#2 hypotheses (make-e2e integration test / non-mocked CLI) were both checked and cleared: e2e conftest drives no installer, and the one unmocked real-wrapper test (`test_mcp_tool_works_on_host`) is HOME-patched.

### Focus #3 — Sweep root-cause + predicate: **root-cause SOUND, predicate BROKEN (D1)**

Root-cause (stop emitting non-prefixed names for global scope) is correct: the 9 names are written to disk ONLY by `_copy_scope_scripts._files`; nothing dispatches to them (hook_runner uses `_runner_entry(...)` internal dispatch, :436-448). Removing `_copy_scope_scripts` from the global path is the real fix and is safe. **But the legacy-sweep predicate is wrong — see D1.**

---

### DEFECT D1 (BLOCKER) — sweep predicate matches only 4 of 9 orphans

Plan line 89 recommends: unlink non-prefixed `name` only when `(global_hooks_dir / f"yadgar-{name}").exists()`, calling content-hash the "stronger alternative" and sibling-existence "simpler and sufficient." **This is inverted.** Verified by grep: NO `yadgar-`-prefixed on-disk sibling is ever written for 5 of the 9 names —

```
grep 'yadgar-post-tool-capture|yadgar-session-start|yadgar-prompt-recall|yadgar-pre-compact|yadgar-post-compact' install_hooks_lib.py  →  ZERO hits
```

Those 5 (`post-tool-capture.py`, `session-start-context.py`, `prompt-recall.py`, `pre-compact-drain.sh`, `post-compact-rehydrate.sh`) are dispatched via `hook_runner.py <type>` and never copied under any prefix. Only the 4 append hooks (`subagent-stop`, `subagent-start`, `instructions-loaded`, `file-changed`) have on-disk `yadgar-`-prefixed siblings (`_install_append_hooks` :467-470). So `(…/yadgar-{name}).exists()` is **False** for the 5 core names → the legacy sweep silently leaves them behind. This **breaks acceptance #6 (idempotent → zero non-prefixed) and #7 (clean install has zero non-prefixed)** for exactly the 5 highest-traffic hook names.

**Correct predicate:** gate on **content-hash equality against the packaged source** `package_hooks / name` (which exists for all 9 — confirmed in the hooks-dir listing), i.e. "delete the non-prefixed copy iff its bytes match what WE would have shipped." That is the only provenance signal that works for all 9 AND still preserves a user's own coincidentally-named file (different content → survives). Fall back to sibling-existence ONLY for the 4 append names if a belt-and-suspenders second signal is wanted, but content-hash alone is necessary and sufficient. **Swap the plan's "recommended" and "alternative" — content-hash is the primary, not the fallback.**

**Is stopping the emission the real fix?** Yes (root-cause is sound). But the legacy sweep still must clean the CURRENT live state (all 9 present on this machine), and the plan's predicate can't clean 5 of them.

### DEFECT D2 (BLOCKER for test-validity) — acceptance #3 fabricates production-impossible state

Acceptance #3 (plan line 118) seeds "all 9 non-prefixed names AND their `yadgar-`-prefixed siblings." Production NEVER creates `yadgar-post-tool-capture.py` et al. (D1). A test that manufactures those 5 fake siblings will pass GREEN under the broken sibling-existence predicate while the real bug (5 orphans never swept in the field) ships. **#3 must seed only the state production actually produces: the 9 non-prefixed copies with NO fabricated `yadgar-` siblings for the 5 core names.** Under the correct content-hash predicate, all 9 (seeded as copies of the packaged source) get swept; a user file with the same name but different content survives (that's acceptance #4). Rewrite #3 accordingly, and make #4 explicitly cover a same-name-different-content core-hook file (not just an append-hook lookalike) to exercise the content-hash discrimination.

### DEFECT D3 (needs enumeration, Focus #4) — manifest-test fix is mis-scoped

Plan #9 says update `test_manifest_references_all_install_intended_scripts` via `_IMPORTED_ONLY` / manifest-scan expectations. Verified the real mechanism (test_install_hooks_sweep.py:151-178): it scans lib source for `"*.py"/"*.sh"` string LITERALS and asserts every shipped hooks-dir source file appears. Grep of literal counts in `install_hooks_lib.py`:

```
pre-compact-drain.sh, post-compact-rehydrate.sh, post-tool-capture.py,
session-start-context.py, prompt-recall.py         → 1 literal each (ONLY in _copy_scope_scripts._files)
subagent-stop.py, instructions-loaded.py,
subagent-start.py, file-changed.py                 → 2 literals each (_files + _append_specs)
```

If `_copy_scope_scripts._files` is removed, the **5 core names lose their sole manifest literal** → `test_manifest_references_all_install_intended_scripts` fails with `missing = {those 5}`. `_IMPORTED_ONLY` (the underscore modules) is the WRONG lever — these are hyphen source files, not underscore imports. **Enumerated fix:** either (a) exclude the 5 hook_runner-dispatched names from `install_intended` (they are dispatched by runner, not copied — analogous to `_IMPORTED_ONLY` but a distinct category, e.g. a new `_RUNNER_DISPATCHED` set), or (b) keep a manifest literal for them elsewhere (e.g. the `_runner_entry("post-tool-capture")` calls at :445/:448 embed `"post-tool-capture"`/`"prompt-recall"` WITHOUT the `.py`/`.sh` suffix, so they do NOT satisfy the current regex `\.(py|sh)`). Option (a) is cleaner. The plan MUST call out that the 4 append names keep their `_append_specs` literal and are unaffected; only the 5 runner-dispatched names need the exclusion. Update the test comment (`# _copy_scope_scripts._files dict`, :154) since that manifest list is being deleted.

### Focus #5 — nix-convergence: **SOUND**

Recommendation (retire nix `yadgar-hooks` copy → single `yadgar install-hooks --scope=global` installer; or rename nix targets to `yadgar-` + have nix own settings.json; hand via MIGRATION_NOTES) is sound and correctly scoped out-of-tree. The conservatism note (line 107: nix-only deploy WITHOUT core install present is NOT swept because the provenance guard requires the core-installed signal) is correct AND is IMPROVED by the D1 content-hash fix: a nix-deployed `session-start-context.py` whose bytes match the packaged source WOULD now be swept even without a core sibling — so under content-hash the fight surface is slightly LARGER than the plan states (any byte-identical nix copy is swept). This strengthens the case for the "retire nix copy" recommendation and MUST be noted: with content-hash, the core sweep can delete nix-deployed files if they are byte-identical to the package, so the nix coordination is not optional if nix keeps deploying identical bytes. **User-decision below.**

### Focus #6 — Version + acceptance testability: **SOUND with caveats**

Core-only, backend-unaffected — correct (all touched files are core). Acceptance criteria are testable EXCEPT #3 (D2 — fabricates impossible state) and #6/#7 (D1 — unreachable under the broken predicate). After D1/D2/D3 fixes, all acceptance criteria are testable. Version bump core-only is right.

---

### Biggest risk

Shipping the sibling-existence predicate (as the plan currently recommends it) — it looks correct, passes the fabricated acceptance #3 green, and leaves the 5 most-used hook orphans un-swept in the field forever. The green test actively hides the bug. This is the one thing that MUST change before build.

### User-decisions required

1. **D1 predicate:** adopt **content-hash-vs-packaged-source** as the PRIMARY sweep predicate (necessary for the 5 runner-dispatched orphans). Confirm.
2. **D2/D3 tests:** rewrite acceptance #3 to seed only production-real state (no fabricated `yadgar-` siblings for the 5 core names); add a `_RUNNER_DISPATCHED` exclusion set to the manifest test rather than touching `_IMPORTED_ONLY`. Confirm.
3. **nix (Focus #5):** with content-hash, the core sweep CAN delete byte-identical nix-deployed hooks even absent a core sibling. Decide: retire the nix `yadgar-hooks` copy (preferred), or accept that `home-manager switch` + core install will race on those files. Hand the chosen nix change via `MIGRATION_NOTES.md`.
4. **HOME full-suite gate:** accept the mandated full-suite green run (Test-plan step 2) as the sufficiency proof for HOME-redirect safety rather than pre-enumerating HOME-sensitive tests. Confirm.

### AUDITED-ready vs needs-rework, by part

- **Part 1 (HOME guard + sentinel): AUDITED-ready.** Env-patch sound, sentinel design correct, `exists()` CI-guard correct. Only the xdist per-worker wording nit (non-blocking).
- **Part 2 (orphan sweep): NEEDS-REWORK.** D1 (predicate), D2 (acceptance #3), D3 (manifest test) must be fixed. Root-cause (stop emitting) is sound and can proceed as-is.

Original plan preserved above; nothing edited outside this AUDIT section.
