# Vacuum side-build branch selection must be deterministic, not inherited-PATH-dependent

**Date:** 2026-08-01
**Task:** #0107 (the vacuum unit inherits the systemd user-manager PATH, so which side-build branch runs is a coin flip per host)
**Status:** DRAFT — not started.
**Target train:** `feat/v5.172-bug-train`.
**Binding neighbour:** **ADR-0186**, whose *consequences* section leaves this exact
question open by name: "Which branch a given host takes is still decided by
inherited systemd environment (task 0107), so the same host can silently switch
launchers across reboots."

---

## 0. Verdict up front

The vacuum's side-build has two branches — a host `surreal` binary and a one-shot
backend container — and **which one runs is decided by an environment variable the
unit never sets.** On the fresh Debian VM the systemd user-manager PATH is
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`, which excludes `~/.local/bin`
where pipx installs. So a host with a perfectly good `surreal` on the interactive
PATH silently takes the container branch under the timer — or, on a host with
neither reachable, takes the `_SKIP_NO_SURREAL` exit and reclaims nothing forever
with a green timer.

**Decision: fix it in `launcher.py` (env-independent resolution) plus an explicit
config knob for the branch. Do NOT fix it by writing `Environment=PATH=` into the
units.** Rationale in §2.3 — the short version is that a unit-side fix reaches only
what this repo renders, and the host that exhibited the bug is not the only surface.

---

## 1. Problem statement — with evidence

### 1.1 Three PATH-dependent resolution points, one question

```
yadgar/core/vacuum/__init__.py:1308         binary = shutil.which("surreal")        # preflight/log
yadgar/core/vacuum/launcher.py:404          if shutil.which("surreal") is not None:  # THE branch decision
yadgar/core/_surreal_runner/_surreal_runner.py:114   subprocess.Popen(["surreal", ...]) # the actual spawn
```

All three ask "is there a `surreal`?" against the ambient `PATH`, and all three
answer independently. `select_side_launcher` (`launcher.py:398-408`) is the one that
decides the branch:

```python
if shutil.which("surreal") is not None:
    return HostBinaryLauncher()
if backend_image_present():
    return ContainerLauncher()
