# PreToolUse Router-Guard (HOOKS train, Car 1)

**Status: DRAFT — awaiting audit**
**Date:** 2026-07-13
**Scope:** core-only. Sequence after #195 (v5.133.0) → target **v5.134.0**.
**Author:** design agent (Opus). Observed-state-verified against actual hook files + settings.json on 2026-07-13.

---

## BLUF

Replace the single-purpose `db-lockdown-check.py` PreToolUse hook with **one router
script** (`pretooluse-router.py`) that reads the tool-call JSON on stdin, dispatches on
`tool_name` to per-tool guard functions, and returns an allow/deny decision. Turns four
prose HARD RULES into mechanical, subagent-proof blocks:

1. `git commit --no-verify` / `--no-gpg-sign` / `-c commit.gpgsign=false` → **deny**
2. `terraform`/`tofu`/`tfp` invocation (+ `docker run …terraform`, `nix run …#terraform`,
   `digger …` in `gh pr comment`/`gh api` bodies) → **deny**
3. `git push` to the repo default branch → **deny**, with a JSON repo-allowlist
   (seeded `nix`, `ledger`, `ostad`)
4. **Subsumes** the existing SurrealDB docker-exec lockdown (`docker exec yadgar-db|backend`)

**Guard count: 4** (3 user-approved + 1 subsumed). **Router fails OPEN** — a bug must never
brick every Bash call; only a positively-matched dangerous pattern denies.

**Installer coupling verdict: Car 1 CAN merge before Car 3.** Car 1 carries the minimal
PreToolUse-entry swap in `install_hooks_lib.py` itself (two anchor sites, below). Car 3
(installer-correctness) owns installer robustness broadly; it is not a blocker for Car 1.

---

## Verified hook-mechanics (source of truth: the working db-lockdown script + tests)

All facts below verified against files on disk, not memory:

### Existing db-lockdown pattern (the proven precedent to subsume)

- **Script:** `yadgar/core/hooks/db-lockdown-check.py` (84 lines). Installed as
  `~/.claude/hooks/yadgar-db-lockdown-check.py` (global).
- **Output schema (WORKS — line-verified):**
  - Allow: `db-lockdown-check.py:32-38` →
    `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}`
  - Deny: `db-lockdown-check.py:41-48` →
    `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"},
    "systemMessage": "<reason>"}`
  - **`hookEventName` is REQUIRED** in `hookSpecificOutput` (2026 Claude Code schema; every
    test asserts it — `test_hook_db_lockdown_check_unit.py:39,57,97,156,196`).
- **Decision channel:** stdout JSON (NOT exit code). Script exits 0 and prints one JSON
  object. Confirmed by tests capturing `print` output and parsing JSON.
- **Stdin fields:** `tool_input` (dict) → `tool_input.command` (Bash command string);
  `tool_name` present (`db-lockdown-check.py:60-63`, test `:105`). Payload also carries
  `agent_id`/`agent_type` when fired inside a subagent.
- **Match style:** naive substring `pattern in cmd` (`db-lockdown-check.py:65-66`). SAFE for
  container names (`docker exec yadgar-db`); **NOT safe** for `terraform`/`git` tokens — see
  match-rules section (false-positive hazard).
- **Fail-soft:** malformed stdin → allow (`db-lockdown-check.py:56-58`). Router keeps this.
- **Subagent fire:** PreToolUse fires inside subagents; payload carries agent info → the
  router's blocks catch delegated (subagent) violations, not just main-thread ones. This is
  the whole reason a mechanical hook beats prose rules (prose resets per subagent spawn).
- **No soft nudge:** PreToolUse supports only `permissionDecision` ∈ {allow, deny, ask} —
  there is NO `additionalContext`. Every guard is a hard allow/deny.

### Deny-payload field resolution (schema-verification gate)

The task's verified-findings mention `permissionDecisionReason`; the working db-lockdown code
uses top-level `systemMessage`. These are DIFFERENT fields with different semantics:

- `systemMessage` (top-level) — surfaced to the human in the UI.
- `permissionDecisionReason` (inside `hookSpecificOutput`) — the reason fed back to the
  **agent/model** so it stops retrying the blocked action.

