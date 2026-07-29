# Fix: the shell/`make` installers never provision code_graph — enabled flag with no binary

**Date:** 2026-07-29
**Status:** DESIGN — awaiting user decisions (see [Open decisions](#open-decisions-for-the-user))
**Train:** `feat/v5.169-install-runtime-fixes` — scoped to ONE car
**Follows:** `7cd74ea0 fix(setup): install code_graph by default, unattended; single coherent opt-out`
**Sibling cars (merged, overlap analysed below):** `7cd74ea0` (owns `cli/setup.py` + `core/install/`), `bb237101` (owns `enable_linger.sh` + the linger step + `Makefile` enable-units)

---

## Symptom (observed, not inferred)

`7cd74ea0` made the Python `yadgar setup` provision code_graph by default:
`cli/setup.py:147-180` (`_maybe_install_code_graph`) installs the host binary and
persists `code_graph.enabled=true`, or — with `--no-code-graph`, or on a failed
download — persists `false` so the flag and the filesystem agree.

**That fix is unreachable from the two installers most users actually run.**

`scripts/install/yadgar-setup.sh` `main()` (`:785-833`) runs its own 11-step chain
and never invokes `yadgar setup`:

| Step | Function | Line |
| --- | --- | --- |
| 1 detect runtime+OS | `_step_detect` | `:376` |
| 2 pull images | `_step_pull_images` | `:395` |
| 3 bootstrap secrets | `_step_bootstrap_secrets` | `:412` |
| 3b op inject (macOS) | `_step_inject_secrets` | `:428` |
| 4 generate units | `_step_generate_units` | `:452` |
| 4b pre-create XDG dirs | `_step_pre_create_dirs` | `:506` |
| 5 enable units (+ linger) | `_step_enable_units` | `:555` |
| 6 install hooks | `_step_install_hooks` → `yadgar install --client claude-code --hooks --scope global` | `:597` |
| 7 install agents | `_step_install_agents` → `yadgar install-subagents` | `:607` |
| 8 config sync | `_step_config_sync` → `yadgar config init` / `config sync` | `:612` |
| 9 install rules | `_step_install_rules` → `yadgar install --client claude-code --rules` | `:623` |
| 10 seed anchors | `_step_seed_anchors` → `yadgar seed --anchors` | `:704` |
| 11 seed agent-prompts | `_step_seed_agent_prompts` → `yadgar seed --agent-prompts` | `:729` |

The `Makefile` `setup:` chain (`:209-247`) is the same shape and likewise never
calls `yadgar setup`.

**Consequence.** `code_graph.enabled` defaults to `true` with no row (ADR-0163),
so a shell/`make` install produces a machine where code_graph is ON and
`~/.local/bin/codebase-memory-mcp` does not exist — the exact incoherent end state
`7cd74ea0` existed to eliminate, on the surface pipx / brew / nix-profile / repo
users hit. Nothing in the shell path writes `code_graph.enabled` at all: `grep`
for `code_graph` / `codebase-memory-mcp` in `scripts/` and `Makefile` returns zero
hits (the only non-Python references are `flake.nix:44-173` and
`scripts/sync_version.py:54`).

Also note `yadgar-setup.sh:79` already refers to "the `--code-graph` defect
removed one car earlier in this train" — the shell surface *documents* the Python
car it cannot reach.

---

## Correcting the deferral premise — the test does NOT pin step numbering

The earlier car deferred this on the belief that renumbering 11→12 forces a
lockstep change in `yadgar/tests/scripts/test_v5_46_0_yadgar_setup_chain_equivalence.py`.
Reading it (119 lines, 4 tests) that is **not** what it pins:

| Test | Asserts | Line |
| --- | --- | --- |
| `test_setup_sh_dryrun_covers_make_setup_chain` | the lowercase substrings `detect, pull, secrets, hooks, agent, config, rules, anchor` each appear somewhere in `--dryrun` output | `:21-49` |
| `test_setup_sh_unit_generation_step_present` | `"systemd"` or `"launchd"` appears | `:52-67` |
| `test_setup_sh_and_make_agree_on_linger_step` | `"linger"` appears in **both** `yadgar-setup.sh --dryrun` and `make -n _enable-units-auto` | `:70-102` |
| `test_setup_sh_dryrun_exits_clean` | exit code 0 | `:105-119` |

No step count, no `Step N/11` string, no ordering. Renumbering is a cosmetic edit
to log strings (`:377, 396, 413, 453, 556, 598, 608, 613, 630, 705, 730`) and the
`--help` block (`:108-119`). The genuine lockstep constraint is the **linger-guard
precedent** (`:70-102`): a drift guard of that shape asserts a shared token on
*both* surfaces, which makes the `Makefile` edit mandatory rather than optional.
That guard is the mechanism that stops this class of divergence recurring, and
this car should add its code_graph twin.

---

## The fork: duplicate in bash (A) vs. call the Python surface (B)

### Recommendation: **B**, via a new `yadgar code-graph install` subcommand.

**Decisive argument — the wheel bundle check.** `yadgar-setup.sh:38-46` defines
`_REQUIRED_HELPERS` and fail-fasts with **exit 2** when any helper file is absent
(`:54-66`). Option A means a new bash helper in that list, so every pipx install
that picks up a new `yadgar-setup.sh` without the matching helper hard-fails at
startup — the precise packaging failure mode that check was added for
(`:29-35`, v5.46.10). Under B, `_REQUIRED_HELPERS` is untouched: the step is one
more `yadgar <subcommand>` line, and steps 6–11 are *already* six such lines.

**Supporting arguments for B:**

- A would fork the pinned SHA-256 table (4 hashes, `codebase_memory_mcp.py:53-66`),
  the OS/arch asset matrix (`:108-133`), tarball verification (`:147-162`), and
  extraction (`:224-243`) into bash. `scripts/sync_version.py:54` already carries a
  comment about keeping `flake.nix`'s `_cbm_version` in sync — a **third** copy of
  a security-relevant pin is a drift surface, not DRY pedantry.
- `install_codebase_memory_mcp(skip_if_exists=True)` is already idempotent and
  offline-tolerant — exactly the shell chain's contract (`yadgar-setup.sh:23-24`).
- Under B the equivalence test becomes trivially true: both surfaces call the same
  function, so "equivalence" is structural rather than asserted.

**Rejected: A.** Only advantage would be zero Python dependency — irrelevant, the
shell chain already requires the `yadgar` CLI for steps 6–11 and would die at step
6 without it.

### Rejected: make `yadgar-setup.sh` invoke `yadgar setup` outright

Not feasible, and the reason is a **mechanism conflict**, not philosophy.
`cmd_setup` (`cli/setup.py:261-373`) does: Docker check → XDG dirs → write
`config.yaml` → **generate `secrets.env`** → `_maybe_install_code_graph` → Claude
Code MCP registration → print next steps. The shell chain already owns three of
those through *different* mechanisms:

| Concern | shell chain | `yadgar setup` |
| --- | --- | --- |
| secrets.env | `bootstrap_secrets.sh` (step 3, `:412-426`) | `_render_secrets_env` + `secrets_path.write_text` (`:300-315`) |
| config.yaml | `yadgar config init` + `config sync` (step 8, `:612-621`) | `cmd_config_init` (`:286-296`) |
| XDG dirs | `_step_pre_create_dirs` (`:506-520`, chmod 700) | `mkdir(parents, exist_ok)` (`:281-283`) |
| MCP registration | `yadgar install --client claude-code` (step 6) | `_register_claude_code_mcp` (`:206-258`) |

Chaining them would double-write secrets and config with two different writers.
The two surfaces have genuinely diverged: `yadgar setup` is the *no-repo, no-units*
minimal bootstrap; `yadgar-setup.sh` is the *full* installer. Converging them is a
separate, much larger effort — explicitly out of scope here.

---

## Ordering — the shell surface can do the persist half BETTER than Python

`_persist_code_graph_enable` / `_persist_code_graph_disable`
(`cli/setup.py:100-144`) POST to `/api/runtime-config/{key}` via
`runtime_config_client.set` (`:104-141`). `cmd_setup` normally runs **before**
`yadgar daemon start`, so that write reliably fails — its own docstring concedes
this (`cli/setup.py:164-169`).

The shell chain does not have that problem. Place the new step **after**
`_step_enable_units` (`:555`) — by then the daemon is normally live, so the persist
can actually land. This car is not the shell surface catching up — it is the shell
surface doing the half the Python surface structurally cannot.

**But do NOT copy steps 10/11's `_wait_for_daemon` skip-gate.** Their pattern is
*skip the whole step* on timeout (`:717-724`, `:731-738`: `warn` then `return 0`).
Applied here that would mean **daemon down → no binary installed → the exact
divergence this car exists to fix survives**. The binary install needs no daemon;
only the persist does, and `provision_code_graph` already fails soft on the persist
(`cli/setup.py:114-121`, `:138-143`). So the step calls it **unconditionally**, with
no health gate. Side benefit: no third 120s stall on a machine whose daemon is
broken.

### But the bearer token blocks it today

- `/api/` is in `_PROTECTED_PREFIXES` (`yadgar/core/auth_middleware/auth_middleware.py:34`).
- `runtime_config_client.set` calls `_apply_auth` (`:128`), which reads
  `os.environ["YADGAR_MCP_AUTH_TOKEN"]` **only** (`:99-104`).
- The shell installer never sources `secrets.env` — README tells users to do that
  *after* install (`README.md:193`).

So the POST 401s and `set()` returns `False`. Split by path:

| Path | Outcome without a token fix |
| --- | --- |
| default (install) | **benign** — `code_graph.enabled` already defaults `true` with no row, so binary-installed + no persist is coherent. The reported divergence is fixed by the binary install alone. |
| `--no-code-graph` | **broken** — the `false` row never lands, so the opt-out leaves the feature ON with no binary: the original bug, inverted. |

### And a live persist introduces a NEW hazard: clobbering a deliberate opt-out

`_persist_code_graph_enable` writes `true` at **global** scope unconditionally
(`cli/setup.py:111`). Today that write almost always fails (daemon down at
`yadgar setup` time), so it is inert. Making the persist actually work — the point
of the ordering above — makes it live: every re-run of the deliberately idempotent
installer (`yadgar-setup.sh:23-24`) would silently resurrect code_graph for a user
who ran `config_set("code_graph.enabled", false, scope="global")`. Per-repo
overrides survive (per-dir beats global, ADR-0163); a **global** opt-out does not.
`cli/setup.py:106-107` names this row but only frames a *failed* write as benign —
it never considers a *successful* write overwriting an intentional opt-out.

Fix: read-before-write — `runtime_config_client.get(_CODE_GRAPH_KEY, default=<sentinel>)`
and persist `true` only when no explicit global `false` row exists (~3 lines, and
`get` is already fail-open so a daemon-down read degrades to "write it"). See open
decision 7.

Two existing call sites already solve the token problem:
`mcp_register.resolve_mcp_auth_token()` (`:83-111`, env → `secrets.env` → `""`) and
`seed.py:_read_auth_token` (`:41-60`, the same pattern hand-rolled). Giving
`runtime_config_client._apply_auth` the same fallback is ~5 lines, makes the shell
opt-out real, and repairs `yadgar setup`'s persist on any machine where the daemon
happens to be up. **Recommended: include it in this car** (see open decision 1).

---

## The car

**One car. Primary seam: `yadgar/core/cli/code_graph.py` (new `install` subcommand)
+ `scripts/install/yadgar-setup.sh` (new step).**

### Phase 1 — extract the provisioning logic to a shared home (behavior-preserving)

New module `yadgar/core/install/code_graph_provision.py` exposing one function,
e.g. `provision_code_graph(*, opt_out: bool) -> bool`. Move verbatim from
`cli/setup.py`: `_CODE_GRAPH_KEY` (`:43`), `_resolve_code_graph_action` (`:46-64`),
`_do_install_code_graph` (`:67-97`), `_persist_code_graph_enable` (`:100-121`),
`_persist_code_graph_disable` (`:124-144`), `_maybe_install_code_graph` (`:147-180`).

`cli/setup.py:325` keeps calling a thin delegate — no behavior change, no logic
edit. Deliberately a pure move: `cli/setup.py` is sibling-car territory (`7cd74ea0`,
merged), and a move is the smallest possible collision surface.

Rationale for a new module rather than growing `codebase_memory_mcp.py`: that
module is a pinned *downloader* (docstring `:1-33`); provisioning additionally
writes runtime config and prints operator guidance. Keeping the downloader pure
keeps its pin table auditable.

### Phase 2 — `yadgar code-graph install` subcommand

`cli/code_graph.py:213-253` today registers `index` / `query` / `refresh` and has no
`install`. Add `p_install` + a handler that calls `provision_code_graph`. Surface:
`--no-code-graph` (mirroring `yadgar setup`, `cli/setup.py:382-395`) so both
entry points share one flag vocabulary.

Dispatch detail (verified, `:185-213`): `p.set_defaults(func=cmd_code_graph)` is on
the **parent** parser, so subcommands are dispatched by an `if cg_command == …`
chain inside `cmd_code_graph` (`:199-208`). Two consequences:

- `install` needs a **branch in that chain**, not just a subparser — otherwise it
  falls into the `else` and exits 1 with "specify a subcommand".
- `cmd_code_graph` resolves+validates `args.repo` before dispatching (`:189-192`)
  and wraps the chain in `except CodeGraphError → _die_binary_missing` (`:209-213`).
  It does **not** resolve the binary pre-dispatch, so `install` is safe on a machine
  with no binary — but the `install` branch must not route through the runner, and
  should short-circuit ahead of any repo-path requirement (`install` takes no repo).

Explicitly NOT: `python -c "from yadgar.core.cli.setup import _maybe_install_code_graph"`
from bash — private API plus the `_get_venv_python` shebang dance
(`yadgar-setup.sh:172-177`).

### Phase 3 — the shell step

Append as **step 12** in `main()` (after `_step_seed_agent_prompts`, `:824`) rather
than inserting mid-chain: the daemon is already warm from steps 10/11's
`_wait_for_daemon`, and the diff touches no existing step body. All eleven `Step
N/11` labels become `N/12`; the `--help` block (`:108-119`) gains line 12.

```
_step_code_graph() {
    log "Step 12/12: Provisioning code_graph (codebase-memory-mcp)..."
    # feature-probe, mirroring _step_install_rules:631
    ... run yadgar code-graph install [--no-code-graph]
}
```

Required properties:

- **Feature-probe before calling**, mirroring `_step_install_rules:631`
  (`yadgar install --help | grep -q -- '--rules'`). A staged upgrade can have a new
  `yadgar-setup.sh` against an older installed `yadgar` with no `code-graph install`
  subcommand — warn and skip, never abort.
- **`--dryrun` safe** — go through `run` (`:140-147`) so nothing downloads and
  `test_setup_sh_dryrun_exits_clean` still passes.
- **Never aborts the install** — the script runs under `set -euo pipefail`; a
  failure here must be non-fatal (the `|| true` pattern at `:552` is the precedent).
- New flag `--no-code-graph` in the parse block (`:82-131`) + `--help` text.

### Phase 4 — Makefile parity

Add the same step to the `setup:` chain (`Makefile:209-247`, after `seed-anchors`)
plus a knob mirroring the linger precedent (`Makefile:33-40`):
`YADGAR_CODE_GRAPH ?= 1`, with `0` substituting an echo-skip. Without this, the
Phase-5 drift guard cannot assert both surfaces.

### Phase 5 — the drift guard

Two precedents from THIS train, both already merged — extend them, don't duplicate:

- `yadgar/tests/scripts/test_v5_169_setup_linger.py` — the per-car file. Its
  `test_c7_make_setup_reaches_linger_step` / `test_c7_make_setup_opt_out_propagates_to_submake`
  (added by `12f729a6`) already prove `make -n setup` recurses through
  `$(MAKE) _enable-units-auto` and that the `YADGAR_ENABLE_LINGER=0` opt-out
  survives the sub-make hop. Mirror that file as `test_v5_169_setup_code_graph.py`
  (including the opt-out-propagation twin for `YADGAR_CODE_GRAPH=0`).
- `test_v5_46_0_yadgar_setup_chain_equivalence.py` — add one twin of
  `test_setup_sh_and_make_agree_on_linger_step` (`:70-102`) asserting a shared token
  in both `yadgar-setup.sh --dryrun` and `make -n setup`.

**Pin the guard on the CLI invocation token `code-graph` (hyphen)** — it appears in
`[dryrun] yadgar code-graph install` and in the make recipe line. Do NOT pin on
prose or on `code_graph` (underscore): the proposed log line uses the underscore
form, so a naive shared-token pick would be fragile. The linger guard's own
docstring (`:74-77`) makes exactly this argument. Optionally add the token to
`REQUIRED_BUILDING_BLOCKS` (`:21-30`).

### Phase 6 — docs

`README.md:69` and `:183` describe code_graph provisioning as a `yadgar setup`
property; `:156` claims "All paths reach the same `yadgar setup` post-install step",
which is false for the repo-checkout path (`make setup`) and for `yadgar-setup`.
Correct those lines to name the installer surfaces accurately.

---

## Files touched, and sibling-car overlap

| File | Change | Overlap |
| --- | --- | --- |
| `yadgar/core/install/code_graph_provision.py` | **new** — shared provisioning fn | `core/install/` is `7cd74ea0` territory (merged); new file, no line collision |
| `yadgar/core/cli/code_graph.py` | `install` subparser + handler | none |
| `yadgar/core/cli/setup.py` | helpers moved out; `cmd_setup:325` delegates | **`7cd74ea0` (merged)** — keep it a pure move, zero logic change |
| `yadgar/core/runtime_config_client.py` | token fallback in `_apply_auth` (open decision 1) | none |
| `scripts/install/yadgar-setup.sh` | new `_step_code_graph`, `--no-code-graph` flag, `--help`, N/11→N/12 | **`bb237101` (merged)** touched `_run_enable_linger` (`:529-553`) / `_step_enable_units` (`:555-595`); the new step lands after `:824`, and the flag block edit (`:82-131`) is adjacent to but not on the `--no-enable-linger` line (`:89`) |
| `Makefile` | step in `setup:` + `YADGAR_CODE_GRAPH` knob | **`bb237101` (merged)** owns `LINGER_STEP` (`:33-40`) and `_enable-units-auto` (`:179`); the new knob mirrors that block, the new step lands in `setup:` after `:245` |
| `yadgar/tests/scripts/test_v5_46_0_yadgar_setup_chain_equivalence.py` | one new both-surfaces drift guard | none |
| `yadgar/tests/scripts/test_v5_169_setup_code_graph.py` | **new** — per-car test file, mirrors `test_v5_169_setup_linger.py` | none (sibling car owns the linger file; this is a separate file) |
| `README.md` | correct install-surface claims | none |

**Explicitly out of scope:**

- **Nix.** `yadgar-setup.sh:391` hard-dies on `linux-nixos`, and `flake.nix:171-173`
  already provisions `packages.codebase-memory-mcp`. The nix path has no gap.
- **`make setup` is missing `seed-agent-prompts`** (`Makefile:239-245` ends at
  `seed-anchors`; the shell path has step 11). Pre-existing drift, observed while
  scoping. Named here so the car does not grow to swallow it.
- **Converging `yadgar setup` and `yadgar-setup.sh`** — see the rejected option above.

---

## Acceptance criteria

- **[unit]** `provision_code_graph(opt_out=False)` on a mocked-successful install →
  binary install attempted + `code_graph.enabled=true` persisted.
- **[unit]** `provision_code_graph(opt_out=True)` → no install attempted +
  `code_graph.enabled=false` persisted.
- **[unit]** a failed binary install (raises) → no exception escapes, and `false` is
  persisted (parity with `cli/setup.py:177-180`).
- **[unit]** the moved functions' printed output is unchanged for both paths
  (characterization on `provision_code_graph` alone — NOT on `cmd_setup`, which also
  writes `secrets.env`, mkdirs, and merges `~/.claude.json`; the real regression
  surface of Phase 1 is import wiring, not text).
- **[unit]** (if open decision 7 = read-before-write) an existing explicit global
  `code_graph.enabled=false` is NOT overwritten by a default install re-run.
- **[unit]** `yadgar code-graph install --help` lists `--no-code-graph`.
- **[unit]** (if open decision 1 = yes) `runtime_config_client.set` attaches the
  bearer header resolved from `secrets.env` when the env var is unset.
- **[unit]** the new drift guard passes: the shared token appears in both
  `yadgar-setup.sh --dryrun` and the `make -n` output; and FAILS when the step is
  removed from either surface (assert the guard actually bites).
- **[e2e]** `bash scripts/install/yadgar-setup.sh --dryrun` exits 0, prints the new
  step, downloads nothing, and all four pre-existing tests in
  `test_v5_46_0_yadgar_setup_chain_equivalence.py` still pass.
- **[e2e]** the step is skipped with a warning (not an abort) when
  `yadgar code-graph install` is unavailable (simulate via a stubbed `yadgar` on PATH).
- **[e2e]** with the daemon DOWN, the step still attempts the binary install (no
  `_wait_for_daemon` skip) and the installer still exits 0 — pins the Phase 3 rule.
- **[unit]** `make -n setup` reaches the `code-graph` token, and
  `make -n setup YADGAR_CODE_GRAPH=0` does not (sub-make propagation — mirrors
  `test_v5_169_setup_linger.py::test_c7_make_setup_opt_out_propagates_to_submake`).
- **[manual]** fresh VM, `yadgar-setup`: `~/.local/bin/codebase-memory-mcp` exists and
  `config_get("code_graph.enabled")` is `true` (or has no row, which resolves `true`).
- **[manual]** fresh VM, `yadgar-setup --no-code-graph`: binary absent AND
  `config_get("code_graph.enabled")` returns `false` — this is the criterion that
  fails without open decision 1.
- **[manual]** `make setup YADGAR_CODE_GRAPH=0`: binary absent, `false` row present.
- **[manual]** re-run either installer: no re-download (`skip_if_exists=True`),
  no error.

---

## Risks

| Risk | Severity | Mitigation |
| --- | --- | --- |
| Opt-out silently no-ops when the token can't be resolved | **High** — inverts the bug | Open decision 1; if deferred, the shell step must PRINT the manual `config_set` remediation, as `_persist_code_graph_disable:138-143` already does |
| A working persist clobbers a deliberate global `code_graph.enabled=false` on every installer re-run | **High** — installer stops being idempotent, becomes destructive | Open decision 7 — read-before-write in `_persist_code_graph_enable` (~3 lines). Latent today only because the write usually fails |
| A `_wait_for_daemon` gate copied from steps 10/11 would skip the binary install when the daemon is down | **High** — ships the bug it fixes | Called out in Phase 3: unconditional call, no health gate. Pin with the `[e2e]` daemon-down criterion |
| Moving helpers collides with sibling car `7cd74ea0` | Medium | Pure move, no logic edit; characterization test on `cmd_setup` output |
| A ~large binary download lands in the install path by default | Medium | Already true for `yadgar setup` since `7cd74ea0` — this car makes the surfaces consistent, it does not introduce the policy. `--no-code-graph` / `YADGAR_CODE_GRAPH=0` are the documented outs |
| Staged upgrade: new script, old CLI without `code-graph install` | Medium | Feature-probe + skip (Phase 3), the `_step_install_rules:631` precedent |
| Binary download failure aborts an otherwise-good install | Medium | Non-fatal step; `provision_code_graph` already swallows install exceptions (`cli/setup.py:84-93`) |
| `--dryrun` accidentally performs a real download | Low | Route through `run`; asserted by the `[e2e]` dryrun criterion |
| Renumbering N/11→N/12 misses a label | Low | Grep `Step [0-9]*/11` to zero; no test pins the numbers |

---

## Open decisions for the user

1. **Include the `runtime_config_client` token fallback in this car?**
   *Recommend: YES.* ~5 lines, third instance of a pattern that already exists twice
   (`mcp_register.py:83-111`, `seed.py:41-60`). Without it `--no-code-graph` /
   `YADGAR_CODE_GRAPH=0` cannot persist `false` from the shell path, and the
   `[manual]` opt-out criterion cannot pass.
   *Alternative:* ship binary-install-only and document the opt-out as
   degraded-with-a-manual-step — smaller car, known-broken opt-out.

2. **Step position: last (12) or mid-chain (new 6, renumber 6→7…11→12)?**
   *Recommend: LAST.* Daemon already warm from steps 10/11; no existing step body
   moves. *Counter-argument:* a several-hundred-MB download at the very end delays
   the "complete" message.

3. **Does the `Makefile` get the step too?**
   *Recommend: YES.* Required for the Phase-5 both-surfaces drift guard — which is
   the only mechanism that prevents this exact divergence recurring a third time.
   *Alternative:* shell-only, guard asserts one surface, `make setup` stays divergent.

4. **Flag/knob naming.** `--no-code-graph` (shell, mirrors `yadgar setup`) +
   `YADGAR_CODE_GRAPH=0` (make, mirrors `YADGAR_ENABLE_LINGER=0`, `Makefile:33-40`).
   Confirm, or name them otherwise.

5. **Should `yadgar code-graph install` also persist the enable flag,** or be a
   pure binary installer with the persist left to `setup`?
   *Recommend: persist.* One function, one coherent outcome — the whole point of
   `7cd74ea0` is that binary and flag never disagree. A pure installer would
   re-open that gap for anyone who runs the subcommand directly.

6. **README correction scope (Phase 6)** — fix the three inaccurate lines only, or
   restructure the install section to name the three surfaces (`yadgar setup`,
   `yadgar-setup`, `make setup`) and what each actually does?
   *Recommend: the three lines only*, to keep the car bounded.

7. **Does the default (enable) path read before it writes?**
   *Recommend: YES — read-before-write.* Without it, a working persist makes every
   installer re-run silently undo a global `config_set("code_graph.enabled", false)`.
   ~3 lines, and it is the difference between an idempotent installer and a
   destructive one. *Alternative:* accept the clobber and document "a global opt-out
   must be re-applied after each install" — which contradicts
   `yadgar-setup.sh:23-24`'s idempotency contract.
