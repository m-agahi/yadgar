# Plan: converge the systemd generators — one renderer for all 9 units, `generate_systemd.sh` renders nothing

**Date:** 2026-08-01 (rev 2026-08-01 — rescoped from 2-of-9 to ALL 9 by user decision)
**Task:** #0110
**ADR:** ADR-0190 (supersedes ADR-0189, which described the naive "shell calls Python"
direction that §1.3 proves would regress the data path). Touches ADR-0185, ADR-0187, ADR-0161.
**Depends on:** #0111 (must land FIRST — see §10)
**Status:** design proposed, not started. Riskiest car in the v5.172 train.

---

## 0. TL;DR

Two generators render systemd units and they do not agree. The decision: **one renderer, in
Python, as the single source of truth for all nine units.** At the end
`scripts/install/generate_systemd.sh` renders nothing and every `.in` template is retired.

The literal reading of that — "`generate_systemd.sh` calls `install_systemd_service`" — is a
**regression**, because the Python generator today emits strictly less than the templates do
(2 units vs 9; no SurrealDB `:8000` publish; no vacuum-trigger bind; named volume where the
templates use the host data dir). So the method is an **inversion of order, same end state**:

> **Absorb, then delegate.** The Python renderer reaches parity with every template *first*,
> verified by render-and-diff against the current `.in` output with an enumerated
> `INTENTIONAL_DELTAS` list. The wrapper flips **only after all nine units render at parity.**

The nine split into two very different jobs:

| | units | job |
|---|---|---|
| **divergent** | `yadgar.service`, `yadgar-backend.service` | **reconcile two renderers.** Content exists on both sides and disagrees — §1.3 is the diff |
| **greenfield** | `yadgar.target`, `yadgar-vacuum.{service,timer}`, `yadgar-vacuum-trigger.{path,service}`, `yadgar-nightly-cycle.{service,timer}` | **new renderer code.** No Python counterpart exists. Timers, a `.path` unit and a `.target` need vocabulary the current string-template renderer does not have (§4) |

Build order follows that split (§5), but the split is **no longer a scope limit**.

---

## 1. Problem

### 1.1 Three install surfaces, routinely conflated

| surface | what it is | units? | linger? | starts the daemon? |
|---|---|---|---|---|
| `yadgar setup` (`yadgar/core/cli/setup.py:165`) | Python subcommand — `config.yaml`, `secrets.env`, code_graph binary, MCP registration | **no** | **no** | **no** |
| `yadgar-setup` (`scripts/install/yadgar-setup.sh`, 12 steps) | the **documented** installer; step 4 shells out to `generate_systemd.sh:478-486`, step 5 enables linger + `systemctl --user enable --now yadgar.target` | **yes, 9** | yes | yes |
| `yadgar daemon install-service` (`yadgar/core/cli/daemon.py:178` → `yadgar/core/daemon/systemd.py:130`) | third generator | **yes, 2** | no | no |

Bug reports say "the installer" and mean any of the three. The names differ by one hyphen.

### 1.2 The two unit generators have drifted on readiness

`scripts/install/yadgar-backend.service.in:51`:

```
Type=simple
```

No `TimeoutStartSec`. No `--sdnotify`. No `--health-cmd`. No `--health-start-period`. The
template says so on purpose at `:26-27` — *"This unit stays Type=simple on BOTH runtimes — it has
no readiness contract to make conditional."*

`yadgar/core/daemon/systemd.py:112-126` renders, for the same unit:

* podman — `Type=notify` + `NotifyAccess=all` + `TimeoutStartSec=180` + `--sdnotify=healthy` +
  `--health-cmd` + `--health-start-period=60s`
* docker — `Type=exec` + `TimeoutStartSec=180` + a bounded `ExecStartPost` `/health` poll
  (ADR-0185)

**Scope the gap precisely: it is BACKEND-ONLY, not generator-wide.** `generate_systemd.sh:88-96`
does carry ADR-0185's `@PODMAN_ONLY@` / `@DOCKER_ONLY@` machinery, and the **core** template *is*
runtime-conditional (`yadgar.service.in:22` `Type=@SERVICE_TYPE@`, `:72` docker gate, `:78`
`TimeoutStartSec=120`). Only the backend template opted out. Reviewers who carry the wrong model
("the shell generator is pre-ADR-0185") will mis-scope the whole car.

**Consequence — ADR-0187's premise is false on the primary surface.** ADR-0187 justifies core
`TimeoutStartSec=120` on podman with: *"the core unit's `After=`/`Requires=yadgar-backend.service`
means the backend is already HEALTHY, so backend model load is not inside the core's budget."*
That holds only where the backend is `Type=notify`. Under `Type=simple`, systemd considers the
backend started the instant `podman run` **forks**, so on the shell path — the documented one —
the backend's cold model load falls **inside** the core's 120s budget, and the quantisation
argument in ADR-0187 never applied there at all.

### 1.3 The Python generator is a *subset*, not a variant — THE REGRESSION LIST

Verified by reading both. **These are the deltas a naive delegation would ship as data loss.**
They stay front and centre for the whole car; every one of them must appear in the converged
renderer or in `INTENTIONAL_DELTAS` with a reason.

