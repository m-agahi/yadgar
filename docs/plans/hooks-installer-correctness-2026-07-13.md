# HOOKS Train — Car 3: `install_hooks` correctness for normal (non-nix) users

**Status:** DRAFT — awaiting audit
**Date:** 2026-07-13
**Author:** design agent (HOOKS train, Car 3)
**Core version at authoring:** 5.133.0 (`pyproject.toml`)
**Scope:** `yadgar/core/install/install_hooks_lib.py` + entry points; no code changed by this doc.

---

## BLUF

`install_hooks` is the **only** path by which non-nix ("normal") users receive Yadgar's
Claude Code hooks. Investigation confirmed **four defects** in the installer, all verified
against current source:

- **BUG A (HIGH)** — `_append_if_absent` dedups on the **full command string** (interpreter
  prefix included). When the interpreter *drifts* between installs (bare `python3` → absolute
  venv path, install-method change on upgrade, worktree-poison-then-heal), the four
  append-hooks accumulate **duplicate** entries. This is a **drift** bug, not a vanilla-rerun
  bug — a plain double-install with a stable interpreter already dedups correctly today.
- **BUG B (MED)** — `YADGAR_MCP_AUTH_TOKEN` is baked as a **literal** value into `settings.json`
  (`env` block). Secret-at-rest + silent auth-fail after token rotation. Fix: `${VAR}`
  indirection (Claude Code interpolates — precedent already in `setup.py:103`).
- **BUG C (LOW)** — installer bakes whatever `YADGAR_MCP_AUTH_TOKEN` holds verbatim; a known
  **test fixture** token (`a-valid-32-char-token-here!!`, `tests/server/test_security_headers.py:243`)
  reaches `settings.json` if that env is exported in the install session. Fix: guard/warn on
  known test-fixture values.
- **BUG D (LOW)** — the container-refusal message cites `POST /hooks/install-bootstrap`, an
  **endpoint that does not exist** in `http.py`. Fix: correct the message (the working
  `host_command` is already returned).

The fix set: (1) re-key dedup on the hook **script path / yadgar signature** + add an idempotent
**migration-sweep** that strips pre-existing yadgar-managed entries per event and rebuilds
(self-heals accumulated dupes + stale interpreters on every install); (2) `${VAR}` token
indirection; (3) test-token guard; (4) message correction.

**Delivery coupling:** Car 3 is the vehicle that ships HOOKS Car 1 (router.py + exceptions
config) and Car 2 (compact/restore scripts) to normal users — the installer's file manifest
must list their finalized script names. **Bugs A/B/C/D are independent of Cars 1+2 and can land
earlier; only the delivery-integration slice is gated on them (integrate LAST).**

---

## install_hooks behavior map (verified file:line)

Impl: `yadgar/core/install/install_hooks_lib.py::install_hooks_impl` (:500).
Shim: `yadgar/core/install_hooks_lib.py` (PEP-562 re-export).

### Entry points

| Caller | File:line | Default scope |
|---|---|---|
| MCP tool `install_hooks` | `yadgar/core/server/tools/misc.py:417` (`@_tool(power=True)`) | `project` |
| CLI `install-hooks` | `yadgar/core/cli/install_hooks.py:17` (`cmd_install_hooks`) | `global` |

Both call `install_hooks_impl(home_dir=Path.home(), scope, project_directory, dry_run)`.
The MCP tool additionally refuses when `is_running_in_container()` (misc.py:439) — this is
where **BUG D**'s dangling message lives (misc.py:446, :450).

### Interpreter resolution (GOOD — do not regress)

`_stable_python(existing, home_dir)` (:159) returns `sys.executable` when durable
(`_is_durable_interpreter`, :71 — rejects `.claude/worktrees/`, tmp dirs, linked git worktrees).
Non-durable → substitute chain: existing-registration-if-durable → pipx venv → canonical repo
venv → keep-existing(warn) → PATH `python3`. This is the task #38 fix and is correct; the plan
must not weaken it. Resolved **once** in `install_hooks_impl` (:544) and threaded to every copy
+ command builder.

### Hook population