Per official docs (OQ-1 resolved, below), `permissionDecisionReason` is the canonical reason
field ("Explanation shown to user when denying"). The docs do not list a top-level
`systemMessage`, but db-lockdown demonstrably uses it and its tests assert it. Observed state
wins → the router deny helper emits BOTH (belt-and-suspenders; extra keys ignored):

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "<agent-facing reason>"
  },
  "systemMessage": "<human-facing reason>"
}
```

> **OQ-1 RESOLVED (claude-code-guide, official docs
> https://code.claude.com/docs/en/hooks.md, 2026-07-13):**
> - `permissionDecisionReason` IS a valid `hookSpecificOutput` key — "Explanation shown to
>   user when denying." So it is BOTH the canonical human- and agent-facing reason field
>   (docs describe a single location, inside `hookSpecificOutput`).
> - The docs do NOT list a top-level `systemMessage` field. **However, observed state wins:**
>   the shipping `db-lockdown-check.py` emits top-level `systemMessage` (line 47) and its
>   tests assert it (`test_hook_db_lockdown_check_unit.py:60-69,147-151`) — it demonstrably
>   works. Conclusion: emit **both** — `permissionDecisionReason` (docs-canonical) AND
>   top-level `systemMessage` (proven-working belt-and-suspenders). Extra keys are ignored by
>   Claude Code, so carrying both is safe and future-proof.
> - Bonus fact: `permissionDecision` also accepts `"defer"` (4th value), and PreToolUse stdin
>   in a subagent carries `agent_id` + `agent_type` (confirms subagent-fire). Exit code 2 is a
>   non-blocking stderr error, NOT a block — the JSON `permissionDecision` is the only
>   allow/deny control. Router uses exit-0 + JSON (matches db-lockdown).

---

## Router design

One PreToolUse settings entry, matcher `"Bash"` (unchanged from db-lockdown — all four guards
are Bash-command-string guards; do NOT broaden the matcher to tools we have no guard for).

**File:** `yadgar/core/hooks/pretooluse-router.py`, installed as
`~/.claude/hooks/yadgar-pretooluse-router.py` (global, standalone — same install path class as
db-lockdown, NOT routed through `hook_runner.py`; keeps the deny path dependency-free and
crash-isolated).

### Control flow

```
main():
  try:
    data = json.load(stdin)          # malformed → _allow()  (fail-soft)
    tool_name = data.get("tool_name")
    if tool_name != "Bash":          # early-exit fast path for non-guarded tools
        return _allow()
    cmd = data["tool_input"]["command"]  # missing/non-dict → _allow()
    for guard in _GUARDS:            # ordered; first deny wins
        decision = guard(cmd, ctx)   # ctx = {cwd, config}
        if decision is DENY:
            return _deny(reason)
    return _allow()
  except Exception:                  # ANY router bug → _allow() + stderr log
    log_to_stderr(...)               # never block work on router error
    return _allow()
```

- **Early-exit:** non-`Bash` tools return allow immediately — zero guard cost.
- **Config-driven:** guards + allowlists loaded from one JSON config (below); absent/corrupt
  config → guards run with built-in defaults (still fail-open on the git-subprocess guard).
- **Guard registry** `_GUARDS = (guard_git_commit_flags, guard_terraform_family,
  guard_git_push_default, guard_db_lockdown)` — ordered list of pure functions
  `(cmd: str, ctx: dict) -> Decision`.

### Command tokenization (critical — do NOT inherit db-lockdown substring matching)

db-lockdown's `pattern in cmd` would false-positive on `git commit -m "fix terraform bug"`.
The router tokenizes:

1. `shlex.split(cmd)` (fail-soft: on `ValueError`, fall back to whitespace split, still guard).
2. Segment the token list on shell separators `&&`, `||`, `;`, `|` → list of sub-commands.
3. For each sub-command, inspect `argv[0]` (the invoked program) and its flags — NOT arbitrary
   substrings. e.g. terraform-family checks `argv[0] in {terraform, tofu, tfp}`; commit-flags
   checks `argv[0..1] == [git, commit]` AND a forbidden flag token present.

This is what makes the false-positive guard (`git commit -m "...terraform..."` → allow) hold.

---

## Per-guard match rules + carve-outs

### G1 — git commit hook-bypass flags (HARD RULE: No Hook Bypass)

- **Match:** a sub-command whose `argv[0]=="git"` and `argv[1]=="commit"` AND any of these
  tokens present: `--no-verify`, `-n` (git commit's short `--no-verify`), `--no-gpg-sign`,
  or a `-c` pair `commit.gpgsign=false` / `commit.gpgsign=0` (also `-c` before the subcommand:
  `git -c commit.gpgsign=false commit`).
- **Deny.** High-frequency violation; blast = silent skip of sign/verify hooks.
- **Carve-out:** none. `-n` ambiguity: only treat `-n` as `--no-verify` when `argv[1]==commit`
  (git commit's `-n` = `--no-verify`; harmless elsewhere, and we only inspect `git commit`).

### G2 — terraform family (HARD RULE: No Terraform)

- **Match (deny) — any sub-command where `argv[0]` (basename) ∈ {`terraform`, `tofu`, `tfp`}**,
  ANY subcommand (apply/plan/init/validate/fmt/state/… — the rule covers outcomes).
- **Also deny:**
  - `docker run …` / `podman run …` whose image token contains `terraform` or `hashicorp/terraform`
    (spawning a fresh terraform container).
  - `nix run …#terraform` / `nix shell -p terraform …` / `nix run nixpkgs#terraform`
    (token match on a `terraform`/`opentofu` flake attr).
  - `gh pr comment …` / `gh api …` whose body/argument contains a `digger ` command
    (`digger apply|plan|unlock|lock|destroy|…`) — posting it triggers terraform via the
    Digger orchestrator. Match on `argv[0]=="gh"` AND (`pr comment`|`api`) AND `digger` token
    in the remaining args. Forbidden absolutely.