return None
```

### 1.2 The units that never set PATH

| Surface | PATH set? | Evidence |
|---|---|---|
| `scripts/install/yadgar-vacuum.service.in` | **no** — only `YADGAR_DB_URL` + `YADGAR_DATA_DIR` | `:15-16` |
| `flake.nix` `systemd.user.services.yadgar-vacuum` | **no** — same two vars | `:578-581` |
| `scripts/install/launchd/com.openfantasy.yadgar-vacuum.plist.in` | **yes, explicitly** — homebrew (Intel + Apple Silicon) + `~/.local/bin`, with a comment saying it is for the pipx `yadgar` binary | `EnvironmentVariables/PATH` block |
| private nix module (out of repo) | unknown — user must confirm | — |

The macOS surface got this right because launchd has no PATH at all by default, so
the omission was fatal and therefore noticed. On systemd the inherited default is
*almost* enough — enough to start `yadgar` (the `ExecStart` is an absolute path,
`@VACUUM_EXEC@` from `_resolve_host_exec`, `generate_systemd.sh:168`), just not
enough to find `surreal`. It fails **silently and partially**, which is why it
survived.

### 1.3 Why this is a correctness problem, not a tidiness one

- **The two branches are not equivalent.** ADR-0186 is explicit that the container
  branch runs *the same binary the real backend will later use to open the store*,
  making builder/opener version skew structurally impossible. The host-binary branch
  has no version gate at all (`_has_surreal_binary` is an existence check by design,
  `__init__.py:1298-1299`). So the branch that runs decides whether a version-skew
  class of corruption is possible — and today that is decided by whether someone
  ran the vacuum from a shell or a timer.
- **The same host can flip across reboots**, per ADR-0186's own consequence line.
  A vacuum that worked in testing (interactive PATH → host binary) can take a
  different path in production (timer PATH → container), and a debugging session
  reads the wrong branch's behaviour.
- **The failure mode when neither resolves is a SKIP, i.e. exit 0.** `_SKIP_NO_SURREAL`
  (`__init__.py:128`, reason plumbed through `_log_vacuum_skip` at `:1358-1400`) is
  honest in its logging but green in its exit status — a broken install that never
  self-heals, presenting as a healthy timer.

---

## 2. The decided approach

### 2.1 Env-independent binary resolution (primary)

New `_resolve_surreal_binary() -> str | None` in `yadgar/core/vacuum/launcher.py`,
resolving in this order and returning an **absolute path**:

1. `YADGAR_SURREAL_BIN` if set and executable — the explicit operator override,
   and the escape hatch for any layout not covered below
2. `shutil.which("surreal")` — today's behaviour, so a nix/dev host is bit-for-bit
   unchanged
3. a fixed candidate list, checked for executability:
   `~/.local/bin/surreal` (pipx / the layout that broke), `/usr/local/bin/surreal`,
   `/opt/homebrew/bin/surreal`, `/usr/bin/surreal`

`select_side_launcher` calls it instead of `shutil.which`. `_has_surreal_binary`
(`__init__.py:1279-1315`) calls it too, so the preflight log line and the branch
decision can no longer disagree. `HostBinaryLauncher.start` passes the resolved
absolute path down, and `spawn_surreal` gains an optional `binary: str = "surreal"`
parameter so the Popen argv is the same resolved path rather than a second,
independent PATH lookup (default preserves every existing caller, including tests).

### 2.2 An explicit branch knob (secondary, and the part that makes it *declarable*)

New config knob `VACUUM_SIDE_LAUNCHER` ∈ `{auto, host, container}`, default `auto`:

- `auto` — today's semantics: resolved host binary first, container second, SKIP third
- `host` — host binary only; if it cannot be resolved, **fail loudly** rather than
  silently falling through to the container
- `container` — container only; ignore any host binary

Why this is worth the knob and not scope creep: ADR-0186 argues the container branch
is *structurally* safer (no version skew), but today the only way to select it is to
not have `surreal` on PATH — i.e. by absence, which is exactly the fragile signal
this car exists to remove. An operator who has read 0186 and wants the container
branch should be able to say so. It also makes the fresh-VM verification a positive
assertion ("pin `container`, confirm the container ran") rather than an inference
from an absence.

### 2.3 Alternatives rejected, with reasons

**(i) `Environment=PATH=...` in the systemd units.** Rejected as the *primary* fix:

- It reaches only what this repo renders. The private nix module is out of repo and
  would still inherit — and that is the surface running on the workstation.
- A literal PATH baked into a unit is precisely the host assumption that broke here.
  `~/.local/bin` is right for pipx-on-Debian, wrong for a nix profile, wrong for a
  system-wide pip install, wrong for Homebrew-on-Linux.
- It leaves the three-way duplication of §1.1 intact, so the preflight log and the
  branch decision can still disagree if anything ever sets PATH differently between
  them.
- It cannot express "prefer the container", which ADR-0186 says is the safer branch.

Not rejected as a *supplement*: if the user wants belt-and-braces, adding
`Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin` to the two systemd
templates is harmless once §2.1 has made it non-load-bearing. **Recommend not
adding it** — a PATH that is set but never relied upon is a maintenance liability
that reads as load-bearing to the next person.

**(ii) Ship a host-side `surreal` with yadgar.** Already rejected by ADR-0186 (turns
a coincidental version alignment into a permanent maintenance invariant tracking
`Dockerfile.backend`'s pin, plus a per-OS/arch download path). Do not re-litigate.

**(iii) Make the container branch unconditional.** Tempting given ADR-0186's
reasoning, but it would make every dev-box vacuum depend on a container runtime and
a pulled image, and would change behaviour on the nix hosts that have been running
the host branch successfully for months. `VACUUM_SIDE_LAUNCHER=container` gives the
same outcome opt-in.

---

## 3. Exact files and functions to change

| File | Change |
|---|---|
| `yadgar/core/vacuum/launcher.py` | new `_resolve_surreal_binary()`; `select_side_launcher` (`:398-408`) consumes it and honours `VACUUM_SIDE_LAUNCHER`; `HostBinaryLauncher.start` (`:172-182`) passes the resolved path. |
| `yadgar/core/_surreal_runner/_surreal_runner.py` | `spawn_surreal` (`:82-129`) gains `binary: str = "surreal"`; argv head becomes `binary`. Default-compatible. |
| `yadgar/core/vacuum/__init__.py` | `_has_surreal_binary` (`:1279-1315`) uses the shared resolver; extend the existing preflight log line (`:1311-1314`) to name **the chosen branch and how the binary was resolved** (`env override` / `PATH` / `candidate dir`), so every run is self-documenting. `_has_side_build_launcher` (`:1318-1354`) honours the knob when reporting the SKIP reason. |
| `yadgar/_shared/config/config.py` + `config_registry.py` + `config_yaml.py` | register `VACUUM_SIDE_LAUNCHER` (three-way sync is pre-commit-enforced). |
| `docs/reference/configuration.md` | row in the vacuum block (`:606-607`). |
| `docs/CHANGELOG.md` | behaviour note. |

**Explicitly NOT changed:** `scripts/install/yadgar-vacuum.service.in`, `flake.nix`,
the launchd plist. That is the point of choosing §2.1 over §2.3(i) — one code change
covers every surface, including the one this repo cannot edit.

---

## 4. The TDD story

**CI gating asymmetry.** `.forgejo/workflows/ci-pr.yaml` runs by directory:
`test-fast` = `yadgar/tests/{scripts,server,hooks,_meta,clients}/`, `test-shared` =
`yadgar/tests/_shared/`, `test-backend` = `yadgar/tests/backend/`, `test-core` =
`yadgar/tests/core/`. `yadgar/tests/integration/` is **not** gated in `ci-pr`.

Resolution/branch tests → `yadgar/tests/core/` (next to
`test_vacuum_side_launcher.py`, which already owns the launcher seam).
Unit-rendering tests → `yadgar/tests/scripts/` (the shell generators live there;
`test_vacuum_trigger_cross_generator.py` is the template).

### 4.1 RED first — `yadgar/tests/core/test_vacuum_binary_resolution.py`

1. **`test_resolves_from_local_bin_when_path_excludes_it`** — `monkeypatch.setenv("PATH", "/usr/bin:/bin")`,
   plant an executable at a faked `~/.local/bin/surreal`; assert
   `_resolve_surreal_binary()` returns that absolute path. **RED today** — this is
   the Debian VM failure, reproduced without a VM.
2. **`test_env_override_wins_over_path`** — `YADGAR_SURREAL_BIN` set to a different
   executable than the one on PATH; the override wins.
3. **`test_returns_none_when_nothing_resolves`** — empty PATH, no candidates, no
   override → `None`, and `select_side_launcher` falls to the container branch.
4. **`test_branch_is_identical_with_and_without_path`** — the determinism assertion
   the car is named for: with `YADGAR_SURREAL_BIN` set, `select_side_launcher()`
   returns the same launcher class whether PATH contains `surreal` or is empty.
5. **`test_spawn_uses_the_resolved_absolute_path`** — record the Popen argv; assert
   `argv[0]` is the resolved absolute path, not the bare string `"surreal"`. This
   closes the third resolution point and would otherwise stay silently PATH-bound.

### 4.2 RED first — knob behaviour, same file

6. `test_launcher_knob_container_ignores_a_present_host_binary`.
7. `test_launcher_knob_host_fails_loudly_when_unresolvable` — must **not** silently
   fall through to the container; assert the SKIP reason names the pin, so an
   operator who pinned `host` and typo'd the path learns it from the log.
8. `test_default_knob_is_auto_and_preserves_host_first` — the behaviour-preservation
   guard for nix/dev hosts.
9. Register the knob in `yadgar/tests/server/test_config_three_way_sync.py` and
   `test_config_default_values.py` — both are **separately gated CI steps**
   (`ci-pr.yaml:418-419`), so a knob missing there fails CI independently of the
   suite jobs.

### 4.3 The anti-recurrence guard — be honest about its strength

The repo's established shape for cross-surface invariants is
`yadgar/tests/scripts/test_vacuum_trigger_cross_generator.py` and
`test_backend_unit_queue_base_cross_generator.py`. Reuse it, but note what it can
and cannot prove here:

10. **`test_vacuum_units_do_not_rely_on_inherited_path`** in `yadgar/tests/scripts/` —
    render each surface's vacuum unit and assert its `ExecStart` is an **absolute
    path** on all three (true today; this guards the neighbouring regression where
    someone "simplifies" `@VACUUM_EXEC@` to a bare `yadgar`).

That is a weaker guard than the trigger-path test, and the plan should say so
plainly: once §2.1 lands, the units legitimately carry no PATH, so there is nothing
positive to assert about them. **The real anti-recurrence is tests 1, 4 and 5** —
they prove the code is env-independent, which is the property that matters. Do not
manufacture a stronger-looking generator test that asserts something the design
deliberately does not require.

---

## 5. Verification

**Local (proves the bug and the fix without a VM)**

1. `pytest yadgar/tests/core/test_vacuum_binary_resolution.py yadgar/tests/core/test_vacuum_side_launcher.py yadgar/tests/core/test_vacuum_preflight.py` green.
2. `pytest yadgar/tests/server/test_config_three_way_sync.py yadgar/tests/server/test_config_default_values.py`.
3. Pre-commit `check-config-three-way-sync`.

Tests 1 and 4 reproduce the Debian PATH exactly via `monkeypatch.setenv`, so the
defect itself needs no VM. What needs the VM is proving the *inherited environment*
is what we believe it is.

**Fresh VM — `192.168.122.101`**
(`sshpass -p 'Aa1234.' ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password root@192.168.122.101`)

4. **Confirm the premise:** `systemctl --user show-environment | grep ^PATH` and
   `systemctl --user show yadgar-vacuum.service -p Environment`. Expected today:
   PATH without `~/.local/bin`, and `Environment=` carrying only `YADGAR_DB_URL` +
   `YADGAR_DATA_DIR`. Record the literal output in the PR body — this is the
   observation the whole car rests on.
5. **Before the fix:** `systemctl --user start yadgar-vacuum.service`, then read the
   preflight line in the journal. It should be either the container-branch line
   (`__init__.py:1339-1343`) or the SKIP — never the host-binary line — even though
   `which surreal` succeeds in an interactive shell on the same host.
6. **After the fix:** the same run logs the host-binary branch with the resolved
   absolute path and the resolution source (`candidate dir`).
7. **Knob proof:** set `VACUUM_SIDE_LAUNCHER=container` in the runtime config, re-run,
   confirm the container branch runs and reaps its container per ADR-0186 (`rm -f`
   after reading the exit code — check no orphan container named after the run
   survives).
8. Both runs must end with a **retained swap** (exit 0, `saved_pct > 0`), not just a
   correct branch choice.

**Out of repo, user-owned**

9. Confirm the private nix `yadgar-vacuum` service's `Environment=` list. It is
   expected to mirror `flake.nix` (no PATH). After this car it does not need one —
   which is the whole argument for §2.1 over §2.3(i).

---

## 6. Rollback story

Clean revert. `_resolve_surreal_binary`'s step 2 **is** today's behaviour, so a host
whose `surreal` is on the unit's PATH resolves identically before and after; a
revert changes nothing for those hosts. Hosts that only worked because of the
candidate-dir fallback revert to taking the container branch (or the SKIP), which is
the pre-change state — degraded, not broken, and never data-unsafe: both branches
run the same verified side-build + exact-count gate, and a SKIP performs no
destructive operation at all (`__init__.py:1345-1353`).

The knob defaults to `auto`, so leaving it unset after a partial revert is inert.

---

## 7. ADRs

- **ADR-0186 (accepted) is the binding neighbour.** Its consequences section names
  task 0107 as an open question; this car **resolves** that consequence. The new ADR
  should say so explicitly and cite 0186 rather than superseding it — 0186's
  decisions (no `--rm`, entrypoint override, graceful-stop assertion, launcher seam)
  are all untouched.
- **New ADR required**, because §2.2 introduces a standing operational knob and
  §2.3 records a rejected alternative (unit-side PATH) that will otherwise be
  proposed again by the next person who reads the Debian journal. It should state:
  the three resolution points, why unit-side PATH loses to code-side resolution
  (out-of-repo reach + host-layout brittleness), and that version-compat gating
  between host binary and image is **still explicitly not done** (0186's position,
  unchanged).
- **ADR-0090** is why the branch matters at all (a half-flushed surrealkv dir is
  corrupt-on-reopen). Cite as context, do not supersede.
- Nothing here touches ADR-0178 or ADR-0076.

---

## 8. Ordering / dependencies vs the rest of the train

- **Fully independent of 0111, 0113 and 0046.** Different files
  (`launcher.py`, `_surreal_runner.py`, config registry) with one shared touch of
  `yadgar/core/vacuum/__init__.py` — but a different function
  (`_has_surreal_binary` / `_has_side_build_launcher`) from the three other cars
  (`_vacuum_finalize`, `cmd_vacuum_impl`, the reap helpers). Textual conflict risk
  is low; land it in any order.
- Practical sequencing note: this car **increases the number of hosts that actually
  reach Phase 3** (a host that was SKIPping now vacuums). That makes 0046's residue
  accounting and 0111/0113's downtime and write-gate behaviour observable on hosts
  where they were previously moot. If the train is being validated on the fresh VM,
  landing 0107 **first** makes the other three testable there at all.
- 0107 and 0046 both add a config knob; the three-way-sync pre-commit hook operates
  on the whole registry, so two cars adding knobs in parallel branches will conflict
  in `config_registry.py` / `config_yaml.py`. Trivial to resolve, but expect it.

---

## 9. Explicitly out of scope

- Version-compatibility gating between the host `surreal` and the backend image
  (ADR-0186 decided existence-check-only; do not quietly reverse it here).
- Shipping a `surreal` binary with yadgar (ADR-0186, rejected).
- Any change to `HostBinaryLauncher.stop_clean` / `ContainerLauncher.stop_clean` —
  the graceful-stop assertion is the swap's safety property and is not this car's
  business.
- Adding `Environment=PATH=` to the unit templates (§2.3(i)) — deliberately not done.
- The `_SKIP_NO_SURREAL` exit-0 semantics. A skip staying exit 0 is a separate
  argument (the timer going red for "this host cannot vacuum" is arguably right);
  this car narrows *when* it fires, it does not re-litigate the exit code.