| property | `.in` template | `systemd.py` | consequence of naive delegation |
|---|---|---|---|
| **SurrealDB publish** | `-p 127.0.0.1:@BACKEND_SURREAL_PORT@:8000` (`yadgar-backend.service.in:35`) | **absent** | the host-side vacuum + nightly units default `YADGAR_DB_URL=http://127.0.0.1:8000` (`yadgar-vacuum.service.in:15`, `yadgar-nightly-cycle.service.in:13`) and would connection-refuse |
| **vacuum trigger** | `-v @STATE_DIR@:/root/.local/state/yadgar` + `-e YADGAR_VACUUM_TRIGGER_PATH=…` (`yadgar.service.in:48,50`) | **absent** | `vacuum_now()` returns `skipped_reason="no_trigger_path_configured"`; nothing watches |
| **core `/data` mount** | host bind `@DATA_DIR@` (`yadgar.service.in:47`) | **named volume** `profile.volume_name` (`systemd.py:247`) | data lands somewhere else |
| **viz port** | `-p 127.0.0.1:42069:42069` (`yadgar.service.in:46`) | absent | viz unreachable |
| **image tag indirection** | `${YADGAR_IMAGE_TAG}` from `EnvironmentFile=-…/upgrade.env` (`yadgar.service.in:32,61`) | `profile.image_name`, fixed at render | the upgrade orchestrator's rewrite target disappears |
| **`ExecReload`** | `ExecReload=/bin/true` (`yadgar.service.in:75`) | absent | reserved reload seam gone |
| **`--stop-timeout 30`** | `yadgar.service.in:60`, `yadgar-backend.service.in:46` | absent | container SIGKILL window changes |
| **`--security-opt label=disable`** | both templates | absent | SELinux-enforcing hosts fail the mount |
| **`TimeoutStopSec=45`** | `yadgar.service.in:79` | absent | pinned by `test_systemd_unit_template.py:100` |
| **`ExecStartPre=mkdir -p @STATE_DIR@/triggers`** | `yadgar.service.in:42` | absent | podman does **not** auto-create a missing `-v` source; core fails to start if the dir was cleaned |
| **unit count** | 9 | 2 | `yadgar.target` is the *only* activation mechanism for the timers (`yadgar.target.in:4-19`) — losing it silently stops all maintenance |

Conversely `systemd.py` carries things the templates lack, which are **also** part of the union:
the HF cache mount (`systemd.py:169-172`), `-v {profile.volume_name}:/queue-data` +
`YADGAR_QUEUE_BASE=/queue-data` (the templates use `/data` for both — a genuine semantic
divergence that must be resolved, not merged), the `--health-cmd`, and the `dev` suffix arm.

**So "converge" is not "pick one file". It is "make one renderer emit the union, then delete the
other."**

### 1.4 Cosmetics observed on the fresh VM (folded into this car)

* `yadgar setup` prints `Checking Docker...  ✓ Docker 5.4.2` on a **docker-free podman** host.
  `yadgar/core/cli/setup.py:176-182` hardcodes the word Docker, but `YadgarDaemon.check_docker()`
  is a back-compat alias for `check_runtime()` (`yadgar/core/daemon/daemon.py:638-640`), so the
  version it returns is the *resolved* runtime's. `5.4.2` is podman. **It probes the right runtime
  and prints the wrong word** — a label fix, not a probe fix. Includes the failure branch at
  `:181-182`, which tells a podman-only user to install Docker Desktop.
* `scripts/install/yadgar.service.in:2` — `Description=Yadgar Memory Engine / MCP Server (Docker)`
  is unconditional, so a podman render is labelled Docker.
* `scripts/install/yadgar.service.in:62-71` — the ten-line comment block explaining the docker
  readiness gate survives the podman render, because only line `:72` carries the column-0
  `@DOCKER_ONLY@` marker that `generate_systemd.sh:95` deletes. Inert comment text (every line
  starts with `#`), so cosmetic — but it is exactly the kind of orphan that ADR-0185's *"an
  unanchored `/@DOCKER_ONLY@/d` … silently ate a prose comment"* note warns about, in the other
  direction.

The last two need **no edit** — those files are deleted. Say that in the commit rather than
touching doomed files. The `setup.py` one is independent of template retirement and is a
standalone fix.

---

## 2. Decision

**D1 — one renderer, in Python, for all nine units.** `yadgar/core/daemon/systemd.py` (or a module
it grows into, §4.4) becomes the single source of truth. Every `scripts/install/*.in` retires.
`generate_systemd.sh` survives only as an env-marshalling wrapper that **renders nothing**.

**D2 — absorb before delegate; the flip is gated on all nine.** The renderer must reach parity
*before* anything calls it. Acceptance is mechanical: capture the `.in` render as a baseline,
render the Python path with the same inputs, `diff`, require empty **modulo an enumerated
`INTENTIONAL_DELTAS` list**. ADR-0185's consequences already record this method — *"Podman
byte-identity was verified by rendering both generators before and after and diffing"* — so it is
house practice. **The wrapper flip (Stage D) does not begin until all nine units pass.** A partial
flip would leave a mixed-generation unit set, which is the one state with no clean recovery
(§9.3).

**D3 — the renderer takes a mode, not a fork.** `install_systemd_service` today is
profile-driven (`ContainerProfile`, `dev` suffix, `profile.port/image_name/volume_name`); the
shell path is env-driven and prod-only, and additionally renders seven units the profile path
never wanted. One renderer with an explicit input record — **not** two template families. Forking
would recreate, inside one module, the exact drift this car removes.

**D4 — the converged backend unit takes the readiness shape, i.e. the Python one wins there.**
`Type=notify` + `--sdnotify=healthy` + `--health-cmd` + `TimeoutStartSec=180` on podman;
`Type=exec` + gate on docker. A real behavioural change to the shell path — see §3.2.

**D5 — model the units as data, render once.** See §4.4. The seven greenfield units are what
forces this; f-string templates cannot express them safely.