- **CARVE-OUT (commit-time exemption) — AUTOMATIC, by design:** a plain `git commit` that
  triggers `terraform_fmt` / `terraform_validate` / `terraform_tflint` / `terraform_docs`
  pre-commit hooks runs terraform as an INVISIBLE subprocess of git. The top-level Bash
  command string the hook sees is just `git commit …` — `argv[0]=="git"`, not `terraform`.
  So G2 never matches it. **No special-casing needed**; document that this is why the carve-out
  is free. (Corollary: the router intentionally does NOT try to inspect pre-commit config —
  doing so would be both fragile and out of scope.)

### G3 — git push to default branch (HARD RULE: Branch First), with allowlist

- **Match candidate:** sub-command where `argv[0]=="git"` and `argv[1]=="push"`.
- **Resolve default branch:** `git symbolic-ref refs/remotes/origin/HEAD` (in `ctx.cwd`),
  strip to short name (e.g. `master`/`main`). Fail-soft: if resolution fails (no origin,
  detached, error) → **allow** (cannot prove it targets default; fail-open — see below).
- **Determine push target:** parse the `git push` args for an explicit `<remote> <refspec>`.
  Deny when the push writes the default branch:
  - explicit `git push origin master` (default resolved = master) → deny
  - `git push` / `git push origin` with current branch == default (HEAD on default) → deny
  - `git push origin HEAD:master` / `…:refs/heads/master` → deny
  - `git push --force`/`-f` to default → deny (already covered; force is worse but same target)
  - a push to a NON-default branch (`git push origin feature/x`) → allow
- **Allowlist carve-out:** repos in the config `push_default_allowlist` may push default
  directly. Seeded `["nix", "ledger", "ostad"]` per CLAUDE.md. **Repo key = basename of the
  repo root** (git toplevel `git rev-parse --show-toplevel`, basename). Basename chosen for
  simplicity (matches how the rule names them); documented collision risk: two unrelated repos
  both named `nix` would both be allowlisted — acceptable given the tiny curated list; escalate
  to full-path keys only if a collision surfaces (OQ-2).
- **This is the ONLY guard that shells out** (`git symbolic-ref`, `git rev-parse`). It fires
  ONLY when the sub-command is `git push` — never on every Bash call. Perf + failure surface
  bounded to push commands.

### G4 — SurrealDB docker-exec lockdown (SUBSUMED from db-lockdown)

- **Match (deny):** command contains `docker exec yadgar-backend` or `docker exec yadgar-db`
  (also `podman exec …`). Substring match acceptable here (container names are specific, no
  false-positive risk) — carries the proven db-lockdown behavior verbatim.
- Same deny message as today: "Direct docker exec into yadgar DB/backend containers is blocked
  … Use yadgar MCP tools instead."

---

## Exceptions-config schema

**One JSON file, all allowlists/carve-outs.**

- **Location:** `~/.claude/yadgar-hook-exceptions.json` (global; alongside the global hook
  scripts). Rationale: guards are installed globally, so exceptions are global too. (A future
  Car may add per-project override; OUT of scope here.)
- **Schema:**

```json
{
  "version": 1,
  "push_default_allowlist": ["nix", "ledger", "ostad"],
  "disabled_guards": []
}
```

- `push_default_allowlist` — list of repo basenames allowed to push their default branch.
- `disabled_guards` — optional escape hatch: guard ids (`git_commit_flags`,
  `terraform_family`, `git_push_default`, `db_lockdown`) to disable. Empty by default. Lets a
  user turn off a mis-firing guard without uninstalling.
