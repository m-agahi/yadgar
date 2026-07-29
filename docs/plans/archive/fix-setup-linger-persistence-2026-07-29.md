# Fix: install never enables systemd lingering — user units die at logout

**Date:** 2026-07-29
**Task:** task:0084
**Status:** DESIGN — awaiting user decisions (see [Open decisions](#open-decisions-for-the-user))
**Train:** `feat/v5.169-install-runtime-fixes` — scoped to ONE car
**Discovered:** fresh Debian 13 VM, v5.168.0 install QA (2026-07-29)

---

## Symptom (observed, not inferred)

On a fresh Debian 13 VM, `loginctl show-user root -p Linger` returns `Linger=no`
**both before and after** a successful install. Nothing in the repo ever calls
`loginctl`:

```
$ grep -rn "loginctl" .        # only docs/testing/upgrade-test.md:23 (a doc hint)
$ grep -rni "linger" --include="*.py" --include="*.sh" --include="*.nix" .
                               # zero hits in any install/runtime code path
```

## Consequence

All three yadgar units are **systemd user units** with `[Install]
WantedBy=default.target`:

| Template | `[Install]` | Line |
| --- | --- | --- |
| `scripts/install/yadgar.target.in` | `WantedBy=default.target` | `:6-7` |
| `scripts/install/yadgar.service.in` | `WantedBy=default.target` | `:40-41` |
| `scripts/install/yadgar-backend.service.in` | `WantedBy=default.target` | `:29-30` |

Without lingering, the per-user systemd manager (`user@$UID.service`) is torn
down when the user's last session ends, and is never started at boot. So the
units are *correctly enabled* and *still* do not persist:

- Installing user logs out → daemon dies.
- Host reboots → daemon never comes back.
- On a workstation with a permanent graphical session this is **invisible**.
- On a server / container host / headless VM — i.e. exactly the environment
  someone installs into over SSH — the install **silently does not persist**.
- CI cannot see this: no logind session, no reboot, no logout.

**Important:** the `[Install]` sections above are all present and correct. This
is a single missing step (linger), not a two-part unit-wiring bug. Verified by
grep before scoping — had `yadgar.target.in` lacked `[Install]`, this car would
have been two fixes.

---

## Where the gap actually lives — the dispatch premise is wrong

The task framing says "`yadgar setup` never enables lingering." That is true but
misleading about the seam: **`yadgar setup` never touches systemd at all.**

`cmd_setup` at `yadgar/core/cli/setup.py:261-373` does exactly: Docker check →
XDG dirs → `config.yaml` → `secrets.env` → `_maybe_install_code_graph` →
`_register_claude_code_mcp` → print next-steps. No `systemctl`, no unit
generation, no `loginctl`. The `yadgar-setup --help` text
(`scripts/install/yadgar-setup.sh:94-95`) confirms the division: the shell
installer "configures Yadgar for users installed via pipx, Homebrew, or nix
profile" and "Parallels `make setup`". The command run on the QA VM was the
**shell installer**, not the Python subcommand.

The unit-owning surfaces are all shell/Make:

| Surface | What it does | Where |
| --- | --- | --- |
| `scripts/install/generate_systemd.sh` | renders the 3 unit files from `.in` templates; **never enables, never lingers** | `:83-108` |
| `scripts/install/yadgar-setup.sh` `_step_enable_units()` | `systemctl --user daemon-reload` + `enable yadgar.target` + `start`/`restart` | `:511-550` |
| `Makefile` `enable-units` | `daemon-reload` + `enable --now yadgar.target` | `:136-142` |
| `Makefile` `_enable-units-auto` | OS-routed variant used by `make setup` | `:166-190` |

`yadgar-setup.sh`'s 11-step `main()` (`:746-777`) does **not** invoke `yadgar
setup` anywhere — confirmed. The two installers are genuinely separate surfaces
and both have the gap.

### Overlap with the merged sibling car — resolved, near-zero

The dispatch flagged HIGH OVERLAP RISK on `yadgar/core/cli/setup.py` and
`yadgar/core/install/`. Checked on this branch:

```
$ git diff --stat master..feat/v5.169-install-runtime-fixes -- \
      yadgar/core/cli/setup.py yadgar/core/install/ scripts/install/ Makefile
 yadgar/core/cli/setup.py                   | 230 +++++------  (117+/117-)
 yadgar/core/install/codebase_memory_mcp.py |   4 +-
```

The sibling car owns `_resolve_code_graph_action`, `_maybe_install_code_graph`,
the `cmd_setup` body, and `register()`. **This car touches none of them** — the
fix belongs in `scripts/install/` + `Makefile`, which the sibling car did not
modify at all. The flagged overlap dissolves once the seam is identified
correctly. Do not implement this against `setup.py` to match the dispatch's
phrasing.

---

## Prior art and constraints

### 1. Nix already does this — declaratively, on the headless host

`/home/max/git/nix/hosts/nixos-media/configuration.nix:47-51` (read-only):

```nix
# Enable lingering so the systemd user session (and home-manager user
# services such as spotifyd) starts at boot without a display manager login.
systemd.tmpfiles.rules = [
  "f /var/lib/systemd/linger/max  0644 root  root  -"
];
```

`modules/home/yadgar.nix` itself declares only `systemd.user.services.*` with
`Install.WantedBy = [ "default.target" ]` (`:473-474`, `:562-563`, `:615-616`)
— identical to the repo templates. The linger enablement lives one level up, in
the **host** config, and only on the headless machine. This is direct prior art
that the maintainer already hit this exact problem outside the repo and fixed it
by enabling linger — not by moving to system-scope units.

### 2. `loginctl enable-linger` for *your own user* needs no privilege

Read from `/run/current-system/sw/share/polkit-1/actions/org.freedesktop.login1.policy`:

| Action | `allow_any` | `allow_inactive` | `allow_active` |
| --- | --- | --- | --- |
| `org.freedesktop.login1.set-self-linger` | **yes** | **yes** | **yes** |
| `org.freedesktop.login1.set-user-linger` | `auth_admin_keep` | `auth_admin_keep` | `auth_admin_keep` |

systemd's logind picks `set-self-linger` when the target uid equals the caller's
uid. So `loginctl enable-linger` (self) needs **no sudo, no polkit auth, no TTY,
no interactive agent**. `set-user-linger` (someone *else's* user) is the
admin-gated one — and this car never needs it.

**The one real failure path:** polkit must be reachable. On a minimal image with
no `polkitd`, a non-root user's `enable-linger` is denied. Root always succeeds
(logind short-circuits uid 0 before the polkit check). The QA ran as **root** —
the trivially-permitted case — so the hard path is **untested**. The design must
tolerate denial without breaking the install.

### 3. `install_runtime.sh` is the flag-shape precedent — but read it correctly

`scripts/install/install_runtime.sh` already runs `sudo apt-get install -y
podman` (`:91-99`) — far more invasive than linger. Its gate
(`_is_noninteractive`, `:132-138`) defaults to **printing a hint, not
installing**, whenever `INSTALL_NONINTERACTIVE=1` or there is no TTY, with
explicit `--install-runtime` / `--no-install-runtime` overrides
(`yadgar-setup.sh:80-81, 330-334`).

Do **not** cite this as "the installer already mutates the host, so auto-enable
is fine." Cite it accurately: its default-to-hint exists because **`sudo` can
hang or fail without a TTY**. That constraint does not apply to auth-free
self-linger. Same flag *shape*; different default, for a stated reason.

### 4. No system-scope option exists anywhere

`grep -rn "etc/systemd/system|systemctl --system|LaunchDaemons"` over the repo
returns only two archived design docs. Every install path is per-user. There is
no `--system` flag to fall back on.

### 5. Nothing is documented

`README.md` has no linger mention. `docs/testing/upgrade-test.md:23` mentions
`loginctl show-session` only as a systemd-version precondition. So this is
**not** "documented but unenforced" — it is undocumented *and* unenforced, which
strengthens the case for the installer handling it rather than a README line.

### 6. No `set -e` in `yadgar-setup.sh`

Confirmed: `scripts/install/yadgar-setup.sh` has no `set -e`. A failing `run`
does not abort the install. This matches the required "never abort setup"
property without extra guarding — but it also means a silent linger failure
would go unnoticed unless we explicitly check and warn.

---

## Design options considered

| | Option | Verdict |
| --- | --- | --- |
| A | Installer runs `loginctl enable-linger` unconditionally, no flag | Rejected — no escape hatch for an operator who deliberately does not want it; violates the repo's own `--no-*` opt-out convention. |
| B | Detect + warn only, change nothing | Rejected as the *sole* answer — see below. Retained as the **failure-path** behaviour. |
| C | Prompt the user | **Disqualified.** The already-merged sibling car in this train deleted setup's only interactive prompt specifically to make setup unattended-safe (`setup.py:378-381` documents the removal). A new prompt re-breaks that property. |
| **D** | **Auto-enable by default, never prompt, `--no-enable-linger` opt-out, loud warn + exact remediation on failure** | **Recommended.** |
| E | Ship system-scope units | Rejected — see below. |

### Why not (B) alone

A warning fires at the end of a long install, on a host where nothing is
observably broken yet (the daemon *is* running — the session is still open). The
user reads it, the install "worked", they log out, and the failure surfaces
hours later disconnected from its cause. Yadgar's failure mode here is silent
data loss of intent, not a crash. A warning that has to survive a scrollback and
a mental "I'll do that later" is not a fix for a silent-persistence bug. It is
the right *fallback* when we genuinely cannot act, not the primary behaviour.

### Why not (E) system-scope units

Superficially attractive ("system units need no lingering"), but it is a much
larger and worse change:

- Requires root for the whole install, killing the current
  install-as-your-own-user story.
- Rootless podman is assumed throughout the unit templates (`--user root` inside
  the container, `%h`-relative paths, `EnvironmentFile=-%h/.local/state/...`
  at `generate_systemd.sh:90-99`). System units run as root with a different
  `XDG_RUNTIME_DIR`, a different podman socket, and a different data root.
- Needs a second template set, a second uninstall path, and a second doctor
  probe — the exact "5 generators" sprawl the sibling
  `fix-systemd-generate-missing-queue-base` work is already complaining about.
- The nix prior art chose linger over system-scope for the identical problem.

Listed as a rejected alternative, deliberately not a recommendation.

### macOS — explicit non-goal

An analogous gap does exist: `_step_enable_units` bootstraps into `gui/$(id -u)`
(`yadgar-setup.sh:543`, `Makefile:~155`), and the `gui/` domain requires a GUI
login session. There is no `enable-linger` equivalent; the only fix is moving to
`/Library/LaunchDaemons`, which
`docs/reports/releases/macos-launchd-port-design-2026-06-07.md:242` explicitly
rejects — per-user LaunchAgents are "the only correct choice" for podman/docker
socket access on macOS.
`docs/plans/archive/PLAN_V5_45_1_MACOS_LAUNCHD.md:49` records the same decision.

Headless macOS is not a target. **Out of scope for this car**; do not design for
it. One sentence in the plan; no code.

---

## Recommendation — Option (D)

Mirror the `install_runtime.sh` flag *shape*, with an **attempt-by-default**
policy justified by the auth-free self-linger evidence:

- **One flag, opt-out only**, on `yadgar-setup.sh`: `--no-enable-linger`.
  Attempting linger is the default; there is deliberately **no `--enable-linger`
  opt-in flag**. An opt-in flag for a default-on behaviour is a no-op — this is
  precisely the defect the sibling car in this train removed when it deleted
  `--code-graph` (`setup.py:378-381`: "once `code_graph.enabled` defaulted to
  True an opt-IN flag for a default-on feature was a no-op, and its existence
  pushed scripted installs onto `--no-code-graph`"). Do not re-introduce the
  same shape one car later.
- **Never prompts. Never reads stdin.** Preserves the sibling car's
  unattended-safe property.
- **Never aborts setup.** Linger failure is a warning, not a fatal.
- Idempotent: if `loginctl show-user "$USER" -p Linger` already reports
  `Linger=yes`, log and skip.
- On failure (no polkit, non-root, `loginctl` absent), emit a loud, exact
  remediation line — this is the (B) behaviour, retained as the fallback:

  ```
  WARN: could not enable systemd lingering for user 'alice'.
        yadgar's user units will NOT survive logout or start at boot.
        Fix with:  sudo loginctl enable-linger alice
        Skip this check next time with: yadgar-setup --no-enable-linger
  ```
- Skip entirely when `loginctl` is unavailable (non-systemd host, container
  without logind) — informational note, no warning noise.

### Rule-compliance argument (explicit, not hand-waved)

The no-auto-apply rule targets **shared infrastructure** and **privilege
escalation**. `loginctl enable-linger` for the invoking user is neither:

1. **No privilege escalation.** `set-self-linger` is `allow_any=yes` — no sudo,
   no polkit prompt, no admin auth. Verified from the policy file, not assumed.
2. **Not shared state.** It writes one file, `/var/lib/systemd/linger/$USER`,
   scoped to the invoking user's own session lifetime. No other user, service,
   or host is affected.
3. **Trivially reversible.** `loginctl disable-linger $USER`.
4. **Squarely inside the installer's declared job.** An installer that writes
   systemd unit files and runs `systemctl --user enable` has already committed
   to managing the user's service lifecycle. Enabling the one setting that makes
   that enablement *mean* what the user asked for is completing the stated
   contract, not exceeding it.
5. **The installer already does strictly more.** `install_runtime.sh` runs `sudo
   apt-get install -y podman`. Linger is a strict subset of that authority — and
   unlike podman install, it cannot hang waiting for a TTY.

Point 5 is the weakest of the five and must not be over-read: `install_runtime`
defaults to *hinting* in non-interactive mode. The difference in default is
justified by the TTY/sudo constraint (point 1), and that reasoning must be
written into the code comment, not just this plan.

**Standing agent-rule note:** this plan is design-only. Neither the implementing
agent nor CI may run `loginctl enable-linger` against a developer workstation or
any real host. The flag's behaviour is exercised via `--dryrun` and a mocked
`loginctl` on `PATH` (see acceptance criteria).

---

## The car

**One car. File seam:**

| File | Change |
| --- | --- |
| `scripts/install/yadgar-setup.sh` | new `--no-enable-linger` flag parsing (`:76-81` block) + `--help` text (`:83-111`); new `_step_enable_linger()` helper; call it from `_step_enable_units()` (`:511`) on the `linux\|linux-other` branch **before** `systemctl --user enable`; add a linger probe to `--doctor` (`:721`, the `linux\|linux-other` case) |
| `Makefile` | same linger step in `enable-units` (`:136`) and `_enable-units-auto` (`:166`) linux branches, gated by a `YADGAR_ENABLE_LINGER ?= 1` variable so `make setup YADGAR_ENABLE_LINGER=0` opts out |
| `scripts/install/generate_systemd.sh` | **no change** — it renders templates; enablement is not its job |
| `yadgar/core/cli/setup.py` | **no change** — does not own units (see overlap section) |
| `scripts/install/uninstall.sh` | **no change** — see non-goal below |
| `README.md` | one line in the install section stating that yadgar enables lingering and how to opt out |
| `yadgar/tests/scripts/test_v5_169_setup_linger.py` | new test module (see criteria) |

**Shared-helper consideration.** The linger logic appears in two places
(`yadgar-setup.sh` and `Makefile`). The repo's own DRY precedent is
`install_runtime.sh` — a standalone helper both surfaces call
(`yadgar-setup.sh:319-320` and the Makefile `install-runtime` target). Following
that precedent yields a fourth file, `scripts/install/enable_linger.sh`. This is
listed as an open decision below rather than assumed, because the logic is ~15
lines and a third script has its own carrying cost.

### Explicit non-goals for this car

- **`uninstall.sh` does not disable linger.** Linger is user-session policy that
  may serve unrelated services (the nix prior art enables it for `spotifyd`).
  Disabling it on yadgar uninstall could silently break something yadgar does
  not own. Asymmetric on purpose; documented in `uninstall.sh` as a comment so
  the wart is deliberate rather than forgotten.
- **macOS / launchd** — see above.
- **System-scope units** — see above.
- **NixOS** — `make setup` already refuses on NixOS (`Makefile:80`); nix users
  get linger from host config. No change.

---

## Acceptance criteria

### [unit] — `yadgar/tests/scripts/test_v5_169_setup_linger.py`

Two precedents, one per surface:

- **Shell installer** — `yadgar/tests/scripts/test_scripts_yadgar_setup_module.py`
  and `test_v5_45_generate_systemd.py`: subprocess + `YADGAR_TEST_*` env seams +
  a stub binary injected on `PATH`.
- **Makefile** — `yadgar/tests/scripts/test_v5_46_2_makefile_install_runtime.py`.
  Its `_make_dry_run()` helper (`:20-33`) runs `make -n <target>` from
  `REPO_ROOT` and asserts on the printed recipe text (e.g. `:66-72` asserts
  `"install_runtime.sh" in combined`). `make -n` prints `@`-prefixed recipes and
  GNU make propagates `-n` through `$(MAKE)` sub-invocations, so this is a real,
  runnable assertion target — **use this idiom, do not invent a new Makefile
  dryrun convention.**

Criteria:

1. `yadgar-setup --dryrun` prints a `loginctl enable-linger` command on Linux.
2. `yadgar-setup --dryrun --no-enable-linger` prints **no** `loginctl` command.
3. `--help` documents `--no-enable-linger`. Negative assertion: `--help` output
   contains **no** `--enable-linger` opt-in flag (guards the no-op-flag defect).
4. With a stub `loginctl` on `PATH` that reports `Linger=yes`, the step is
   skipped (idempotence) and says so.
5. With a stub `loginctl` that exits non-zero, the installer **continues** (exit
   code unchanged) and stderr contains both the literal string
   `enable-linger` and the user name — i.e. the remediation is actionable.
6. With `loginctl` absent from `PATH`, the step is skipped with an
   informational note and **no** warning.
7. **(Makefile)** `make -n enable-units` and `make -n _enable-units-auto` both
   print the linger invocation; with `YADGAR_ENABLE_LINGER=0` in the env
   neither does. Plus a static assertion that `Makefile` declares
   `YADGAR_ENABLE_LINGER ?= 1`.
8. Regression guard: `--dryrun` still prints `systemctl --user enable
   yadgar.target`, and `make -n enable-units` still prints `systemctl --user
   enable --now yadgar.target` — the linger step must not displace unit
   enablement on either surface.
9. **(drift guard, R5)** Added to
   `yadgar/tests/scripts/test_v5_46_0_yadgar_setup_chain_equivalence.py`: the
   linger step is present in **both** the `yadgar-setup.sh --dryrun` output and
   the `make -n _enable-units-auto` output. Fails if either surface gains or
   loses the step alone.
10. **(doctor probe)** `yadgar-setup --doctor` on Linux reports the current
    linger state. With a stub `loginctl` reporting `Linger=yes` the output says
    so and emits no warning; with `Linger=no` it warns and prints the exact
    `loginctl enable-linger <user>` remediation. Doctor **never mutates** linger
    state — assert the stub records no `enable-linger` invocation on the doctor
    path.
11. **(R6 timeout)** With a stub `loginctl` that sleeps past the configured
    timeout, the installer completes within a bounded wall-clock budget, exit
    code unchanged, and takes the same warn-and-continue path as criterion 5.
    Guards the "logind present but not running" container case.

### [e2e] — N/A, stated with reason

No e2e criterion is proposed. CI has no logind session, no second login, and no
reboot; `loginctl` calls there are meaningless. **This absence is the root cause
of the bug shipping** — an e2e that cannot observe the property is worse than
none. Recorded here explicitly so a future reader does not assume it was
overlooked.

### [manual] — fresh-VM QA (the only real proof)

Run on a fresh headless VM, as a **non-root** user (the untested path):

1. `loginctl show-user "$USER" -p Linger` → `Linger=no` (baseline).
2. Install via `yadgar-setup`.
3. `loginctl show-user "$USER" -p Linger` → **`Linger=yes`**.
4. `systemctl --user is-enabled yadgar.target` → `enabled`.
5. Log out fully; from a second connection confirm the container is still
   running (`podman ps` / `curl -sf localhost:8765/metrics`).
6. Reboot; without logging in, confirm the daemon came back.
7. Repeat 1-3 with `--no-enable-linger` → `Linger` unchanged, warning absent,
   install still succeeds.
8. Repeat on an image **without** `polkitd` as non-root → install completes,
   exit code 0, warning printed with the exact `sudo loginctl enable-linger`
   line.

---

## Risks

| # | Risk | Severity | Mitigation |
| --- | --- | --- | --- |
| R1 | Non-root user on a polkit-less image → linger denied. Untested (QA ran as root). | **High (likelihood)** / Low (impact) | Never fatal; loud warn with exact remediation. Manual criterion 8 covers it. |
| R2 | Operator objects to the installer changing host state without asking. | Medium | `--no-enable-linger`; README documents it; rationale in-code. Genuinely a judgement call → surfaced as open decision D1. |
| R3 | Enabling linger keeps containers running after logout on a shared/multi-user box — resource surprise. | Medium | This is the *intended* behaviour and the entire point; document it in the README line so it is a stated property, not a surprise. |
| R4 | Third helper script (`enable_linger.sh`) adds to generator sprawl the queue-base plan already flags. | Low | Open decision D2 — inline duplication is a defensible alternative at ~15 lines. |
| R5 | Makefile and shell installer drift apart (only one gets the step). | Medium | Unit criterion 7 covers the Makefile path; criterion 9 adds the explicit drift guard in `test_v5_46_0_yadgar_setup_chain_equivalence.py`. |
| R6 | `loginctl` exists but logind is not running (container) → hang or slow failure. | Low | Wrap with a short timeout; treat non-zero/timeout identically to R1. Unit criterion 11. |

---

## Open decisions for the user

**D1 — (the rule-compliance question) Is auto-enabling linger acceptable?**
Options: (A) unconditional, (B) warn-only, (C) prompt, (D) default-on with
`--no-enable-linger` opt-out.
**Recommendation: (D).** Argument in
[Rule-compliance argument](#rule-compliance-argument-explicit-not-hand-waved):
`set-self-linger` is polkit `allow_any=yes` (verified from the policy file), so
there is no privilege escalation; the write is `/var/lib/systemd/linger/$USER`,
not shared state; it is reversible in one command; and the installer already
runs `sudo apt-get install podman` under a flag. (C) is disqualified outright —
the sibling car in this same train just removed setup's last prompt to make it
unattended-safe. (B) alone is rejected because a scrollback warning does not fix
a silent-persistence bug. **If you disagree, (B) is the safe fallback and the
car shrinks to warn + doctor probe + README.**

**D2 — Shared helper or inline duplication?**
`scripts/install/enable_linger.sh` (matches the `install_runtime.sh` DRY
precedent, adds a 4th install script) vs. ~15 duplicated lines in
`yadgar-setup.sh` and `Makefile`. **Weak preference: shared helper**, because
R5 (drift) has already bitten this repo — but no strong opinion.

**D3 — Should `uninstall.sh` disable linger?**
Proposed: **no**, because linger may serve unrelated user services. Asymmetric
by design, commented in-code. Confirm or override.

**D4 — Default for `make setup`.**
Proposed `YADGAR_ENABLE_LINGER ?= 1` (matches the shell installer's default). An
alternative is defaulting the Makefile to `0` on the theory that `make setup`
implies a dev workstation where linger matters less. Proposed: keep the two
surfaces identical — divergent defaults between the two install paths is exactly
the class of bug this train is cleaning up.

**D5 — README wording.**
Should the README line be a neutral statement of behaviour ("yadgar enables
systemd lingering so the daemon survives logout; opt out with
`--no-enable-linger`") or a fuller note explaining the multi-user resource
implication (R3)? Proposed: the short neutral line plus one sentence on R3.
