# PreToolUse Router-Guard (HOOKS train, Car 1)

**Status: AUDITED-ready** (D3 gate CLOSED 2026-07-13 — claude-code-guide confirmed against official docs https://code.claude.com/docs/en/hooks.md that PreToolUse fires in subagents AND carries `agent_id`/`agent_type` when in a subagent AND `tool_name` is a real top-level stdin field; see "Pre-build gating item (D3)" below)
**Date:** 2026-07-13 (reworked from DRAFT — audit fixes folded into body 2026-07-13)
**Scope:** core-only. Sequence after #191 (core 5.132.0 on HEAD) → target **core 5.133.0**.
**Author:** design agent (Opus). Observed-state-verified against actual hook files + `install_hooks_lib.py` + `pyproject.toml` + tests on 2026-07-13.

---

## BLUF

Replace the single-purpose `db-lockdown-check.py` PreToolUse hook with **one router
script** (`pretooluse-router.py`) that reads the tool-call JSON on stdin, dispatches on
`tool_name` to per-tool guard functions, and returns an allow/deny decision. Turns four
prose HARD RULES into mechanical, **wrapper-and-global-option-aware** guard blocks:

1. `git commit --no-verify` / `--no-gpg-sign` / `-c commit.gpgsign=false` → **deny**
2. `terraform`/`tofu`/`tfp` invocation (+ `docker run …terraform`, `nix run …#terraform`,
   `digger …` in `gh pr comment`/`gh api` bodies) → **deny**
3. `git push` to the repo default branch → **deny**, with a JSON repo-allowlist
   (seeded `nix`, `ledger`, `ostad`)
4. **Subsumes** the existing SurrealDB docker-exec lockdown (`docker exec yadgar-db|backend`)

**Guard count: 4** (3 user-approved + 1 subsumed). **Router fails OPEN** — a bug must never
brick every Bash call; only a positively-matched dangerous pattern denies.

**Honest-scope framing (audit fix, do NOT over-claim):** this router is a **mechanical
speed-bump against accidental and forgotten-prose violations** — not a hardened
sandbox and not a wall against a determined or scripted bypass. It defeats the common
real-world leaks (leading git global options like `git -C`, transparent wrapper prefixes
like `sudo`/`env`, one level of `bash -c` recursion, prefix/bundled flag forms). It does
NOT defeat shell aliases, command substitution (`$(…)`/backticks), deeply nested subshells,
or an adversary who actively wants around it. Because the router fails OPEN with no
defence-in-depth backstop, a missed match = allow — so the exposed surface that matters is
**false-negatives**, not false-positives (see Risks R1, re-ranked). The value proposition is:
prose HARD RULES reset per subagent spawn; a mechanical hook does not — so the router catches
the delegated/forgetful violation that prose cannot, for the patterns it does cover.

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
- **Stdin fields (CORRECTED cite):** the shipping script reads `tool_input` (dict) →
  `tool_input.command` (Bash command string) at `db-lockdown-check.py:60-63`. It does **NOT**
  read `tool_name` at all — the earlier draft mis-cited `:60-63` for `tool_name`; those lines
  read `tool_input`→`command`. The router's control-flow adds a `tool_name` early-exit
  (`if tool_name != "Bash"`), which depends on `tool_name` being a real top-level stdin field.
  The precedent script never reads it — **now confirmed against official docs (D3, 2026-07-13):**
  `tool_name` IS a real top-level PreToolUse stdin field (e.g. `"Bash"`, `"Edit"`, MCP tool
  names). Safe to key the early-exit on it.
- **Match style:** naive substring `pattern in cmd` (`db-lockdown-check.py:65-66`). SAFE for
  container names (`docker exec yadgar-db`); **NOT safe** for `terraform`/`git` tokens — see
  match-rules section (both false-positive AND false-negative hazard).
- **Fail-soft:** malformed stdin → allow (`db-lockdown-check.py:54-58`; the `try` opens at
  `:54`). Router keeps this.
- **Subagent fire (VERIFIED 2026-07-13 — D3 gate CLOSED):** the "beats prose rules" value prop
  rests on the premise that PreToolUse fires inside subagents and the payload carries agent info
  so the router catches delegated violations. This was UNVERIFIED (docs-bonus only) at DRAFT.
  **Now confirmed** against official docs (https://code.claude.com/docs/en/hooks.md, via
  claude-code-guide): PreToolUse "fires on every tool invocation, regardless of whether it's in
  the main session thread [or] inside a subagent call"; and the payload carries **Subagent
  Context Fields** `agent_id` + `agent_type` **when (and only when) fired inside a subagent**.
  The router does not need to read those fields to function (its guards act on `tool_input.command`
  regardless of thread), but their presence proves the guard fires in subagent context — which is
  the whole point. Premise holds.
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
- **Shared front-end:** every guard consumes the output of the tokenize → segment →
  wrapper-peel → git-global-option-peel pipeline (below), not the raw `cmd` string. G4
  (db-lockdown) is the one exception — it retains verbatim substring matching on `cmd` (container
  names are specific, no false-positive risk, proven behavior).

### Command tokenization + normalization (critical — the load-bearing correctness layer)

db-lockdown's `pattern in cmd` has TWO failure classes the router must fix:
- **false-*positive*:** `git commit -m "fix terraform bug"` (substring "terraform" → spurious deny).
- **false-*negative* (the real exposed surface):** a fixed-argv-index matcher
  (`argv[1]=="commit"` / `argv[1]=="push"`) is silently shifted by any leading git global
  option or wrapper prefix, so the dangerous subcommand slips past. `git -C /path push origin
  master` and `git -c k=v commit --no-verify` are common, legitimate everyday forms — this is a
  demonstrable high-likelihood leak, not a corner case.

The router therefore does NOT locate subcommands at a fixed index. It runs a four-stage
tokenize → segment → **wrapper-peel** → **git-global-option-peel** pipeline, then applies the
per-guard match on the *resolved* argv.

**Stage 1 — tokenize.** `shlex.split(cmd)` (fail-soft: on `ValueError`, fall back to
whitespace split, still guard). shlex keeps quoted args as single tokens — this is what makes
the false-*positive* guard hold (`-m "fix terraform bug"` is one token, never inspected as a
program).

**Stage 2 — segment.** Split the token list on shell separators `&&`, `||`, `;`, `|`, and
strip a leading `(`/trailing `)` subshell wrapper on a segment → list of sub-commands. Each
sub-command is guarded independently; first deny wins. **Limitation (documented, out of reach):**
command substitution `$(…)` / backticks are NOT top-level segments (shlex does not expand them),
so a dangerous command hidden inside `$( )` is NOT seen — accept as a false-negative (Scope-OUT).

**Stage 3 — wrapper-peel (`peel_wrappers(argv) -> argv`).** Repeatedly strip a leading
transparent wrapper to reach the real command, consuming the wrapper's OWN arguments so we don't
land on a wrapper arg by mistake:

- `sudo` — peel; skip its options: flags until the first non-flag token. Arg-consuming sudo
  flags to skip-with-next: `-u <user>`, `-g <group>`, `-C <fd>`, `-p <prompt>`. `sudo -- cmd`:
  peel through the `--`.
- `env` — peel; skip leading `VAR=value` assignment tokens (and `-i`, `-u <name>`), then the
  remainder is the real command. (`env FOO=bar terraform apply` → resolves to `terraform apply`.)
- `nice` — peel; skip `-n <N>` / `-<N>` adjustment.
- `timeout` — peel; skip its `DURATION` positional (and flags like `-s <sig>`, `-k <dur>`,
  `--preserve-status`), then the remainder is the real command.
- `nohup`, `xargs`, `time`, `stdbuf …`, `ionice …` — peel their own flags similarly (best-effort;
  the common transparent forms).
- `bash -c "<STR>"` / `sh -c '<STR>'` (and `zsh -c`) — **recurse:** re-feed `<STR>` through
  Stage 1→4 (re-tokenize, re-segment, re-peel, re-guard). One level of recursion (bounded; a
  depth counter caps at e.g. 3 to avoid pathological nesting) — fail-OPEN (allow) if the inner
  string is unparseable. This satisfies the task's "detect+deny-or-recurse into the wrapped
  command" for `bash -c`; we choose **recurse** (stricter than merely documenting it out of scope).

Peeling is **best-effort and fail-open on exotic option combos** — it adds its own small
false-positive surface, so it is scoped to the common transparent forms above; an unrecognized
wrapper shape leaves argv as-is (which then simply doesn't match a guard = allow). Shell aliases
(`alias tf=terraform`) are unresolvable by a static matcher → out of reach (Scope-OUT).

**Stage 4 — git-global-option-peel (`peel_git_globals(argv) -> (argv, pre_c_opts)`).** When the
peeled `argv[0]` basename is `git`, walk forward from `argv[1]` skipping leading git *global*
options until the first non-option token = the real git **subcommand**. This is the single fix
that closes the `git -C` G1+G3 leak. Global options and their argument-consumption:

- Arg-consuming, SPACE form (skip the flag AND the next token): `-C <path>`, `-c <name=value>`,
  `--git-dir <path>`, `--work-tree <path>`, `--namespace <name>`, `--exec-path <path>`,
  `--super-prefix <path>`.
- `=`-joined form (skip one token): `--git-dir=…`, `--work-tree=…`, `--namespace=…`,
  `--exec-path=…`, `-c name=value` may also appear as one token in some shells — treat any single
  token matching `-c` followed by a separate `k=v`, OR a `--<opt>=<val>` token, as one unit.
- Standalone (skip one token): `-p`, `--paginate`, `-P`, `--no-pager`, `--bare`, `--no-replace-objects`,
  `--literal-pathspecs`, `--no-optional-locks`, `--html-path`, etc.
- **Collect the pre-subcommand `-c k=v` pairs** into `pre_c_opts` — G1 inspects them for
  `commit.gpgsign=false`/`=0`/`=no`/`=off` (this is the ONLY correct place to read gpgsign; a
  `-c` appearing AFTER the subcommand is a different flag — see G1).
- Unknown `-<x>` before a subcommand: skip it conservatively as a standalone global (single
  token) so we still reach the subcommand; if that misclassifies an arg-consuming unknown, the
  worst case is landing one token early = a benign non-match (fail-open), never a spurious deny.
- If no non-option token is ever found (e.g. `git --version`, `git -C /p` with nothing after),
  there is no subcommand → no G1/G3 match → allow.

After Stage 4, G1/G3 test the resolved subcommand (`subcmd == "commit"` / `subcmd == "push"`)
plus the *remaining post-subcommand* tokens for their flags — never a fixed index.

---

## Pre-build gating item (D3) — CLOSED 2026-07-13

The Car's entire "beats prose rules" rationale rests on ONE premise that db-lockdown never
proves: **PreToolUse fires inside subagents, with a payload the router can key on.** The DRAFT
assumed it. This is a gating pre-build item, resolved BEFORE writing the router:

- **Verified 2026-07-13** via claude-code-guide against official docs
  (https://code.claude.com/docs/en/hooks.md):
  1. PreToolUse "fires on every tool invocation, regardless of whether it's in the main session
     thread [or] inside a subagent call." → **subagent-fire CONFIRMED.**
  2. Payload carries **Subagent Context Fields** `agent_id` + `agent_type` **when in a subagent**
     → agent info present as claimed.
  3. `tool_name` IS a real top-level stdin field (`"Bash"`/`"Edit"`/MCP names) → the router's
     `if tool_name != "Bash"` early-exit is safe.

Gate satisfied → Status **AUDITED-ready**. (Had it failed — e.g. PreToolUse main-thread-only —
the Car's value prop would collapse and the design would need rethinking before any code.)

---

## Per-guard match rules + carve-outs

### G1 — git commit hook-bypass flags (HARD RULE: No Hook Bypass)

Runs on the resolved argv AND `pre_c_opts` from Stage 3→4 (wrapper-peel + git-global-option-peel),
so `git -C /p commit --no-verify`, `env X=1 git commit -n`, and `bash -c "git commit --no-verify"`
are all caught, not just bare `git commit …`.

- **Match:** peeled program basename `== "git"` AND resolved subcommand `== "commit"` AND EITHER:
  - a forbidden flag among the **post-subcommand** tokens: `--no-verify`, its unique-prefix long
    forms (`--no-verif`, `--no-veri`, … — prefix-match any `--no-veri`-prefixed token, since git
    accepts unambiguous long-option prefixes), `-n` (git commit's short `--no-verify`),
    `--no-gpg-sign`, or `-n` appearing inside a **bundled short group** (`-nm`, `-am` with `n` —
    scan each char of a `-<letters>` bundle for `n`); OR
  - a `-c commit.gpgsign=<false|0|no|off>` pair in `pre_c_opts` (the pre-subcommand git globals
    collected in Stage 4) — this is the `git -c commit.gpgsign=false commit` form.
- **Deny.** High-frequency violation; blast = silent skip of sign/verify hooks.
- **Carve-outs / non-matches (must NOT deny):**
  - `-n`/`-nm` bundling is treated as `--no-verify` ONLY when the resolved subcommand is `commit`
    (git commit's `-n`; harmless elsewhere, and we only inspect resolved `commit`).
  - **`git commit -c <commit-ish>` (message-reuse) MUST NOT deny.** A `-c` that appears AFTER the
    subcommand is git commit's `--reuse-message` flag (takes a commit ref like `HEAD`), NOT a
    global config setter. G1 reads gpgsign ONLY from `pre_c_opts` (pre-subcommand globals), never
    from post-subcommand tokens — so `git commit -c HEAD -m x` resolves to no gpgsign match =
    allow. Covered by fixture 5b.

### G2 — terraform family (HARD RULE: No Terraform)

- **Match (deny) — any sub-command whose peeled `argv[0]` (basename, AFTER wrapper-peel) ∈
  {`terraform`, `tofu`, `tfp`}**, ANY subcommand (apply/plan/init/validate/fmt/state/… — the rule
  covers outcomes). Because Stage 3 peels wrappers, `sudo terraform apply`,
  `env FOO=bar terraform apply`, `timeout 300 terraform apply`, `nice terraform apply`, and
  `bash -c "terraform apply"` all resolve to `argv[0]==terraform` → deny.
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

- **Match candidate:** sub-command whose peeled program basename `== "git"` AND resolved
  subcommand (after git-global-option-peel, Stage 4) `== "push"`. This closes the
  `git -C /path push origin master` leak — the fixed-`argv[1]` matcher in the DRAFT would see
  `argv[1]=="-C"` and never fire; Stage 4 walks past `-C /path` to reach the real `push`.
- **Resolve default branch:** `git symbolic-ref refs/remotes/origin/HEAD` (in `ctx.cwd`),
  strip to short name (e.g. `master`/`main`). Fail-soft: if resolution fails → **allow**
  (cannot prove it targets default; fail-open — see below).
  - **Documented real limitation (not just theoretical):** `git symbolic-ref
    refs/remotes/origin/HEAD` FAILS when `origin/HEAD` is unset locally — common on fresh clones
    and CI checkouts (`git clone` sets it, but many CI flows and `git fetch`-only setups leave it
    unset). On those repos G3 fail-opens → **push-to-default guard is silently inoperative there.**
    Acceptable given fail-open philosophy, but must be stated (fixture 18c documents the behavior).
    A future hardening could fall back to `git remote show origin` or the configured
    `init.defaultBranch`, but that shells out more and is OUT of scope for this Car.
- **Determine push target:** parse the **post-subcommand** `git push` args (the tokens after the
  resolved `push`, so leading globals like `-C /path` are already stripped) for an explicit
  `<remote> <refspec>`. Deny when the push writes the default branch:
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
3. **`hooks_installed` report list (line 635, verified):** update
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
| 5 | `git -c commit.gpgsign=false commit -m x` | Bash | **deny** | G1 | `-c` before subcmd (pre_c_opts) |
| 5b | `git commit -c HEAD -m x` | Bash | **allow** | — | **message-reuse `-c` after subcmd** — NOT gpgsign (R6) |
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
| 16 | `git push origin master` (repo=nix, allowlisted) | Bash | **allow** | G3 | allowlist repo (nix) |
| 16b | `git push origin master` (repo=ledger, allowlisted) | Bash | **allow** | G3 | allowlist repo (ledger) |
| 16c | `git push origin master` (repo=ostad, allowlisted) | Bash | **allow** | G3 | allowlist repo (ostad) |
| 17 | `git push origin feature/x` (default=master) | Bash | **allow** | G3 | non-default branch |
| 18 | `git push --force origin HEAD:master` (repo=yadgar) | Bash | **deny** | G3 | force + refspec to default |
| **FALSE-NEGATIVE (leak) fixtures — the load-bearing matcher-redesign coverage** ||||||
| 23 | `git -C /p push origin master` (default=master, repo=yadgar) | Bash | **deny** | G3 | **`git -C` global-opt shift** — DRAFT would ALLOW (leak) |
| 24 | `git -C /p commit --no-verify -m x` | Bash | **deny** | G1 | **`git -C` global-opt shift** — DRAFT would ALLOW (leak) |
| 25 | `git -c core.x=y push origin master` (repo=yadgar) | Bash | **deny** | G3 | non-gpgsign `-c` global still shifts subcmd |
| 26 | `sudo terraform apply` | Bash | **deny** | G2 | wrapper-peel (`sudo`) |
| 27 | `env FOO=bar terraform apply` | Bash | **deny** | G2 | wrapper-peel (`env` + assignment) |
| 28 | `timeout 300 terraform apply` | Bash | **deny** | G2 | wrapper-peel (`timeout` + duration positional) |
| 29 | `nice terraform apply` | Bash | **deny** | G2 | wrapper-peel (`nice`) |
| 30 | `bash -c "terraform apply"` | Bash | **deny** | G2 | **`bash -c` recursion** (re-tokenize inner string) |
| 31 | `git commit --no-verif -m x` | Bash | **deny** | G1 | unique-prefix long flag (`--no-veri…`) |
| 32 | `git commit -nm x` | Bash | **deny** | G1 | bundled short (`-n` inside `-nm`) |
| **fail-soft / limitation-documenting fixtures** ||||||
| 18c | `git push origin master`, `origin/HEAD` UNSET (repo=yadgar) | Bash | **allow** | G3 | **documents fail-open** when default unresolvable (real CI/fresh-clone limitation) |
| 33 | `alias tf=terraform; tf apply` | Bash | **allow** | — | **documented out-of-reach** — static matcher can't resolve aliases (Scope-OUT) |
| 34 | `$(terraform apply)` / `` `terraform apply` `` | Bash | **allow** | — | **documented out-of-reach** — command substitution not a top-level segment (Scope-OUT) |
| 19 | `docker exec yadgar-db psql` | Bash | **deny** | G4 | subsumed db-lockdown |
| 20 | `docker exec my-app bash` | Bash | **allow** | — | other container (db-lockdown parity) |
| 21 | malformed stdin (`{broken`) | — | **allow** | — | fail-soft |
| 22 | guard raises (simulated) | Bash | **allow** | — | fail-open on router error |

Fixtures 23-32 are the **matcher-redesign acceptance gate** — each is a DRAFT-era leak that the
wrapper-peel + git-global-option-peel pipeline must now DENY. Fixtures 18c/33/34 pin the
explicitly-documented out-of-reach limitations as ALLOW (so a future contributor doesn't mistake
them for regressions). Fixtures 16/16b/16c exercise all three seeded allowlist repos.

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

- **Test location:** new tests live under `yadgar/tests/hooks/` (same dir as the existing
  `test_hook_db_lockdown_check_unit.py` — NOT a bare `tests/`). Suggested file:
  `yadgar/tests/hooks/test_hook_pretooluse_router_unit.py`.
- **Unit (primary):** importlib-load `pretooluse-router.py` (hyphenated filename → same
  importlib trick as `yadgar/tests/hooks/test_hook_db_lockdown_check_unit.py`), patch `sys.stdin`
  + `print`, assert the AC-UNIT matrix. Pure-function guard tests (each `guard_*` directly with
  crafted token lists) + **dedicated peel-pipeline tests** (`peel_wrappers`, `peel_git_globals`
  as pure functions — arg-consuming globals, `=`-joined forms, pre_c_opts collection, wrapper
  arg-skip, `bash -c` recursion depth cap) + end-to-end `main()` tests. Mock `subprocess.run`
  for G3 (`git symbolic-ref`/`rev-parse`) — no real git needed; parametrize default-branch +
  allowlist + `origin/HEAD`-unset scenarios.
- **Installer:** extend the existing `install_hooks` test suite — dry-run asserts PreToolUse
  entry repointed to router, matcher `"Bash"`, report string updated, exceptions.json
  create-if-absent (write a sentinel, reinstall, assert survives).
- **Schema conformance:** a test that the deny payload JSON matches the AC-SCHEMA shape (keys
  present, values in the allowed enum).
- **Parity:** keep/port the db-lockdown deny/allow cases (fixtures 19-20) so subsuming does
  not regress the SurrealDB lockdown.
- **CI:** all new tests green. Per D4 (resolved: delete + redirect), the standalone
  `yadgar/tests/hooks/test_hook_db_lockdown_check_unit.py` is deleted and its deny/allow parity
  cases ported into the router suite as fixtures 19-20 — so there is no lingering
  dead-but-green suite to keep passing.

---

## Risks

- **R1 — false-NEGATIVE leaks (HIGH impact, HIGH likelihood pre-mitigation — THE exposed
  surface).** Re-ranked from the DRAFT, which wrongly branded false-*positives* the biggest risk.
  The token-aware design already handles false-positives well (fixtures 6/7/14/17/20/5b hold).
  The real danger is a dangerous command slipping PAST a guard, because the router fails OPEN with
  no defence-in-depth backstop (a missed match = allow, full stop). The worst instance —
  **`git -C`/wrapper index-shift** — is what the Stage 3/4 peel pipeline exists to close
  (fixtures 23-32). **Residual, accepted false-negatives (documented, out of reach):** shell
  aliases (fixture 33), command substitution `$(…)`/backticks (fixture 34), deeply nested
  subshells beyond the recursion cap, and G3 when `origin/HEAD` is unset (fixture 18c). Net: this
  is a **speed-bump against accidental/forgotten-prose violations, not a wall** against a scripted
  or determined bypass (see BLUF honest-scope framing + Scope-OUT). Mitigation: the peel pipeline,
  the leak fixtures 23-32 as an acceptance gate, and the `disabled_guards` escape hatch.
- **R1b — false-positive blocks (HIGH impact, LOW-MED likelihood).** A guard denying legitimate
  work erodes trust. Mitigation: token-aware matching (not substring), quoted-arg preservation via
  shlex (fixture 6), the message-reuse `-c` carve-out (fixture 5b), peel being best-effort
  fail-open on exotic combos (never a spurious deny — worst case lands one token early = benign
  non-match), and the `disabled_guards` escape hatch. Hot-path (commit/push) mis-fire is the
  concern, bounded by the explicit ALLOW fixtures.
- **R2 — router crash bricks all Bash (HIGH impact, LOW likelihood post-mitigation).**
  Mitigated by fail-OPEN: any exception → allow. Standalone script (not via hook_runner) keeps
  the deny path dependency-free. Guards are pure ops.
- **R3 — G3 subprocess latency/hang.** Bounded: fires only on `git push`, 5s timeout,
  fail-open on timeout.
- **R4 — allowlist basename collision.** Two repos named `nix` both allowlisted. Low; curated
  list. Escalate to full-path keys if it surfaces (OQ-2).
- **R5 — orphaned db-lockdown script after upgrade.** Harmless (unreferenced) but confusing;
  best-effort unlink on install.
- **R6 — `-c commit.gpgsign=false` placement variants (RESOLVED by Stage 4 `pre_c_opts`).**
  `git -c … commit` (global config setter, gpgsign) vs `git commit -c …` (message-reuse
  `--reuse-message`, takes a commit-ish — different meaning). G1 distinguishes them by reading
  gpgsign ONLY from `pre_c_opts` (pre-subcommand globals collected in Stage 4), never from
  post-subcommand tokens. Deny form covered by fixture 5; message-reuse `git commit -c HEAD -m x`
  must NOT deny — covered by fixture 5b.

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
- **Explicitly out-of-reach false-negatives (stated so they are not mistaken for bugs):**
  - **Shell aliases** (`alias tf=terraform; tf apply`) — a static matcher cannot resolve
    runtime aliases. (Fixture 33.)
  - **Command substitution** `$(terraform apply)` / backticks — not a top-level shell segment;
    shlex does not expand it. (Fixture 34.)
  - **Nested subshells beyond the `bash -c` recursion depth cap** (default 3).
  - **G3 default-branch resolution** when `origin/HEAD` is unset locally (fresh clone / CI) →
    guard fail-opens on that repo. (Fixture 18c.)
  - **Determined/scripted bypass generally** — the router is a mechanical speed-bump against
    accidental and forgotten-prose violations, NOT a sandbox. (BLUF honest-scope framing.)

---

## Version impact

- **core-only.** No backend/model/API surface change. Touches `yadgar/core/hooks/` +
  `yadgar/core/install/` + `yadgar/tests/hooks/`.
- **Version (reconciled against observed HEAD 2026-07-13):** `pyproject.toml:7` on HEAD reads
  **core `5.132.0`** (last bump = #191, commit `471eba13`). There is **no `#195` and no
  `5.133.0`** anywhere in the git log — the DRAFT's "after #195 → v5.134.0" and the audit's
  "5.133.0 already on disk from `b0f53cac`" were BOTH stale/incorrect (neither ref exists in the
  tree). This Car is simply the next core bump: **core 5.132.0 → 5.133.0.** Backend version
  unchanged.
- No migration. No schema change. Installer change is backward-compatible (repoints one hook
  entry; seeds one config file create-if-absent).

---

## Resolved decisions (audit D1–D5 + OQ-1–4 — defaults applied, no open blockers)

All audit user-decisions are resolved inline with sensible defaults below. **None genuinely
require the user** — the one real gate (D3, subagent-fire) was verified 2026-07-13. Build may
proceed on these defaults; flagged where a reasonable person might choose otherwise.

- **D1 (wrapper-prefix handling) — RESOLVED: peel + recurse (stricter than audit's suggestion).**
  The audit floated "peel `sudo`/`env`, document `bash -c` out of scope." The task mandates
  handling `bash -c` ("detect+deny-or-recurse"), so this plan **peels `sudo`/`env`/`nice`/
  `timeout`/`nohup`/`xargs`/`time` AND recurses one bounded level into `bash -c "…"`** (Stage 3).
  Aliases + `$(…)` stay documented out-of-reach. No user input needed.
- **D2 (git global-opt handling) — RESOLVED: IN scope, mandatory.** Generalized
  subcommand-finding (Stage 4 `peel_git_globals`) is the load-bearing fix; it is the design's
  spine, not optional. No user input needed.
- **D3 (subagent-fire verification) — RESOLVED / CLOSED 2026-07-13.** Verified via
  claude-code-guide against official docs (see "Pre-build gating item (D3)"). PreToolUse fires in
  subagents, carries `agent_id`/`agent_type` in-subagent, and `tool_name` is a real top-level
  field. Gate satisfied → Status AUDITED-ready. (This was the only decision that could have
  needed the user; it did not — docs were unambiguous.)
- **D4 (db-lockdown teardown, = OQ-3) — RESOLVED: delete + redirect (single source of truth).**
  Plan and audit already concur. On build: delete `yadgar/core/hooks/db-lockdown-check.py` +
  `yadgar/tests/hooks/test_hook_db_lockdown_check_unit.py`, port its deny/allow cases into the
  router suite as fixtures 19-20, best-effort-unlink the orphaned installed script. No user input
  needed.
- **D5 (version) — RESOLVED: core 5.132.0 → 5.133.0** (observed HEAD reconciliation; see Version
  impact). No `#195`/`5.133.0` exists in-tree; observed state wins. No user input needed.

### Remaining OQ (non-blocking, defaults chosen)

- **OQ-1 (schema `permissionDecisionReason`) — RESOLVED (non-load-bearing).** Emit BOTH
  `permissionDecisionReason` (docs-canonical) AND top-level `systemMessage` (proven-working in
  db-lockdown). Extra keys ignored → safe. Folded into AC-SCHEMA. (Audit confirmed the
  load-bearing part — exit-0 + JSON + `hookEventName` + `systemMessage` — against shipping code.)
- **OQ-2 (allowlist key) — DEFAULT: repo basename.** Simplest, matches how the rule names
  `nix`/`ledger`/`ostad`. Documented collision risk (two repos named `nix`); escalate to
  full-path keys only if a collision surfaces. Non-blocking.
- **OQ-4 (settings scope) — RESOLVED: both scopes point at the global router script.** Global
  scope: PreToolUse in `~/.claude/settings.json`; project scope: PreToolUse in
  `<proj>/.claude/settings.json` — both reference the global
  `~/.claude/hooks/yadgar-pretooluse-router.py`. Matches db-lockdown today (project settings.json
  PreToolUse already points at the global `~/.claude/hooks/yadgar-db-lockdown-check.py`).
  Non-blocking.
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