- **Editing UX (normal users):** open the JSON, add a repo basename to
  `push_default_allowlist` (add-exception) or remove it (remove-exception). No tool needed;
  documented in the deny message ("to allow this repo, add its name to
  `~/.claude/yadgar-hook-exceptions.json` → push_default_allowlist"). The deny reason string
  itself teaches the fix.
- **Install behavior:** **create-if-absent, NEVER clobber.** The router SCRIPT is
  replace-always (like db-lockdown); the CONFIG is seeded only when the file does not exist
  (mirrors `_append_if_absent` semantics) so reinstall preserves user-added exceptions. This
  is a hard requirement — clobbering would wipe the user's allowlist on every reinstall.

---

## Fail-open / perf

- **Fail-OPEN on router error** — any exception in `main()` (bad config, guard bug, unexpected
  payload) logs to stderr and returns `allow`. A router bug must not brick every Bash call.
- **Reconciliation with "catastrophic terraform blast":** fail-open does NOT weaken the
  high-blast guards. G1/G2/G4 are **pure string/token ops that cannot realistically throw** —
  fail-open never exposes them. The only guard with a real failure mode is G3 (it shells out
  to `git symbolic-ref`/`git rev-parse`), and its fail-open blast is **recoverable** (a
  direct push to default the user can revert), not catastrophic. So fail-open is safe
  precisely where it matters least and strictest where it matters most.
- **Perf:** non-`Bash` tools early-exit (allow) with zero work. For Bash: `shlex.split` +
  segment scan is microseconds. G3's subprocess (`git symbolic-ref`) runs ONLY on `git push`
  sub-commands — the common Bash call pays nothing. Config read is a small JSON file
  (cache within the single hook invocation; the process is short-lived so no cross-call cache
  needed).
- **Timeout:** the hook process is invoked per tool-call; G3 subprocesses use a 5s timeout
  (matches `install_hooks_lib.py` git probes) → on hang, fail-open allow.

---

## Installer coupling (Car 3)

**Verdict: Car 1 can build/merge before Car 3.** Car 1 carries the minimal installer edit
itself; Car 3 (installer-correctness) is a broader, independent concern.

Two anchor sites in `yadgar/core/install/install_hooks_lib.py` (verified line numbers):

1. **`_install_global_scripts` (lines 334-352):** currently copies
   `db-lockdown-check.py` → `yadgar-db-lockdown-check.py` (line 349-350). Car 1 changes this to
   copy `pretooluse-router.py` → `yadgar-pretooluse-router.py`, AND seed
   `~/.claude/yadgar-hook-exceptions.json` create-if-absent. Return the router dst.
2. **`_build_core_hooks` (lines 355-393):** line 392-393 builds the PreToolUse entry pointing
   at `db_lockdown_dst` with matcher `"Bash"`. Car 1 repoints it at the router dst. Matcher
   stays `"Bash"`.
3. **`hooks_installed` report list (line 634-635):** update
   `"PreToolUse (DB lockdown)"` → `"PreToolUse (router-guard)"`.

**Coupling summary:** Car 1 owns the *PreToolUse-entry swap* (the 3 edits above). Car 3 owns
*installer robustness generally* (durable-python, atomic-write, idempotency broadening). They
touch the same file but different concerns; Car 1's edits are additive/substitutive and small.
If Car 3 lands first, Car 1 rebases trivially. **Car 1 is NOT blocked by Car 3.**

Backward-compat: on reinstall over an old install, the stale
`~/.claude/yadgar-db-lockdown-check.py` script becomes orphaned but harmless (settings.json no
longer references it). Car 1 SHOULD delete the orphan on install (best-effort unlink) to avoid
confusion. Flag as a minor install step, not a blocker.

---

## Acceptance criteria

### AC-SCHEMA (verified against official docs 2026-07-13 — OQ-1 resolved)

- Deny payload carries `hookSpecificOutput.hookEventName="PreToolUse"`,
  `hookSpecificOutput.permissionDecision="deny"`, `hookSpecificOutput.permissionDecisionReason`
  (docs-canonical reason), AND top-level `systemMessage` (proven-working in db-lockdown). Both
  reason fields present — extra keys ignored by Claude Code.
- Allow payload carries `hookSpecificOutput.hookEventName="PreToolUse"`,
  `permissionDecision="allow"`, no reason fields (matches db-lockdown `_allow()`).
- Decision channel = exit-0 + stdout JSON (NOT exit code 2, which is a non-blocking stderr
  error per docs).

### AC-UNIT — fixture tool-call → decision matrix

Fixtures are stdin JSON `{"tool_name": ..., "tool_input": {"command": ...}, "cwd": ...}`.
Guard behavior must satisfy:

| # | Fixture command | tool_name | Expected | Guard | Discriminator |
|---|---|---|---|---|---|
| 1 | `ls -la /tmp` | Bash | allow | — | benign |
| 2 | (any) | Read | allow | — | non-Bash early-exit |
| 3 | `git commit --no-verify -m x` | Bash | **deny** | G1 | |
| 4 | `git commit --no-gpg-sign -m x` | Bash | **deny** | G1 | |
| 5 | `git -c commit.gpgsign=false commit -m x` | Bash | **deny** | G1 | `-c` before subcmd |
| 6 | `git commit -m "fix terraform bug"` | Bash | **allow** | — | **false-positive guard** (contains "terraform") |
| 7 | `git commit -m x` (triggers terraform_fmt pre-commit) | Bash | **allow** | — | **commit-time carve-out** (top-level argv0=git) |
| 8 | `terraform apply` | Bash | **deny** | G2 | |
| 9 | `cd foo && tofu plan` | Bash | **deny** | G2 | compound-command segmentation |
| 10 | `tfp` | Bash | **deny** | G2 | |
| 11 | `docker run hashicorp/terraform:1.5 plan` | Bash | **deny** | G2 | fresh-container |
| 12 | `nix run nixpkgs#terraform -- plan` | Bash | **deny** | G2 | nix run |
| 13 | `gh pr comment 5 --body "digger apply"` | Bash | **deny** | G2 | digger-on-PR |
| 14 | `echo "digger apply is scary"` | Bash | **allow** | — | digger mention, not a gh/api invocation |
| 15 | `git push origin master` (default=master, repo=yadgar) | Bash | **deny** | G3 | push-to-default |
| 16 | `git push origin master` (repo=nix, allowlisted) | Bash | **allow** | G3 | allowlist repo |
| 17 | `git push origin feature/x` (default=master) | Bash | **allow** | G3 | non-default branch |
| 18 | `git push --force origin HEAD:master` (repo=yadgar) | Bash | **deny** | G3 | force + refspec to default |
| 19 | `docker exec yadgar-db psql` | Bash | **deny** | G4 | subsumed db-lockdown |
| 20 | `docker exec my-app bash` | Bash | **allow** | — | other container (db-lockdown parity) |
| 21 | malformed stdin (`{broken`) | — | **allow** | — | fail-soft |
| 22 | guard raises (simulated) | Bash | **allow** | — | fail-open on router error |

### AC-CONFIG

- Reinstall with an existing exceptions.json containing a user-added repo → file NOT clobbered
  (user entry survives).
- Missing exceptions.json → router runs with built-in defaults; still denies G1/G2/G4; G3
  uses seeded allowlist defaults.
- Corrupt exceptions.json → router logs stderr, runs with built-in defaults, fail-open on G3.

### AC-INSTALL

- `install_hooks_impl` writes a PreToolUse entry pointing at `yadgar-pretooluse-router.py`
  with matcher `"Bash"` (dry-run preview asserts this).
- `hooks_installed` list reports `"PreToolUse (router-guard)"`.
- Router script + exceptions.json present in global hooks dir after install.

---

## Test plan

- **Unit (primary):** importlib-load `pretooluse-router.py` (hyphenated filename → same
  importlib trick as `test_hook_db_lockdown_check_unit.py`), patch `sys.stdin` + `print`,
  assert the AC-UNIT matrix. Pure-function guard tests (each `guard_*` directly with crafted
  token lists) + end-to-end `main()` tests. Mock `subprocess.run` for G3
  (`git symbolic-ref`/`rev-parse`) — no real git needed; parametrize default-branch +
  allowlist scenarios.
- **Installer:** extend the existing `install_hooks` test suite — dry-run asserts PreToolUse
  entry repointed to router, matcher `"Bash"`, report string updated, exceptions.json
  create-if-absent (write a sentinel, reinstall, assert survives).
- **Schema conformance:** a test that the deny payload JSON matches the AC-SCHEMA shape (keys
  present, values in the allowed enum).
- **Parity:** keep/port the db-lockdown deny/allow cases (fixtures 19-20) so subsuming does
  not regress the SurrealDB lockdown.
- **CI:** all new tests green; no regression in existing `test_hook_db_lockdown_check_unit.py`
  (keep the standalone script until the router fully replaces it, or delete + redirect its
  tests — decide in build; see OQ-3).

---

## Risks

- **R1 — false-positive blocks (HIGH impact, MED likelihood).** A guard denying legitimate work
  is worse than a missed block (erodes trust, blocks the user). Mitigation: token-aware matching
  (not substring), the explicit false-positive fixtures (6, 7, 14, 17, 20), and the
  `disabled_guards` escape hatch. G3 fail-open on resolution failure. **This is the biggest
  risk** — a mis-firing guard on a hot path (git commit / push) blocks every commit.
- **R2 — router crash bricks all Bash (HIGH impact, LOW likelihood post-mitigation).**
  Mitigated by fail-OPEN: any exception → allow. Standalone script (not via hook_runner) keeps
  the deny path dependency-free. Guards are pure ops.
- **R3 — G3 subprocess latency/hang.** Bounded: fires only on `git push`, 5s timeout,
  fail-open on timeout.
- **R4 — allowlist basename collision.** Two repos named `nix` both allowlisted. Low; curated
  list. Escalate to full-path keys if it surfaces (OQ-2).
- **R5 — orphaned db-lockdown script after upgrade.** Harmless (unreferenced) but confusing;
  best-effort unlink on install.
- **R6 — `-c commit.gpgsign=false` placement variants.** `git -c … commit` vs
  `git commit -c …` (the latter is a message-reuse flag, different meaning). Guard must
  distinguish: global `-c KEY=VAL` appears BEFORE the subcommand. Covered by fixture 5;
  message-reuse `git commit -c HEAD` must NOT deny (add as a guard test).

---

## Scope

**IN:**
- One router script subsuming db-lockdown + 3 new guards (G1-G4).
- One global exceptions JSON (create-if-absent).
- Minimal installer edits (2 anchor sites + report string + orphan cleanup).
- Unit + installer + schema tests.

**OUT:**
- Per-project exceptions override (global only for now).
- Guarding tools other than Bash (matcher stays `"Bash"`).
- A CLI/tool to edit exceptions (users edit the JSON directly).
- Broad installer-correctness hardening (that is Car 3).
- Soft-nudge / `ask` decisions (PreToolUse has no additionalContext; deny-only).
- Additional HARD RULES beyond the 3 user-approved + db-lockdown (e.g. kubectl apply,
  git push --force to arbitrary shared branches) — candidate future guards, not this Car.

---

## Version impact

- **core-only.** No backend/model/API surface change. Touches `yadgar/core/hooks/` +
  `yadgar/core/install/` + tests.
- **Sequence after #195 (v5.133.0) → v5.134.0.**
- No migration. No schema change. Installer change is backward-compatible (repoints one hook
  entry; seeds one config file create-if-absent).

---

## Open questions

- **OQ-1 (schema):** confirm `permissionDecisionReason` is a valid `hookSpecificOutput` key in
  the current Claude Code hook schema (resolved by claude-code-guide before merge; fallback =
  `systemMessage` only). Folded into AC-SCHEMA.
- **OQ-2 (allowlist key):** basename vs full-path repo key for `push_default_allowlist`.
  Plan picks basename (simplest, matches rule naming); revisit if a collision surfaces.
- **OQ-3 (db-lockdown teardown):** keep the standalone `db-lockdown-check.py` + its tests as
  dead-but-green, or delete and redirect its tests into the router suite? Recommend delete +
  redirect (single source of truth) — decide in build.
- **OQ-4 (settings scope):** the swap edits the core-hooks PreToolUse entry, which the
  installer writes to the scope's settings.json (project or global) + the router SCRIPT to the
  global hooks dir. Confirm both scopes get the router (global scope: PreToolUse in
  `~/.claude/settings.json`; project scope: PreToolUse in `<proj>/.claude/settings.json`, both
  point at the global `~/.claude/hooks/yadgar-pretooluse-router.py`). Matches db-lockdown
  today (verified: project settings.json PreToolUse already points at the global
  `~/.claude/hooks/yadgar-db-lockdown-check.py`).
```

---

## AUDIT (2026-07-13)

Adversarial audit. Read-only; original preserved above. Observed-state verified against
`yadgar/core/hooks/db-lockdown-check.py`, `yadgar/core/install/install_hooks_lib.py`,
`yadgar/tests/hooks/test_hook_db_lockdown_check_unit.py`, `pyproject.toml`.

### Per-claim verification table

| # | Claim (plan §) | Verdict | Evidence (file:line) |
|---|---|---|---|
| 1 | Deny channel = exit-0 + stdout JSON, NOT exit code (§Verified) | **VERIFIED** | `db-lockdown-check.py:52-77` exits 0, prints one JSON; no `sys.exit(2)`. Tests capture `print`, parse JSON. |
| 2 | `hookEventName` REQUIRED in `hookSpecificOutput` | **VERIFIED** | `db-lockdown-check.py:35,44`; asserted `test…:39,57,97,156,196`. |
| 3 | Deny emits top-level `systemMessage` (proven-working) | **VERIFIED** | `db-lockdown-check.py:41-48`; asserted `test…:60-69,147-151`. |
| 4 | `permissionDecisionReason` is a valid `hookSpecificOutput` key (OQ-1) | **UNVERIFIED (docs-derived, non-load-bearing)** | NOT in the shipping script. Sourced from claude-code-guide/official docs only. Additive belt-and-suspenders → uncertainty does not brick anything. Emitting both is safe. |
| 5 | "Exit code 2 = non-blocking stderr, JSON is only deny channel" | **UNVERIFIED (docs-derived) but CONSISTENT** | Script never uses exit 2; consistent with the claim. Docs-sourced, not provable from the script. Non-load-bearing (router copies the proven exit-0+JSON path regardless). |
| 6 | Fail-soft: malformed stdin → allow | **VERIFIED** | `db-lockdown-check.py:54-58`. (Plan cites `:56-58`; the `try` opens at `:54` — trivially off.) |
| 7 | Match style = naive substring `pattern in cmd` | **VERIFIED** | `db-lockdown-check.py:65-66`. |
| 8 | "`tool_name` present (`db-lockdown-check.py:60-63`, test `:105`)" | **WRONG (cite) / partially true (fact)** | Script `:60-63` reads `tool_input`→`command`, NOT `tool_name`. The script does not read `tool_name` at all. Fact "payload carries tool_name" is plausibly true (fixture `test…:104-107` passes it) but the evidence cited is the wrong lines. Router's early-exit `if tool_name != "Bash"` (§Control flow) relies on a field the precedent never reads — untested against a real payload. |
| 9 | **PreToolUse fires inside subagents; payload carries `agent_id`/`agent_type`** (§58-60, the Car's whole rationale) | **UNVERIFIED — LOAD-BEARING** | Nothing in db-lockdown or its tests proves subagent-fire. Sourced from OQ-1 docs bonus only. This is the single premise the entire "subagent-proof" value prop rests on. Must verify before merge (see decisions). |
| 10 | Install path: db-lockdown standalone, copied to `~/.claude/hooks/yadgar-db-lockdown-check.py` | **VERIFIED** | `install_hooks_lib.py:349-350` (`_install_global_scripts`). |
| 11 | PreToolUse entry built at `_build_core_hooks`, matcher `"Bash"`, points at `db_lockdown_dst` | **VERIFIED** | `install_hooks_lib.py:391-393`. |
| 12 | Report list `"PreToolUse (DB lockdown)"` to update | **VERIFIED (line drift)** | Actual `install_hooks_lib.py:635` (plan says `634-635`). |
| 13 | 5s timeout "matches install_hooks_lib.py git probes" | **VERIFIED** | `install_hooks_lib.py:55,132` (`timeout=5` on `git -C … rev-parse`). |
| 14 | Test file `test_hook_db_lockdown_check_unit.py` | **STALE (path)** | Actual: `yadgar/tests/hooks/test_hook_db_lockdown_check_unit.py`. Plan implies bare `tests/`. Line numbers inside are correct; directory is wrong. |
| 15 | "Sequence after #195 (v5.133.0) → v5.134.0" | **STALE / ambiguous** | `pyproject.toml:7` already reads `5.133.0` on disk (commit `b0f53cac`); #195 is NOT merged (only docs commit `6effa5da` references it). Either the bump landed locally or #195≠the bump. Reconcile: if HEAD is already 5.133.0, this Car IS 5.134.0 — the "after #195" framing is stale. |
| 16 | G2 commit-time carve-out is automatic (top-level argv0=git, not terraform) | **VERIFIED (logic sound)** | Correct: pre-commit `terraform_fmt` runs as an invisible git subprocess; the Bash string is `git commit …`. Fixture 7 holds. No special-casing needed — accurate. |
| 17 | Fixture 6 `git commit -m "fix terraform bug"` → allow (false-positive guard) | **VERIFIED (logic sound)** | shlex.split keeps the quoted arg one token; G2 checks argv0∈{terraform,tofu,tfp}, argv0=git → allow. Token-aware design genuinely handles the false-*positive* class. |
| 18 | Fail-OPEN on router error (crash → allow + stderr) | **VERIFIED (design)** | §Control flow `except Exception: … return _allow()`. Matches db-lockdown fail-soft philosophy. Design is internally consistent. |

**STALE: 3** (#14 path, #15 version, and #6/#12 line-drift = cosmetic). **WRONG: 1** (#8 cite). **UNVERIFIED load-bearing: 1** (#9 subagent-fire). **UNVERIFIED non-load-bearing: 2** (#4, #5).

### Schema crux — VERDICT: load-bearing part CONFIRMED

The claim that actually matters — **exit-0 + stdout JSON + `hookSpecificOutput.{hookEventName,permissionDecision:deny}` + top-level `systemMessage`** — is verified against the shipping db-lockdown code and its asserting tests (rows 1-3, 6-7). The router copies proven code → de-risked. The two *docs-derived* extras (`permissionDecisionReason`, "exit-2 is non-blocking") are **additive and non-load-bearing**: emitting both reason fields is harmless (extra keys ignored), and the router never uses exit-2 anyway. The plan's "if wrong every guard fails silently" framing is over-scary: the guard-critical schema is proven; only the belt-and-suspenders reason field is docs-only.

### Matcher false-negatives — the real risk (plan's R1 mis-ranks this)

The plan brands false-*positives* "biggest risk" (R1). Wrong emphasis: its token-aware design handles false-positives well (fixtures 6/7/14/17/20 hold). The exposed surface is false-*negatives* — legitimate-looking wrappers that slip a dangerous command past a fixed-argv-index matcher.

**WORST FINDING — git global-option index-shift breaks G1 AND G3.** Both guards locate the
subcommand at a fixed index (`argv[1]=="commit"` §163 / `argv[1]=="push"` §194). Any leading
git global option shifts it:
- `git -C /path push origin master` → `argv[1]=="-C"` → **G3 never fires → push-to-default UNGUARDED.**
- `git -C /path commit --no-verify` → **G1 never fires → hook-bypass UNGUARDED.**
- same for `git -c core.x=y …`, `git --git-dir=… …`, `git -P …`.

The plan is *half-aware*: §166 special-cases `-c KEY=VAL` before the subcommand for G1 (fixture 5)
but never generalizes, and G3 has zero such handling. `git -C` is common, legitimate, everyday
usage → this is a demonstrable, high-likelihood leak, not a corner case. **Fix:** skip leading
`git` global options when locating the subcommand (loop past tokens starting `-` / matching known
global-opt shapes until the first non-option = subcommand), then apply G1/G3.

**Wrapper-bypass class (task named env-prefix + subshell; full enumeration):** the segment-scan
inspects only each sub-command's own `argv[0]`. It misses:
- `sudo terraform apply`, `env FOO=bar terraform apply` (argv0 = sudo/env).
- `bash -c "terraform apply"`, `sh -c '…'`, heredoc into `bash` (dangerous cmd is a string arg).
- command substitution `$(terraform apply)` / backticks (not a top-level segment).
- `xargs terraform`, `nohup terraform`, `time terraform`, subshell `(terraform apply)`.
- shell aliases (`alias tf=terraform`) — unresolvable by a static matcher; accept as out-of-reach.

**G1-specific extras:** git accepts unique-prefix long flags → `git commit --no-verif` bypasses an
exact-`--no-verify` token check; bundled short flags `-nm` (n+m) bypass a bare `-n` check. Guard
must prefix-match `--no-veri…` and scan bundled shorts.

**G3-specific:** `git symbolic-ref refs/remotes/origin/HEAD` fails when `origin/HEAD` is unset
locally (common on fresh/CI clones) → fail-open → **push-to-default guard silently inoperative**
on exactly those repos. Note as a real limitation, not just theoretical.

Net: after these, the BLUF's "subagent-proof mechanical block" is over-claimed. Realistically this
is a **speed-bump against accidental / forgotten-prose violations**, NOT a wall against wrapped,
scripted, or determined bypass. The verdict must reflect that gap.

### Fail-open — VERIFIED, design is coherent

Any exception → `_allow()` + stderr log (§Control flow, §Fail-open). The reconciliation argument
(§262-267: G1/G2/G4 are pure string ops that can't throw; only G3 shells out and its fail-open
blast is a *recoverable* push, not a catastrophic terraform apply) is sound. One caveat: pervasive
fail-open is precisely *why* the matcher gaps above matter — there is no defence-in-depth backstop;
a missed match = allow, full stop.

### Acceptance-criteria matrix — mostly testable, gaps

AC-SCHEMA + AC-UNIT (22 fixtures) + AC-CONFIG + AC-INSTALL are concrete and cover both deny and
carve-out ALLOW cases (fixtures 6,7,14,16,17,20 = the ALLOW discriminators). **Missing fixtures
(add before build):**
- `git -C /p push origin master` → expected deny (currently would ALLOW = the G3 leak).
- `git -C /p commit --no-verify` → expected deny (G1 leak).
- `sudo terraform apply` / `env X=1 terraform apply` → decision the team must *choose* (deny = need wrapper-peel; accept-as-allow = documented limitation).
- `git commit -c HEAD -m x` (message-reuse `-c`, NOT gpgsign) → expected **allow** (plan R6 names it but no fixture).
- `git commit --no-verif -m x` (prefix flag) → expected deny.
- G3 with `origin/HEAD` unset → documents fail-open-allow behavior.

### Installer coupling — VERIFIED, Car1-before-Car3 verdict holds

Anchor sites confirmed: `_install_global_scripts:349-350`, `_build_core_hooks:391-393`, report
`:635`. Car 1's edits are additive/substitutive and self-contained (repoint one entry + seed one
config create-if-absent + orphan unlink). Independent of Car 3's broad installer hardening. **"Car 1
can merge before Car 3" = correct.** Version: **core-only is right** (touches only
`yadgar/core/hooks/` + `yadgar/core/install/` + tests). Target number needs the #195/5.133.0
reconciliation above — if HEAD already ships 5.133.0, this Car is simply the next bump 5.134.0.

### Status: **AUDITED — needs rework** (targeted, not structural)

The architecture is sound and the schema is de-risked by copying proven code. Rework is confined to
the matcher and two honesty fixes. Blocking items:

1. **Generalize subcommand-finding past git global options** (fixes the `git -C` G1+G3 leak). Non-negotiable — `git -C` is common legitimate usage and the leak is silent.
2. **Verify PreToolUse fires in subagents with `agent_id` in payload** (row 9) — the Car's entire rationale. Cheap via claude-code-guide; do it or make it a gating decision.
3. **Downgrade over-strong claims:** BLUF "subagent-proof" → "mechanical speed-bump"; state the wrapper/prefix-flag/global-opt limitations explicitly in Scope-OUT.
4. Fix WRONG cite (row 8), STALE test path (row 14), reconcile version line (row 15).

### User-decisions required

- **D1 (matcher scope):** peel wrapper prefixes (`sudo`/`env`/`bash -c`/`xargs`) and inspect
  inside, OR accept them as documented false-negatives? Peeling adds complexity + its own
  false-positive surface. Recommend: peel `sudo`/`env` (cheap, common); document `bash -c`/subshell
  as out-of-scope limitations.
- **D2 (git global-opt handling):** confirm generalized subcommand-finding is IN scope for this Car
  (audit says it must be — it's the worst leak).
- **D3 (subagent-fire verification):** verify now (claude-code-guide) or accept the docs claim and
  ship? Audit recommends verify — it's the load-bearing premise.
- **D4 (OQ-3 db-lockdown teardown):** delete + redirect tests vs keep dead-but-green. Audit concurs
  with plan's recommend = delete + redirect (single source of truth).
- **D5 (version):** reconcile whether this Car is 5.134.0 given pyproject already reads 5.133.0.