### 2.1 Alternatives rejected

| option | verdict |
|---|---|
| **shell keeps `.in`, Python generator retired; `daemon install-service` shells out to `generate_systemd.sh`** | Rejected by the user. Also worse on merit: keeps `sed`-with-column-0-markers as the conditional mechanism, which ADR-0185 already documents as fragile, and puts the renderer outside the test surface CI gates by directory. |
| **keep both, add a sixth cross-generator invariant** | What the repo has done four times. Convergence removes the class instead of taxing it. |
| **generate the `.in` templates FROM Python at build time** | Codegen keeps two artifacts and adds a staleness check. All cost, no reduction in surfaces. |
| **naive delegation (shell calls today's `install_systemd_service`)** | Rejected — §1.3. Data-path and maintenance-path regression. This is what the superseded ADR-0189 described. |
| **converge the 2 divergent units only, leave 7 in the shell** | **Rejected by the user on rescope.** It leaves `generate_systemd.sh` a live renderer, so the "one source of truth" property is never actually reached and the wrapper keeps a `sed` path that will drift again. |
| **port the 7 first, the 2 last** | Rejected — the 2 are where the risk and the diff live; proving the parity harness on them is what de-risks the rest. |

---

## 3. What changes

### 3.1 Files

| file | change |
|---|---|
| `yadgar/core/daemon/systemd.py` | absorb the two service templates (§1.3); grow the unit model (§4.4) |
| **new** module for the seven greenfield units (sibling of `systemd.py`) | timers, `.path`, `.target`, host-CLI resolution |
| **new** entry point (CLI subcommand or module `main`) | what the wrapper invokes; takes the env contract at `generate_systemd.sh:5-22` |
| `scripts/install/generate_systemd.sh` | strip `render_template`, the `UNITS` array, the marker `sed` block, and the `_resolve_host_exec` block (ported to Python, §4.3). Keep: env contract, runtime detection call, the invocation, and the skew assertion (§7) |
| **all nine** `scripts/install/*.in` | **delete** (Stage D) |
| `yadgar/tests/_unit_render.py` | **Stage-D casualty.** `render_systemd` invokes `GENERATE_SYSTEMD_SH` (`:26,64`), which renders nothing after the flip. Either retarget it at the Python entry point, or keep it for `render_launchd` only and give the systemd side a new helper. Every caller across `yadgar/tests/scripts/` follows. **The parity baseline must be committed fixtures captured in Stage A** (§6.1) precisely because this helper does not survive |
| `scripts/install/uninstall.sh:109` | `SYSTEMD_UNITS=(…)` is a hand-maintained mirror of the generator's `UNITS` array. Repoint it (or its test) at the Python unit list — `generate_systemd.sh`'s *"Single source of truth for the unit set"* comment becomes false the moment it stops rendering |
| `yadgar/core/cli/setup.py:176-182` | print the resolved runtime's name via `check_runtime()`, both branches |
| `pyproject.toml:103` | unchanged — `scripts/install` → `share/yadgar/scripts` still ships the wrapper and the other helpers |

### 3.2 Behaviour changes the converged units introduce on the shell path

Not a refactor. Call these out in the ADR:

* the backend gains `--health-cmd` + `--health-start-period=60s` + `--sdnotify=healthy` — a
  container healthcheck it **never had**. ADR-0187's quantisation arithmetic (30s tick after a 60s
  grace ⇒ 90s is structurally too tight) did not apply to this path before and now does
* the backend gains `TimeoutStartSec=180` where it had none (systemd default 90s, but under
  `Type=simple` the timeout was never binding)
* the core's `TimeoutStartSec=120` becomes *justified* rather than accidentally-correct: ADR-0187's
  premise becomes true on this path for the first time
* ADR-0185's `@PODMAN_ONLY@` / `@DOCKER_ONLY@` column-0 marker mechanism is **removed**. The
  runtime conditional survives; its implementation becomes a Python branch
  (`_readiness_directives`) instead of anchored `sed`

---

## 4. The seven greenfield units — new renderer code, not diff-reconciliation

This is the part the earlier draft deferred. It is not more of the same work.

### 4.1 Shapes the current renderer has no vocabulary for

Read from the templates:

| unit | shape | why the current renderer cannot express it |
|---|---|---|
| `yadgar.target` | `[Unit]` with **`Wants=` written TWICE** (`:3` and `:19`), `After=`, `[Install] WantedBy=default.target` | systemd **unions** repeated directives. Any dict-keyed model silently drops one line — and the dropped one (`:19`) is what activates all three maintenance units. Highest-consequence trap in the car |
| `yadgar-vacuum.timer` | `[Timer]` `OnCalendar=Sun *-*-* 04:00:00` (**local**), `RandomizedDelaySec=30min`, `Persistent=true` | no `[Timer]` section exists in the renderer |
| `yadgar-nightly-cycle.timer` | `[Timer]` `OnCalendar=*-*-* 19:00:00 UTC` (**UTC**) | the two timers deliberately differ in timezone to match `flake.nix`; a shared "timer" helper that normalises them is a bug |
| `yadgar-vacuum-trigger.path` | `[Path]` `PathExists=@STATE_DIR@/triggers/vacuum_requested`, `[Install] WantedBy=paths.target` | no `[Path]` section |
| `yadgar-vacuum-trigger.service` | `Type=oneshot` with **two `ExecStart=` lines** (`rm -f …`, then `systemctl --user start …`) | multiple `ExecStart=` is legal **only** for `Type=oneshot`. A single-`ExecStart` string field cannot express it, and getting it wrong on a non-oneshot unit is a hard systemd parse error |
| `yadgar-vacuum.service` | `Type=oneshot`, `TimeoutStartSec=30min`, `EnvironmentFile=-`, three `Environment=` lines, **no `[Install]`** | time **spans** (`30min`, `1h`), not ints; optional-file leading `-`; and `[Install]` must be **omitted** |
| `yadgar-nightly-cycle.service` | `Type=oneshot`, `TimeoutStartSec=1h`, four `Environment=`, `ExecStart=@NIGHTLY_EXEC@` **bare**, no `[Install]` | the exec is invoked with **no arguments** — `nightly_cycle.main()` has no argparse (`yadgar-nightly-cycle.service.in:18-21`); any flag is an error or silently discarded |