- **Core (replace-always):** `_build_core_hooks` (:356) sets `PreCompact`, `SessionStart` (x2),
  `PostToolUse` (x2), `UserPromptSubmit`, `PreToolUse`. These are **overwritten** each install →
  immune to BUG A.
- **Append-if-absent:** `_install_append_hooks` (:396) registers `SubagentStop`,
  `InstructionsLoaded`, `SubagentStart`, `FileChanged` via `_append_if_absent` (:314). **These
  four are the only ones affected by BUG A.**
- **Global Stop/SessionEnd:** written to `~/.claude/settings.json` (`_write_global_stop_hooks`,
  :422) — replace-always, immune.

### Script manifest (the Car 1/Car 2 coupling surface)

- `_copy_scope_scripts._files` (:465) — 9 dispatcher scripts (incl. `pre-compact-drain.sh`,
  `post-compact-rehydrate.sh` = Car 2's compact/restore vehicles).
- `_install_global_scripts` (:335) — `stop-memory-checkpoint.py`, `session-end-capture.py`,
  `db-lockdown-check.py`.
- `_install_append_hooks._append_specs` (:406) — 4 append scripts (hyphen-named).
- `hook_runner.py` copied from `scripts/` (:560).

All script names are **hardcoded strings** in these three functions. Any Car 1/Car 2 rename
must be mirrored here or the installer copies stale / misses files.

---

## The four bugs (evidence)

### BUG A — triple-registration via command-string dedup (HIGH)

**Evidence.** `_append_if_absent` (install_hooks_lib.py:322-331):

```python
already = any(
    entry.get("hooks", [{}])[0].get("command", "") == cmd    # ← full command incl. interpreter
    for entry in existing ...
)
```

`cmd` is `f"{_python} {shlex.quote(str(dst))}"` (:418) — interpreter prefix + script path.
Dedup key = the whole string. If `_python` differs between two installs, the same script is
registered twice.

**Trigger conditions (drift — NOT vanilla rerun).** For a normal user with a stable pipx/venv
python, `_stable_python` returns a constant absolute path across runs → identical command →
correct dedup (already proven by `test_append_if_absent_deduplicates`, lib_module test:96).
Dupes require the interpreter to **change** between installs:

1. First install resolved fallback bare `python3` (chain step e), a later install resolves an
   absolute venv path (or vice versa).
2. Install method changes across an upgrade (pipx → repo venv, or path moves).
3. Worktree-poison in one session, heal in the next (the `_stable_python` substitute differs
   from the poisoned value).

**Why "no dupes on double-install" is NOT a valid acceptance test.** That passes today without
any fix for the target population. The reproducing test must **seed the target settings with a
different prior interpreter** and assert the second install collapses rather than appends.

### BUG B — token baked as literal (MED)

**Evidence.** install_hooks_lib.py:570-571:

```python
_auth_token = os.environ.get("YADGAR_MCP_AUTH_TOKEN", "")
_env_block: dict = {"YADGAR_MCP_AUTH_TOKEN": _auth_token} if _auth_token else {}
```

The literal token value lands in every hook entry's `env` block in `settings.json`. Two harms:
secret written at rest to a plaintext file; after token rotation the stale value silently
overrides ambient env → auth failures until re-install.

**Fix precedent.** `yadgar/core/cli/setup.py:103` already emits the MCP snippet with
`"Authorization": "Bearer ${YADGAR_MCP_AUTH_TOKEN}"` — Claude Code interpolates `${VAR}` from
the environment. The installer should do the same (see fix below).

### BUG C — test-fixture token contamination (LOW)

**Evidence.** The only occurrence of the fixture value is
`yadgar/tests/server/test_security_headers.py:243`:
`monkeypatch.setenv("YADGAR_MCP_AUTH_TOKEN", "a-valid-32-char-token-here!!")`. It is scoped to
the test via `monkeypatch` (does not leak in CI). Risk is narrow: if a developer exports that
value in an install shell, BUG B's verbatim baking writes it into settings. Guard is cheap
insurance, not a live incident. **Not in VCS** (`.claude` gitignored — confirmed, not a concern).

### BUG D — dangling endpoint reference (LOW)

**Evidence.** `misc.py:446` + `:450` cite `POST /hooks/install-bootstrap`. Grep of
`yadgar/core/server/http.py` route decorators (`@mcp_server.custom_route("/hooks/…")`) lists:
pre-compact, post-compact, block-reflect, auto-capture, session-context, prompt-recall,
subagent-stop, seed-anchor, seed-agent-prompts, file-changed, instructions-loaded,
subagent-start — **no `install-bootstrap`**. The refusal already returns a working
`host_command` (`yadgar install-hooks --scope=global`). `host_command_fallback` has **no
consumer** outside misc.py itself (grep confirmed) — safe to edit the message.

---

## Fixes

### Fix A1 — dedup key = script path / yadgar signature

Re-key `_append_if_absent` (and any duplicate check) on the **hook script path**, not the full
command. Reuse the existing yadgar-managed signature already defined in `_entry_interpreter`
(:224): a command is yadgar-managed iff `"hook_runner.py" in cmd or "yadgar-" in cmd`. For the
append-hooks, the stable identity is the **destination script basename** (e.g.
`yadgar-subagent-stop.py`). Dedup: "an entry whose command contains this script basename already
exists" → skip. Interpreter drift no longer defeats it.

- Preserves `test_append_if_absent_allows_different_commands` intent: different **scripts** still
  coexist.
- Changes `test_append_if_absent_deduplicates` semantics: now dedups even when the interpreter
  prefix differs (that is the fix — update the test accordingly).

### Fix A2 — migration-sweep (heals existing dupes + stale interpreter)

**Division of labor is deliberate:** Fix A1 *prevents new* dupes; the sweep *heals existing*
dupes and refreshes a stale interpreter that A1 alone would leave in place. Both are required.

On every install, for each of the four append-events: **strip all pre-existing yadgar-managed
entries** (predicate: command matches the yadgar signature — `hook_runner.py` or `yadgar-`
substring, tightened to the four managed script basenames), **then rebuild** the single correct
entry with the freshly resolved durable interpreter. Idempotent: a clean settings file →
identical output; a poisoned/duplicated file → collapsed to one entry with the current
interpreter.

**Over-deletion mitigation (the task's stated risk).** The strip predicate must be the
**tightest available** — match only the four yadgar-managed script basenames (or the
`yadgar-`/`hook_runner.py` signature), never a foreign entry a user added to a shared event
(e.g. a user's own `SubagentStop` hook). Reusing the in-file `_entry_interpreter` predicate keeps
this consistent and conservative.

**Marker approach — OPEN QUESTION, not baseline.** The task suggests injecting a
`version/marker` key into the entry dict. Deferred: Claude Code's hook schema may reject or strip
unknown keys in a hook entry. **Baseline = command-signature detection** (already proven by
`_entry_interpreter`). Injected-marker listed under Open Questions pending schema confirmation.

### Fix B — token via `${VAR}` indirection

Replace the literal env block with indirection so Claude Code interpolates at hook-fire time:

```python
_env_block = {"YADGAR_MCP_AUTH_TOKEN": "${YADGAR_MCP_AUTH_TOKEN}"} if _auth_token else {}
```

(Or omit the env block entirely and rely on ambient env — decide during impl; `${VAR}` mirrors
`setup.py:103` and is the safer default because it makes the dependency explicit.) Removes
secret-at-rest and the stale-token silent-failure. The presence check still keys on whether a
token exists in the install environment, but the **written value** is the indirection literal.

### Fix C — test-fixture guard

Before writing the env block, compare the token against a small deny-set of known test fixtures
(currently `a-valid-32-char-token-here!!`). On match: **do not bake it**, log a warning
("refusing to register a known test-fixture auth token — check YADGAR_MCP_AUTH_TOKEN"). Combined
with Fix B, this is belt-and-suspenders; keep it because Fix B still keys presence off the env
var.

### Fix D — correct the container-refusal message

Drop the `POST /hooks/install-bootstrap` clauses (misc.py:446, :450). Keep the working
`host_command` (`yadgar install-hooks --scope=global`). Set `host_command_fallback` to the plain
instruction to run the CLI on the host, or remove the key (no external consumer). **Do NOT**
implement a new `/hooks/install-bootstrap` endpoint — higher cost, unneeded; listed as the
rejected alternative.

---

## Car 1 + Car 2 delivery coupling (build-last?)

**Verdict: the delivery-integration slice of Car 3 integrates LAST; bugs A–D do not.**

- **Car 1 (router.py + exceptions config)** — `router.py` does **not exist** in the repo yet
  (`find yadgar -name router.py -path '*hook*'` empty; no `router` reference in
  `yadgar/core/hooks/` or `hook_runner.py`). Car 3 cannot copy/register a file that Car 1 has not
  produced. → Car 3's manifest addition for router.py is gated on Car 1 landing.
- **Car 2 (compact/restore scripts)** — `pre-compact-drain.sh` + `post-compact-rehydrate.sh`
  already present in `yadgar/core/hooks/` and already in `_copy_scope_scripts._files` (:465). If
  Car 2 rewrites/renames them, the manifest string must follow.

**Concrete coupling = the file manifest**, three hardcoded lists: `_copy_scope_scripts._files`
(:465), `_install_global_scripts` (:335), `_install_append_hooks._append_specs` (:406). Car 3's
delivery work is: extend these lists to include Car 1's router + config and confirm Car 2's final
script names, then register the new hook events.

**Sequencing recommendation.** Split Car 3 into two mergeable slices:

1. **Correctness slice (A/B/C/D)** — zero dependency on Cars 1+2. Land early, independently.
2. **Delivery slice** (manifest + registration for Car 1's router/exceptions + Car 2's finalized
   scripts) — integrate **after** Cars 1+2 finalize their script contents/names. This is the
   "build-last" part.

Do not gate the correctness fixes behind Cars 1+2.

---

## nix-vs-installer consistency

The user manages his own hooks via a **separate nix repo** (`modules/home/…`, out of this
repo's tree). Normal users get **only** `install_hooks` output. The two are **parallel installers
of the same underlying hook scripts** → divergence risk (a script added/renamed here won't
appear in nix, and vice versa).

**Recommendation.** Treat nix parity as **OUT OF SCOPE for this car** (different repo, user-owned).
But flag the divergence explicitly and recommend one guard that lives *in this repo*: a test/CI
check that the installer's three hardcoded manifest lists match the actual set of shipped script
files under `yadgar/core/hooks/` + `yadgar/core/scripts/` (catches "script added, manifest not
updated" — the failure mode that also silently breaks nix parity). The nix side must be updated
by the user in his repo; this car ensures the installer output is **complete + correct
standalone**.

---

## Idempotency / upgrade design

Post-fix invariants:

- **Re-run with identical interpreter** → byte-identical settings (no-op).
- **Re-run after interpreter drift** → append-hooks collapse to one entry each, interpreter
  refreshed to current durable value (sweep + rebuild).
- **Upgrade from a poisoned/duplicated pre-fix settings file** → first post-fix install self-heals
  (sweep strips the accumulated yadgar entries, rebuild writes exactly one per event).
- **Foreign user hooks in shared events** (e.g. user's own `SubagentStop`) → **preserved** (tight
  strip predicate).
- Core hooks + global Stop/SessionEnd remain replace-always (unchanged).

---

## Acceptance criteria

**[unit]**

1. **Drift dedup:** seed target settings with a `SubagentStop` yadgar entry whose command uses
   interpreter `python3` (bare); run install with a *different* absolute interpreter; assert the
   event has **exactly one** yadgar entry and its command carries the new interpreter.
   (Naive "install twice, same interpreter → no dupes" also kept, but is NOT sufficient on its
   own — it passes pre-fix.)
2. **Sweep heals dupes:** seed settings with **two** duplicate yadgar `SubagentStop` entries; run
   install; assert exactly one remains.
3. **Foreign hook preserved:** seed a non-yadgar `SubagentStop` entry; run install; assert it
   survives alongside exactly one yadgar entry.
4. **Token as `${VAR}`:** with `YADGAR_MCP_AUTH_TOKEN` set to a real-looking value, assert every
   written `env` block value is the literal `"${YADGAR_MCP_AUTH_TOKEN}"`, never the raw token.
5. **Test-token guard fires:** with `YADGAR_MCP_AUTH_TOKEN=a-valid-32-char-token-here!!`, assert
   no env block carries that literal and a warning is logged.
6. **BUG D message:** assert the container-refusal detail string contains no
   `install-bootstrap` substring and still exposes a runnable `host_command`.
7. **Interpreter durability not regressed:** existing `test_install_hooks_stable_python` +
   `test_install_hooks_shebang` still green.

**[integration / manifest]**

8. Manifest-completeness check: every `*.py`/`*.sh` hook script shipped under
   `yadgar/core/hooks/` intended for install is referenced by exactly one manifest list (guards
   Car 1/Car 2 rename drift).

---

## Test plan

Existing suites to extend (do not duplicate):

- `yadgar/tests/hooks/test_install_hooks_lib_module.py` — the `_append_if_absent` tests
  (`:89–:120`) get re-pointed to script-path dedup; add drift + sweep + foreign-preserve cases.
- `yadgar/tests/hooks/test_install_hooks_injection.py` — add token `${VAR}` + test-token-guard
  assertions.
- `yadgar/tests/hooks/test_install_hooks_host_vs_container.py` — add BUG D message assertion.
- `yadgar/tests/hooks/test_install_hooks_stable_python.py` / `..._shebang.py` — regression guard
  (unchanged expectations).
- New manifest-completeness test (small, in `tests/hooks/`).

TDD order per the repo's Test-Driven rule: write each failing assertion first (red), then the
minimal impl change (green). Run the hooks + scripts test subset to clean, then the full suite.

---

## Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| **Sweep over-deletes** foreign user hooks in shared events | Med | Tightest predicate — match only the four yadgar-managed script basenames / `yadgar-`+`hook_runner.py` signature (reuse `_entry_interpreter`). Acceptance test #3 pins it. |
| **Breaking existing installs** — settings shape change | Low | Sweep+rebuild produces the same shape; only collapses dupes + refreshes interpreter. Idempotency tests pin no-op on clean input. |
| **Injected marker rejected by Claude Code schema** | Med (if pursued) | Not baseline — command-signature detection used instead; marker deferred to open question. |
| **Car 1/Car 2 rename desync** (manifest drift) | Med | Manifest-completeness test (#8); delivery slice integrates last, after their names finalize. |
| **`${VAR}` not interpolated in `env` block context** | Low | Precedent is the MCP `headers` block (`setup.py:103`); verify Claude Code interpolates `${VAR}` inside hook `env` specifically during impl (open question). Fallback: omit env block, rely on ambient env. |
| **Orphan underscore hook files** (`subagent_stop.py` etc.) copied by a future manifest edit | Low | See open question — classify/remove; currently unreferenced by installer or imports. |

---

## Scope

**IN**

- Fix BUG A (dedup key + migration-sweep), BUG B (`${VAR}`), BUG C (guard), BUG D (message).
- Re-point/extend the affected install_hooks tests.
- Manifest-completeness guard test.
- Delivery-integration hooks for Car 1 (router + exceptions) and Car 2 (compact/restore) — the
  wiring, gated on those cars landing.

**OUT**

- The nix installer (separate user-owned repo). Flag divergence; recommend the in-repo manifest
  guard only.
- Implementing a new `/hooks/install-bootstrap` endpoint (rejected in favor of message
  correction).
- Any change to `_stable_python` durability logic (task #38 — keep as-is).
- Authoring Car 1's `router.py`/exceptions config or Car 2's script bodies (those are the sibling
  cars; Car 3 only *delivers* them).

---

## Version impact

Core package (`pyproject.toml` currently **5.133.0**). Correctness slice (A–D) = a minor bump
(bug fixes, one behavior change in append-hook dedup semantics). Delivery slice sequences **after**
Cars 1+2 (and after the referenced #195 gate). Backend untouched. No migration to the DB —
`settings.json` is regenerated in place by the sweep.

---

## Open questions

1. **Marker key in hook entries** — does Claude Code's hook schema tolerate an extra
   `_yadgar_managed`/version key in an entry dict, or strip/reject it? If tolerated, it is a more
   robust sweep predicate than substring matching. Verify before adopting; baseline stays
   signature-based.
2. **`${VAR}` inside a hook `env` block** — confirmed for MCP `headers` (`setup.py:103`); confirm
   Claude Code performs the same interpolation for hook-command `env` values. If not, fall back to
   omitting the env block (ambient env).
3. **Orphan underscore hook files** — `yadgar/core/hooks/` contains both hyphen and underscore
   variants (`subagent-stop.py`/`subagent_stop.py`, `file-changed.py`/`file_changed.py`,
   `instructions-loaded.py`/`instructions_loaded.py`, `subagent-start.py`/`subagent_start.py`;
   underscore versions newer + larger, look like an in-flight T2 rename). The installer manifest
   references **hyphen** names only; underscore files are unreferenced by the installer and not
   imported as modules (grep confirmed). Are they dead artifacts to remove, or the intended
   post-rename targets Car 1/2 will switch to? Resolve before the delivery slice, else the
   installer may copy stale files.
4. **Token env-block omission vs `${VAR}`** — final call between the two Fix B variants (explicit
   indirection vs rely-on-ambient). Lean `${VAR}` for explicitness.
5. **`host_command_fallback` key** — correct its text or drop it entirely (no external consumer)?

---

## AUDIT (2026-07-13)

Adversarial audit. Method: every file:line claim verified against current source
(`install_hooks_lib.py`, `misc.py`, `setup.py`, `http.py`, tests). Read-only; no code changed.
Yadgar `recall` unavailable this session (bare `recall` is deferred; real tool is
`mcp__yadgar__recall`, not loaded) — source verification IS the authoritative check for a source
audit, so the observed-state-wins contract is satisfied without it.

### Verdict

**Status: APPROVE WITH REQUIRED FIXES (2 must-fix, both in acceptance criteria — not the impl plan).**
The engineering diagnosis is correct on every load-bearing claim. The drift-reframe (BUG A's crux)
holds against source. Two acceptance tests as written repeat the exact "green-lights-without-a-fix"
hole the plan itself identifies for BUG A — they must be tightened or the plan can ship "done" with
the over-deletion bug live. One internal predicate ambiguity in Fix A2 must be resolved down to the
tight form before impl.

### Per-claim verification table

| # | Claim | Verdict | Evidence |
|---|---|---|---|
| A | Dedup key = full command string (interpreter incl.) | **VERIFIED** | `install_hooks_lib.py:325` — `entry.get("hooks",[{}])[0].get("command","") == cmd`; `cmd` built `:418` as `f"{_python} {shlex.quote(str(dst))}"` |
| A | Vanilla double-install w/ stable interp dedups fine; only interp DRIFT dupes | **VERIFIED** | Existing `test_append_if_absent_deduplicates` (lib_module:96-100) passes because both calls send the *identical* bare string `"myscript.py"`. No existing test seeds a differing prior interpreter (confirmed: only `#!/usr/bin/env python3` shebang strings at :241/:251/:262, none in an append-command). The reframe is sound. |
| A | Fix A1 re-key on script basename preserves `allows_different_commands` | **VERIFIED** | `test_append_if_absent_allows_different_commands` (:103-107) uses `script_a.py`/`script_b.py` — different basenames still coexist under a basename key. Safe. |
| A2 | Sweep predicate reuses `_entry_interpreter` signature | **VERIFIED (but see Finding 1)** | `_entry_interpreter:224` = `"hook_runner.py" not in cmd and "yadgar-" not in cmd → continue`. Signature exists as claimed. |
| B | Token baked literal into env block | **VERIFIED** | `:570-571` — `_env_block = {"YADGAR_MCP_AUTH_TOKEN": _auth_token}`. Raw value. |
| B | `setup.py:103` is the `${VAR}` precedent | **VERIFIED — but precedent is MCP `headers`, NOT hook `env`** | `:103` = `"headers": {"Authorization": "Bearer ${YADGAR_MCP_AUTH_TOKEN}"}`. Different config surface. Plan already flags this gap (Risk table + OQ#2). Honest. Non-blocking (see Finding 3). |
| C | Fixture token only in `test_security_headers.py:243`, `monkeypatch`-scoped | **VERIFIED** | Single grep hit at `:243`; `monkeypatch.setenv(...)`, no CI leak. |
| D | `misc.py:446` + `:450` cite non-existent `/hooks/install-bootstrap` | **VERIFIED** | Both lines confirmed. `http.py` has 12 `/hooks/*` routes, none `install-bootstrap`. |
| D | Plan's http.py route enumeration is complete | **VERIFIED** | Plan lines 156-160 list all 12 routes; matches grep 12/12 (pre-compact, post-compact, block-reflect, auto-capture, session-context, prompt-recall, subagent-stop, seed-anchor, seed-agent-prompts, file-changed, instructions-loaded, subagent-start). No omission. |
| D | `host_command_fallback` has no external consumer | **VERIFIED** | Grep: `host_command_fallback` appears only in `misc.py`. Note: the fallback's *value itself* (`:450`) is a second dangling `install-bootstrap` reference — the whole key is a dead pointer, stronger case to drop it. |
| Delivery | `router.py` does not exist | **VERIFIED** | `find … -name router.py -path '*hook*'` empty; no `router` ref in `core/hooks/`. |
| Delivery | Car 2 scripts present + in manifest | **VERIFIED** | `pre-compact-drain.sh`, `post-compact-rehydrate.sh` in `_copy_scope_scripts._files` (dict at ~:472). |
| Manifest | 3 hardcoded lists at :465/:335/:406 | **VERIFIED (line nit)** | `_copy_scope_scripts` dict actually begins ~:472 (fn def :459); `_install_global_scripts:335` and `_append_specs:406` exact. Off-by-~7 on the first cite only. |
| Orphans | underscore variants newer/larger, unreferenced | **VERIFIED** | `file_changed.py`/`subagent_stop.py` etc. dated Jul 11, larger; installer references hyphen names only. |
| Version | core 5.133.0 | **VERIFIED (stale target note)** | `pyproject.toml` = `5.133.0`. Audit-foci "post-#195 5.134" is a forward target, not current — harmless. |

### Findings (must-fix + notes)

**Finding 1 — MUST FIX (acceptance test #3 does not discriminate the over-deletion mitigation).**
Same class of hole the plan nails for BUG A. Test #3 as written ("seed a *non-yadgar* SubagentStop
entry") uses a foreign command like `python3 /home/user/myhook.py` — that survives under BOTH the
safe basename predicate AND the unsafe loose `"yadgar-" in cmd` predicate, so it passes either way
and pins nothing. To actually prove the sweep won't over-delete, #3 MUST seed a foreign hook whose
command *contains the `yadgar-` substring but is not one of the four managed basenames* — e.g.
`python3 /opt/yadgar-extras/custom.py`. That entry is deleted by the loose predicate and preserved
by basename-scoping → it is the only seed that discriminates. Without this change the plan can ship
"acceptance met" with the over-deletion bug live. This is the audit's highest-value finding.

**Finding 2 — MUST FIX (Fix A2 predicate is internally ambiguous; the loose half is the
over-deletion vector).** Fix A2 (plan :188, :196) offers TWO predicates as if interchangeable:
(a) `hook_runner.py`/`yadgar-` substring, and (b) "tightened to the four managed script basenames."
They are NOT equivalent. The `yadgar-` substring matches any foreign hook whose command path merely
contains `yadgar-` (the `/opt/yadgar-extras/` case above). `_entry_interpreter:224` uses the loose
form for *interpreter detection* (read-only) where it is safe; as a *deletion* predicate it is not.
Direct the impl: **sweep strip key = exactly the four managed destination basenames**
(`yadgar-subagent-stop.py`, `yadgar-instructions-loaded.py`, `yadgar-subagent-start.py`,
`yadgar-file-changed.py`). Do not reuse the loose substring for deletion. (Reusing `_entry_interpreter`
for interpreter *refresh* is still fine — only the delete predicate must be basename-scoped.)

**Finding 3 — NOTE (`${VAR}`-in-env unverified, non-blocking).** Confirmed `setup.py:103` precedent
is the MCP `headers` block, not a hook `env` block — so the interpolation claim for hook `env` is
genuinely unverified against source. BUT: the verdict does not change if interpolation turns out
false — Fix B has a sound fallback (omit the env block, rely on ambient env; ambient token is how
the daemon already authenticates). Plan is honest about the gap (Risk row + OQ#2). Mark
**unverified, non-blocking, fallback adequate**. A doc check adds nothing to the verdict; do not gate
on it. Recommend: default to the omit-env-block variant unless a live Claude Code test confirms
`${VAR}` interpolation in hook `env`, since omit-env is provably correct today and `${VAR}` is not.

**Finding 4 — NOTE (double-copy of append scripts, pre-existing, not introduced by this plan).**
`_copy_scope_scripts._files` copies `subagent-stop.py` etc. into `hooks_dir` under their hyphen
names, AND `_install_append_hooks` copies the same sources into `hooks_dir` under `yadgar-`-prefixed
names. Each append script lands twice under two filenames. Harmless (the hyphen copies are unused by
any registered hook command — only the `yadgar-` copies are referenced at :418), but it is latent
cruft the manifest-completeness test (#8) should be written NOT to trip over, and a candidate for the
same cleanup pass as the orphan underscore files. Do not expand scope for it; just don't let test #8
flag the unreferenced hyphen copies as "shipped but unmanifested."

**Finding 5 — NOTE (BUG D fix should drop, not reword, the fallback key).** Since
`host_command_fallback`'s entire value is a second `install-bootstrap` dead pointer and it has no
consumer, dropping the key is cleaner than rewording it. Plan already lists this as OQ#5 — resolve
toward *drop*.

### Answers to the audit foci

1. **Drift-reframe correct?** YES. The dedup-on-full-command-string diagnosis is verified at :325,
   and no existing test seeds a differing prior interpreter, so the naive "install twice → no dupes"
   acceptance test genuinely green-lights pre-fix. The plan's insistence on a differing-interpreter
   seed (acceptance #1) is the right call. Fix A1 (basename key) is correct and breaks no legit case.
2. **Migration-sweep over-delete risk?** REAL but mitigable — gated on Findings 1 + 2. As currently
   drafted the predicate is ambiguous and test #3 doesn't catch the loose branch. With both fixes,
   risk drops to Low.
3. **BUGs B/C/D?** B verified (literal token, :570-571); C verified (single monkeypatch fixture);
   D verified (two dangling refs, no route, no consumer). `${VAR}`-in-hook-`env` unverified but
   non-blocking (Finding 3).
4. **Delivery coupling / Car3-last?** Correct. `router.py` absent → cannot manifest it; Car 2
   scripts present + manifested. Splitting correctness (A–D, land early) from delivery (integrate
   last) is sound. The three manifest lists are real and correctly located (one line-nit).
5. **Orphan files scoping compatible?** YES. Underscore files are unreferenced by installer +
   unimported; keeping their classify/remove as an OPEN QUESTION (not baseline) is correct. Removal
   fits equally in this car or #19 dead-code — plan does not over-commit. Compatible either way.
6. **Version + acceptance testable?** 5.133.0 confirmed. Acceptance criteria are testable AFTER
   Finding 1 tightens #3. #1 (drift dedup), #2 (sweep heals), #4 (`${VAR}` written), #5 (test-token
   guard), #6 (no install-bootstrap substring), #7 (durability regression) are all concrete and
   discriminating as written.

### User decisions required

1. **Sweep delete predicate (Finding 2)** — confirm: strip key = the four managed basenames ONLY,
   not the loose `yadgar-`/`hook_runner.py` substring. (Recommended: YES.)
2. **Acceptance test #3 seed (Finding 1)** — confirm the foreign-hook seed must contain a `yadgar-`
   substring in a non-managed path so it discriminates the loose-vs-tight predicate. (Recommended: YES.)
3. **Fix B variant (Finding 3 / OQ#2, OQ#4)** — default to omit-env-block (provably correct today)
   or ship `${VAR}` pending a live Claude Code interpolation test? (Recommended: omit-env unless
   interpolation confirmed.)
4. **`host_command_fallback` (Finding 5 / OQ#5)** — drop the key vs reword. (Recommended: drop.)
5. **Hyphen/underscore + double-copy cleanup (Finding 4 / OQ#3)** — this car or defer to #19
   dead-code? (Either; ensure manifest test #8 tolerates the unreferenced hyphen copies.)