Four of the nine units have **no `[Install]` section at all**, by design: they are started by their
timer/path, and `yadgar.target`'s `Wants=` is the activation mechanism (`yadgar.target.in:4-19`).
A renderer that always emits `[Install]` does not merely add noise — it changes what
`systemctl --user enable` pulls in, which is the exact class of bug `yadgar.target.in:4-8`
documents (*"renders correctly, passes every render assertion, and NEVER ACTIVATES"*).

### 4.2 The cross-unit invariant

`@STATE_DIR@` must be spelled **identically** in `yadgar.service`'s `-v` bind source
(`yadgar.service.in:48`) and in `yadgar-vacuum-trigger.path`'s `PathExists=`
(`yadgar-vacuum-trigger.path.in:14`). Both templates say so in prose, and
`test_vacuum_trigger_cross_generator.py` compares them as exact strings.

Once both are rendered by one Python module this stops being a string-comparison invariant and
becomes a **shared constant** — strictly stronger. Say so in the ADR; it is one of the car's real
wins, and it retires that test's systemd arm (§6.4).

### 4.3 Render-time host-CLI resolution must be ported, not dropped

`generate_systemd.sh:120-165` resolves `@VACUUM_EXEC@` / `@NIGHTLY_EXEC@` at render time through
`_resolve_host_exec`, in this order: explicit override env → `~/.local/bin/<script>` →
`command -v` → `python3 -I -m <module>`; failing all four, `_fail_no_host_cli` **aborts the
install** with an actionable message.

Two pieces of load-bearing logic to carry over verbatim in behaviour:

* **the `-I` is not optional.** `generate_systemd.sh:137-141`: *without `-I` the probe succeeds
  from inside a repo checkout even with nothing installed, and the unit — which runs from a
  different working directory — then fails at 4am.* Reproduce the isolation, and pin it with a
  test that runs the probe with cwd inside the repo and asserts it does **not** resolve.
* **the two entry points are different binaries.** `yadgar-nightly-cycle` is a console script;
  there is **no** `yadgar nightly-cycle` subcommand (`generate_systemd.sh:113-117`). A Python port
  is exactly where someone "tidies" this into one code path.

Fail-loud must survive the port: an unresolvable CLI aborts, it does not render a broken
`ExecStart`.

Also port the **DP5 nix-symlink guard** (`generate_systemd.sh:98-110`) — refuse when an existing
unit is a `/nix/store` symlink — and the two render-time side effects: seeding
`~/.local/state/yadgar/upgrade.env` (`:217-231`, must **not** overwrite an existing file) and
pre-creating `$STATE_DIR/triggers` (`:194`).

### 4.4 This argues for a unit model, not more string templating

**Recommended: a small ordered directive model.**

```
UnitFile   := name + [Section]
Section    := "[Unit]" | "[Service]" | "[Timer]" | "[Path]" | "[Install]"
              + ordered list of (key, value) pairs        # duplicates PRESERVED
```

Sections are an ordered list, `[Install]` is optional, directives are a **list of pairs, not a
dict**. Rendering is then one `render(UnitFile) -> str` function and the nine units become data.

Why this and not more f-strings:

* **duplicate directives** (`Wants=` ×2 in `yadgar.target`, `ExecStart=` ×2 in the trigger service)
  are representable and cannot be silently collapsed. A dict cannot express them; an f-string can,
  but nothing *checks* it
* **optional `[Install]`** becomes structural rather than a remembered omission
* the parity diff and `INTENTIONAL_DELTAS` become mechanical — compare structures, then text
* the wiki page `yadgar-install-surface-generators` records that f-string templates emitting unit
  text defeat AST-based guards (*"f-string chunk linenos land one line early on any directive
  following an interpolation"*, cars 0104/0105). A data model makes the line-scan detectors
  unnecessary for this generator: the directives are inspectable before they are text
* the `@STATE_DIR@` invariant (§4.2) becomes a shared constant

**Cost, stated honestly:** it is a bigger diff than f-strings for the two units that already exist,
and it churns `systemd.py` while ADR-0185's readiness logic lives there. Mitigation: build the
model in Stage A behind the parity harness, so the two existing units are re-expressed in it
**before** any behaviour changes. If the byte-diff on those two comes out non-empty for reasons
other than the enumerated deltas, the model is wrong and it is cheap to find out then.

**Do not** introduce a third-party unit-file library. The nine units are static, the vocabulary is
tiny, and a dependency here is a supply-chain surface on the install path.

---

## 5. Stages — and the gate on the flip

| stage | content | exit condition |
|---|---|---|
| **A. model + baseline** | capture the current `.in` render for both runtime arms as snapshot fixtures; build the unit model (§4.4); re-express the two existing Python units in it with **no behaviour change** | parity harness exists and is RED for 9 of 9 |
| **B. divergent two** | `yadgar.service` + `yadgar-backend.service` reach parity — the whole §1.3 regression list resolved into the renderer or into `INTENTIONAL_DELTAS` | 2 of 9 GREEN. Wrapper still renders everything |
| **C. greenfield seven** | port `yadgar.target`, vacuum ×4, nightly ×2, plus `_resolve_host_exec`, the DP5 guard, and the two render-time side effects (§4.3) | 9 of 9 GREEN |
| **D. the flip** | wrapper stops rendering; `.in` files deleted; test dispositions (§6.4); `uninstall.sh` unit list repointed; version stamp + skew assertion (§7) | wrapper renders nothing; suites green |
| **E. VM** | fresh-VM matrix (§8.2) | see §8.2 |

**The flip in Stage D begins only after every one of the nine units renders at parity in
Stage C.** Not "the important ones". Not "the two that matter". Nine. The mixed-generation state
(some units from `sed`, some from Python, both claiming to own the set) is the one failure mode
with no clean recovery, because `uninstall.sh` and `yadgar.target` both assume a single coherent
unit set.

Stages B and C are independently revertible; Stage D is the one-way door.

---

## 6. TDD story

CI gates by directory (`.forgejo/workflows/ci-pr.yaml:79-83` runs `yadgar/tests/scripts/`;
`:259` runs `yadgar/tests/core/`). A test in the wrong directory is never gated. Both are gated
here, so placement is about ownership.

### 6.1 RED first — the nine-unit parity harness

**New:** `yadgar/tests/scripts/test_systemd_generator_convergence.py` — the load-bearing test.

1. **Baseline — COMMITTED FIXTURES, not regenerated at test time.** This is a hard constraint,
   not a preference. `yadgar/tests/_unit_render.py:26` points `render_systemd` at
   `GENERATE_SYSTEMD_SH`, and after Stage D that script renders nothing — so the harness that
   produces the baseline stops working in the same commit that most needs it. Capture the `.in`
   render once, in **Stage A**, for both runtime arms, via
   `render_systemd(tmp_path, {"YADGAR_RUNTIME": "docker"})` (`yadgar/tests/_unit_render.py:47`),
   and **commit the output** under `yadgar/tests/scripts/snapshots/` (the dir already exists).
   From then on the harness compares the Python render against those files and never re-invokes
   the shell renderer. **Capture POST-#0111** so the baseline already carries `Wants=` (§10).
   `_unit_render.py` itself is a Stage-D migration item — see §3.1.
2. **Render the Python path** with the same inputs (same `HOME`, `YADGAR_STATE_DIR`,
   `YADGAR_INSTALL_PREFIX`, images, secrets path). Pin every input the snapshot was taken with;
   an unpinned `HOME` or image tag makes the diff noise rather than signal.
3. **Strip the schema-stamp header before comparing** — see §6.2. The two stamp lines are
   harness-level exempt, not per-unit deltas.
4. **Assert set-equality of the nine filenames first**, then per-unit text equality modulo
   `INTENTIONAL_DELTAS`.

RED on day one: the Python renderer emits two files, not nine, and neither matches.

### 6.2 `INTENTIONAL_DELTAS` — enumerated per unit, each with a reason

The list is the **deliverable**, not scaffolding: it is the reviewable answer to "what changed
about the installed units?" Structure it so an unexplained delta cannot pass:

```
INTENTIONAL_DELTAS: dict[str, list[Delta]]   # unit filename -> deltas
Delta: what changed (matcher), and WHY (non-empty reason string)
```

Rules, enforced by the test itself:

* keyed **per unit** — a delta approved for `yadgar.service` does not excuse the same text in
  `yadgar-vacuum.service`
* every entry carries a **non-empty reason**; the test asserts that (a blank reason is how this
  list rots into a mute allowlist)
* a unit with **no** entry must match **byte-for-byte**. **Exactly two units — `yadgar.service`
  and `yadgar-backend.service` — are expected to carry entries; the other seven must be
  byte-identical.** A third unit needing an entry is the tripwire: the port is drifting, not
  converging. Stop and re-examine rather than adding a line
* an **unmatched** diff line fails, and an **unused** delta entry also fails (stale-allowlist
  guard — the repo already treats a stale allowlist entry as a hard failure, per the
  `yadgar-install-surface-generators` wiki page)

**The §7 schema stamp is HARNESS-LEVEL EXEMPT, not a delta.** It lands on all nine units, so
listing it per-unit would put an entry on every unit, make zero units byte-identical, and fire the
tripwire above on a *correct* implementation — destroying the only signal the list carries. Strip
the two `# yadgar-unit-schema:` / `# rendered-by:` header lines from both sides before diffing,
and pin the stamp's own shape in a separate one-line test.

Expected entries, from §1.3 and §3.2 — pre-declare them so review is about the list, not
discovery. **`yadgar-backend.service`:** readiness shape
(`Type=`/`TimeoutStartSec`/`--sdnotify`/`--health-cmd`/`--health-start-period`);
`YADGAR_QUEUE_BASE` `/data`→`/queue-data` plus the queue mount; the HF cache mount.
**`yadgar.service`:** the `Description=… (Docker)` label; the orphaned docker comment block; the
core `/data` mount shape if §9.5 does not scope it out. Everything else in §1.3 is a **regression
to fix**, not a delta to approve — do not let those leak into the list.

### 6.3 RED — the readiness change on the shell path

**Extend:** `yadgar/tests/scripts/test_runtime_readiness_cross_generator.py`.

ADR-0187 scoped its assertions by `Type=` so a `Type=simple` unit is exempt *on principle*. After
convergence no backend unit is `Type=simple`, so the existing floor assertion (`TimeoutStartSec` >
systemd's 90s default) binds on the shell-rendered backend for the first time. Add a RED test
asserting the shell-path backend carries a `TimeoutStartSec` at all.

Mutation-prove both halves as ADR-0187 did: set `90`, and set an empty value
(`TimeoutStartSec=`, which systemd reads as *reset to default*). Use horizontal whitespace
`[^\S\n]` around `=` in unit regexes, never `\s` — otherwise an empty value swallows the newline
and captures the next directive.

### 6.4 Tests that go vacuous or hard-break — decide per test

Two distinct hazards. Do not conflate them.

**(a) Hard-break — these read the `.in` files as text and fail the moment they are deleted.**

`yadgar/tests/core/test_systemd_unit_template.py` opens
`REPO_ROOT/"scripts"/"install"/"yadgar.service.in"` directly (`:23`) and asserts on its content:
`test_unit_template_has_type_notify` (`:37`), `..._docker_render_gates_readiness_without_notify`
(`:57`), `..._uses_image_tag_env_var` (`:82`), `..._has_timeoutstopsec` (`:100`),
`..._environmentfile_optional_prefix` (`:112`). **Retarget every one to the Python renderer's
output** — they pin real properties from the §1.3 regression list (`${YADGAR_IMAGE_TAG}`,
`TimeoutStopSec=45`, the `EnvironmentFile=-` prefix) and are exactly the guards that stop the port
from quietly dropping them. Its launchd assertions (`:124-133`) are unaffected.

**(b) Vacuous-but-green — the five cross-generator invariants.**

Post-convergence these compare a thing to itself, stay green, and stop guarding — the *"guard
shape must match bug shape"* failure the `yadgar-install-surface-generators` wiki page documents.
Decide explicitly, in the PR:

| test | disposition |
|---|---|
| `test_admin_token_cross_generator.py` | **retarget** — launchd + `docker-compose.yml` + `flake.nix` remain independent surfaces; drop only the shell arm |
| `test_backend_db_mount_cross_generator.py` | **retarget** — `daemon.py` (`yadgar daemon start`) is still a separate mounting path (task #0100) |
| `test_backend_unit_queue_base_cross_generator.py` | **retarget** to launchd; the `/data` vs `/queue-data` divergence is resolved *by* this car and must appear in `INTENTIONAL_DELTAS` |
| `test_vacuum_trigger_cross_generator.py` | **retarget** to launchd; the systemd arm becomes a single-generator property assertion — and per §4.2 the two tokens become one shared constant, which is stronger than what the test could check |
| `test_runtime_readiness_cross_generator.py` | **keep + extend** — §6.3. Still covers launchd and both runtime arms |

Plus **0111's two new tests** (§10): `test_core_unit_wants_backend_not_requires` retargets to the
renderer; 0111's shell-vs-Python agreement test goes vacuous and converts to a single-generator
assertion. Neither may be deleted — see §10.

A test that would become vacuous and is neither retargeted nor converted must be **deleted** in
the same PR with the reason in the commit. A green vacuous guard is worse than no guard.

### 6.5 Greenfield-specific tests

* **`yadgar.target` carries BOTH `Wants=` lines** — assert the union, not the presence of one.
  Mutation-prove by dropping the second and confirming RED. This is §4.1's highest-consequence trap
* **the four units with no `[Install]`** — assert the section is absent, not merely that
  `WantedBy` is missing
* **`ExecStart` ×2 on the trigger service**, in order (`rm` before `systemctl start`) — the order
  is load-bearing (`yadgar-vacuum-trigger.service.in:6-8`)
* **timer timezones differ** — vacuum local, nightly UTC. Assert both literally; a "helper" that
  normalises them is the bug
* **`_resolve_host_exec` isolation** — run the module probe with cwd inside the repo and assert it
  does **not** resolve (§4.3)
* **`upgrade.env` is not overwritten** when it already exists (`generate_systemd.sh:225-231`)
* **`systemd-analyze verify`** on all nine rendered units where available; skip cleanly otherwise

---

## 7. Version skew — larger with all nine, so mitigate rather than note

Today `yadgar-setup.sh` and the `.in` templates ship in the same wheel (`pyproject.toml:103` maps
`scripts/install` → `share/yadgar/scripts`), so they **cannot** disagree. After delegation the
renderer comes from whatever host `yadgar` resolves, which need not match the `yadgar-setup.sh`
being executed (curl-piped installer vs an older pipx CLI).

The host-CLI *dependency* is not new — `generate_systemd.sh:120-165` already hard-fails without
one. The *version coupling* is, and with nine units delegated the exposure is the whole install,
not two files.

**Mitigation, all three layers:**

1. **Prefer the co-shipped tree.** Resolve the renderer relative to the wrapper's own location
   first (`share/yadgar/scripts/..` → the installed package), before `command -v yadgar`. In the
   common case wrapper and renderer are the same install and skew never arises.
2. **A schema stamp in every rendered unit.** The renderer emits a header comment in each of the
   nine:
   `# yadgar-unit-schema: <N>` and `# rendered-by: yadgar <version>`.
   Cheap, inert to systemd, and it buys three things: the wrapper can assert on it, `uninstall.sh`
   / `--doctor` can detect stale units, and a support question ("what generated these?") is
   answerable from the unit file. Bump `N` only on a **breaking** shape change, not per release.
3. **A wrapper-side minimum-schema assertion.** The wrapper knows the schema it expects; it asks
   the resolved renderer for its schema (a `--print-schema`-style query) and **fails loud** on a
   lower one, reusing `_fail_no_host_cli`'s actionable-message shape (*name what was tried, name
   the fix, exit non-zero*). Never silently proceed: a renderer one minor behind emits units for a
   different image contract, and the failure mode is a unit that starts and is wrong.

   **Bootstrap gap — spell this out or the first implementer gets it backwards.** A genuinely old
   renderer does not implement the query flag *at all*: the wrapper gets a non-zero exit or an
   argparse error, **not** a low number. So a failed, empty, or unparseable schema query must be
   treated as *"too old"* and abort with the same message — a naive `if schema < N: abort` lets the
   real old-CLI case fall straight through the check it exists for. Test both: a renderer
   reporting `N-1`, and a renderer that rejects the flag.

The stamp is **not** an `INTENTIONAL_DELTAS` entry — it is stripped at the harness level before
diffing. See §6.2 for why listing it per-unit would break the tripwire.

---

## 8. Verification

### 8.1 Provable locally

* the nine-unit parity harness (§6.1) — the bulk of the proof
* every retargeted test from §6.4(a) — they pin the §1.3 regression list
* the greenfield tests (§6.5)
* both runtime arms render without error; `systemd-analyze verify` clean where available
* the `setup.py` runtime-label cosmetic — unit test on `cmd_setup`'s output with `check_runtime()`
  stubbed to a podman result, asserting the word "Docker" appears in **neither** branch
* `uninstall.sh`'s unit list matches the renderer's

### 8.2 Requires the fresh VM

`192.168.122.101` —
`sshpass -p 'Aa1234.' ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password root@192.168.122.101`

Snapshot before each run. Local rendering cannot prove any of these:

1. **Cold start end to end.** `yadgar-setup` from scratch, then
   `systemctl --user is-active yadgar.target yadgar.service yadgar-backend.service`. The backend's
   first cold model load is what the 180s budget exists for and is only observable here.
2. **`--sdnotify=healthy` fires on this host's podman.** The shell path never carried it.
   `systemctl --user show yadgar-backend.service -p Type,ActiveEnterTimestamp` proves the unit
   reached active via READY=1, not via a timeout.
3. **Vacuum trigger round trip.** MCP `vacuum_now()` → file under
   `~/.local/state/yadgar/triggers/` on the host → `yadgar-vacuum-trigger.path` fires →
   `yadgar-vacuum.service` runs. This is the §1.3 regression the naive approach would have
   shipped, **and** the §4.2 shared-constant invariant, end to end.
4. **`yadgar.target` still activates all three maintenance units.** `systemctl --user list-timers`
   shows both timers; the `.path` is active. `is-enabled` reports `disabled` for target-pulled
   units — probe `is-active` / `list-timers` (`yadgar.target.in:16-18`). **This is the §4.1
   duplicate-`Wants=` trap's field check.**
5. **Both maintenance units actually run.** `systemctl --user start yadgar-vacuum.service` and
   `yadgar-nightly-cycle.service` by hand: the ported `@VACUUM_EXEC@` / `@NIGHTLY_EXEC@` resolve to
   a host CLI that exists, and the nightly one is invoked **bare** (§4.3).
6. **Upgrade path.** `~/.local/state/yadgar/upgrade.env` exists, was not overwritten on reinstall,
   and `yadgar.service` still reads `${YADGAR_IMAGE_TAG}` from it.
7. **Reinstall over an existing install** — step 5's restart branch (`yadgar-setup.sh:571-583`) on
   a VM that already has units, including units rendered by the **old** generator (the real upgrade
   path, §9.1).
8. **Version-skew assertion fires.** Point `YADGAR_HOST_CLI` at a deliberately old CLI and confirm
   the wrapper aborts with the actionable message rather than rendering (§7).
9. **`uninstall.sh` removes all nine.**

---

## 9. Rollback

### 9.1 How existing installs upgrade

Units are **regenerated wholesale on every install**, never migrated — `generate_systemd.sh`
overwrites each unit unconditionally, and `yadgar-setup.sh` step 5 restarts `yadgar.target` when it
is already active. That property is what makes this car survivable: an upgrade is a full rewrite,
and a downgrade is a full repair. State it in the ADR consequences.

Two carve-outs that are **not** regenerated and must not be clobbered:
`~/.local/state/yadgar/upgrade.env` (seeded only if absent) and anything under `$STATE_DIR`.

### 9.2 Concrete recovery paths

| situation | recovery |
|---|---|
| **pre-merge** | the branch. `.in` files are deleted in the **last** commit of Stage D, so any earlier revert leaves a working shell path |
| **post-merge, pre-release** | revert the merge; templates return; no user has run the new renderer |
| **post-release, units broken** | re-run `yadgar-setup` from the previous version — it rewrites all nine from that wheel's templates. Full repair, no manual editing |
| **mid-migration (install interrupted between units)** | **the dangerous one.** See §9.3 |
| **renderer unavailable / skew abort** | the wrapper aborted before writing, so the previous units are still on disk and running. Fail-loud *is* the recovery |

### 9.3 Mid-migration: make it impossible rather than recoverable

Today `render_template` writes each unit **directly into `OUTPUT_DIR`, one at a time**
(`generate_systemd.sh:172-186`), so an abort halfway leaves a mixed-generation set — some units
new, some old, `uninstall.sh` and `yadgar.target` both assuming coherence. There is no good
recovery from that state short of a manual `rm` of nine files.

**Plan the port to render all nine into a temp dir, validate, then move them into place**, so the
window shrinks to a rename loop. Full atomicity across nine files is not achievable without a
directory swap (and `~/.config/systemd/user` holds unrelated units, so swapping the directory is
wrong) — but render-and-validate-then-write removes every failure mode except a crash inside the
final loop, and the fallback for that is §9.2's "re-run the installer".

Document the manual escape where `_fail_no_host_cli` documents its own:
`systemctl --user stop yadgar.target`, remove the nine units, re-run `yadgar-setup`.

### 9.4 Do the templates stay on disk for one release?

Two honest options; pick one in review:

* **(a) delete the templates, no fallback** — cleanest. The fallback is "downgrade the wheel",
  which §9.1 makes a genuine full repair rather than a patch-up.
* **(b) keep the nine `.in` files for one release**, wrapper renders from them under
  `YADGAR_SYSTEMD_LEGACY=1` — a real in-place escape hatch, at the cost of keeping the drift
  surface alive for a release and needing the parity harness to keep running against both.

**(a) is recommended.** The whole point of the car is that two renderers drift; keeping a dormant
second renderer for a release keeps the defect alive at exactly the moment nobody is watching it.
(b) is only worth taking if the VM matrix (§8.2) surfaces something unexplained — in which case the
right move is probably to hold the car, not to ship it with a hatch.

### 9.5 No data migration

The one mount that changes shape — core `/data`, named volume → host bind — must be listed in
`INTENTIONAL_DELTAS` and, if it also changes for the `daemon install-service` arm, needs the
detect-copy-switch treatment task #0100 used. **Prefer scoping it out** by keeping the
profile-driven arm on its current mount. A data move does not belong in the same car as a
generator rewrite.

---

## 10. Ordering vs #0111 — and how its decoupling survives the port

**0111 → 0110. Unchanged, non-negotiable.**

0111 edits `scripts/install/yadgar.service.in:4` (`Requires=` → `Wants=`) and
`yadgar/core/daemon/systemd.py:232`, and flips `yadgar/tests/core/test_daemon_runtime_binary.py:604`.
If 0110 lands first, that template no longer exists and 0111 must be re-pointed mid-flight.

**With all nine in scope, 0110 retires the very file 0111 edits — so the decoupling must be
carried, not inherited.** Three concrete mechanisms, all required:

1. **Rebase, don't merge-and-hope.** 0110 branches off *after* 0111 lands.
2. **Capture the parity baseline POST-0111.** The `.in` snapshot in §6.1 must come from a tree that
   already has `Wants=` at `yadgar.service.in:4`. Then "core carries `Wants=`" is enforced by
   byte-parity itself — the strongest available guarantee, because reverting it produces a diff
   with no `INTENTIONAL_DELTAS` entry and fails.
3. **Carry 0111's own tests forward, retargeted.** 0111 adds
   `test_core_unit_wants_backend_not_requires` (assert `Wants=` present, `Requires=yadgar-backend`
   absent, `After=` unchanged) and a shell-vs-Python agreement test. After convergence the first
   **retargets to the renderer** and stays non-vacuous; the second goes vacuous and **converts** to
   a single-generator assertion. Neither may be deleted. Add both to §6.4's disposition table when
   0111 lands and the exact names are known.

The failure this guards against is specific and plausible: a port that reconstructs the core unit
from the *Python* generator's memory of it silently reintroduces `Requires=`, 0111's test was
pointed at a deleted template, and nothing fails. Mechanism 2 is what makes that impossible.

Other cars:

* **#0027c** is coupled to 0111, not to this car — but this car changes the regime it reasons
  about (post-0110 the backend is `Type=notify`/gated on the shell path, so the
  core-starts-before-backend race narrows). 0027c's plan states both regimes and explicitly says
  the retry gate is **not** the cold-boot mechanism; `After=` is, once 0110 lands.
* **#0112** is independent. Parallel, zero conflict.
* **#0109** already shipped. No interaction.

---

## 11. ADRs

**New ADR: ADR-0190** (recorded 2026-08-01). It **supersedes ADR-0189**, which described the naive
"shell calls Python" direction; §1.3 is the evidence that direction regresses the data path.

It must state:

* the decision: one Python renderer for **all nine** units; `generate_systemd.sh` renders nothing;
  every `.in` retires; a unit **model** rather than string templates (§4.4); mode parameter, not a
  fork
* the method: **absorb then delegate**, with the flip gated on all nine at parity (D2 / §5)
* that it **removes ADR-0185's marker mechanism** while preserving its decision (runtime-conditional
  readiness in one unit shape). ADR-0185 is **not** superseded — its *what* survives, its *how*
  changes. Record the marker retirement as a consequence and cross-link
* that it **corrects ADR-0187's premise**: the sentence *"`After=`/`Requires=` means the backend is
  already HEALTHY"* was true only of the Python generator and becomes true of the shell path as a
  **result** of this car. Recommend **amend + cross-link**, not supersede — no decision is
  reversed, a scope error is corrected. Note that 0111's `Requires=`→`Wants=` weakens the same
  premise from the other direction
* consequences: the version stamp + skew assertion (§7); the cross-generator and
  `test_systemd_unit_template` dispositions (§6.4); the `@STATE_DIR@` invariant becoming a shared
  constant (§4.2); regenerate-not-migrate as the upgrade and rollback story (§9.1); and the
  temp-dir render that closes the mid-migration window (§9.3)
* `revisit_trigger`: a fourth Linux unit generator appears, or launchd is converged into the same
  model (the obvious next consolidation, deliberately out of scope here)

**ADR-0161** (global-authoritative install) — read it before writing; this car changes *where*
units come from, not *whose* config is authoritative, so no conflict is expected. Confirm, do not
assume.
