# Repo-hygiene bundle — dirty-tree test, docker-literal hint, stale flake health probe, wheel entry-point gap

**Date:** 2026-07-29
**Tasks:** four independent defects surfaced during the v5.169 install-runtime train (no task IDs yet)
**Status:** PLANNED — Car H1 ready to implement; Car H2 blocked on decisions D3/D4.
**Target train:** `feat/v5.169-install-runtime-fixes`
**Sibling plans (same day, same train):**
`docs/plans/fix-vacuum-reclaim-and-core-stability-2026-07-29.md`,
`docs/plans/fix-vacuum-trigger-path-and-watcher-2026-07-29.md`

---

## 0. Verdict up front

| # | Defect | Still real? | Verdict | Car |
|---|---|---|---|---|
| **D1** | `test_wiki_add_phase0_profiling` rewrites the tracked file `docs/reports/releases/v5-41-5-profiling-report.md` | **YES — confirmed at source, deterministic** | **fix-now** (2-line test change) | **H1** |
| **D2** | hardcoded `docker pull …` in the `container` upgrade hint | **YES — literal confirmed** — but severity is *lower* than reported (see §2.3) | **fix-now** (1 line + 1 test assertion) | **H1** |
| **D3** | `flake.nix` core healthcheck still probes `/health`, not `/health/live` | **YES — plus a THIRD stale surface nobody has mentioned: `Dockerfile:24`** | **fix-now** (2 lines + 1 static guard test) | **H1** |
| **D4** | no clean-venv wheel-install + entry-point test | **YES — and the gap is wider than "console scripts"** | **separate-car**, worth doing, needs authoring | **H2** |

**Split answer, stated plainly:** **two cars, not one, and not four.**

- **Car H1 = D1 + D2 + D3.** Each is genuinely 1–2 lines of production/test change plus one
  small guard test. They are unrelated in mechanism but identical in shape (one-line hygiene
  fixes with a pin), and each is independently verifiable. Bundling three one-liners into one
  car costs nothing and saves two round-trips of branch/PR/CI.
- **Car H2 = D4 alone.** Different mechanism, requires authoring a new test from scratch,
  touches **two divergent CI mirrors**, and is not scopeable until decision **D3** (which
  invocation surfaces) and **D4** (which tier) are answered. Bundling it with three one-liners
  would make the H1 diff unreviewable and delay three cheap wins behind one design question.

If any H1 fix grows past ~5 lines during implementation, split it out rather than letting the car swell.

---

## 1. D1 — a test rewrites a tracked file

### 1.1 Root cause (confirmed at source, not inferred)

`yadgar/tests/core/test_wiki_handler_phase0_profile.py:274-281`:

```python
    report_path = (
        Path(__file__).parent.parent.parent.parent
        / "docs" / "reports" / "releases" / "v5-41-5-profiling-report.md"
    )
    report_path.write_text(report, encoding="utf-8")
```

The test writes **into the repo working tree**, unconditionally, on every run.

**Two independent diff sources, both deterministic:**

1. **Trailing whitespace.** `_build_report` emits markdown hard-breaks —
   `"**Date:** 2026-06-02  "`, `"**Phase:** 0 — pre-fix baseline  "`,
   `"**I9 budget:** ≤5ms p50  "`, `"**Machine:** local dev …  "`
   (`test_wiki_handler_phase0_profile.py:177-180`, note the two trailing spaces).
   The committed file has those stripped, because `.pre-commit-config.yaml` runs
   `pre-commit/pre-commit-hooks` → `trailing-whitespace`. So every test run *restores*
   whitespace that the commit hook *removes*. This is a permanent oscillation, not a
   flake — it reproduces on every machine, which is exactly why two separate agents hit it.
2. **Fresh measurements.** Every numeric cell in the report is re-measured, so the table
   body changes run-to-run regardless of whitespace.

### 1.2 Second finding — it should not be in the fast tier at all

- The docstring at line 3 says *"NOT a regular test — run standalone to generate profiling report"*,
  but the function carries **no marker**.
- `pyproject.toml:221` → `addopts = "… -m 'not integration and not e2e'"`, and
  `.github/workflows/ci-pr.yml:277-285` (`test-core`) runs
  `-m 'not integration and not e2e and not perf'`.
  With no marker, this test runs **by default, locally and in CI**.
- Its only assertions are `assert e2e_p50 > 0.0` and `assert sim_p50 > 0.0`
  (lines 284-285) — trivially true. It is a **report generator wearing a test's clothes**.
- Cost is not trivial: 5 warmup calls + 100 substep iterations + 100 e2e iterations, each
  hitting the real similarity gate. The report the repo has committed records
  **similarity-gate p50 = 38.4 ms**, so the similarity leg alone is ≈4 s of wall time, on
  top of real `all-MiniLM-L6-v2` load and a real SurrealDB.
- `pyproject.toml:237` already defines the right marker:
  `perf: timing-sensitive perf guard; run SERIALLY only … dedicated test-perf job`.

Writing to `tmp_path` alone therefore fixes the *dirty tree* but leaves a zero-assertion
4-second benchmark in the fast tier. The correct fix is the pair.

### 1.3 Recommended fix

1. Add `@pytest.mark.perf` to `test_wiki_add_phase0_profiling`.
2. Change the write target to the already-present-but-unused `tmp_path` fixture argument
   (it is in the signature at line 234 and never used), and keep the `print(f"\nReport: …")`
   so a human running it standalone still learns where the file landed.

**The `perf` marker does route the test somewhere — verified, not assumed.** `ci-pr.yml:326-330`
(`test-perf`) runs `python -m pytest yadgar/tests/ -m perf -n 0 -q --tb=short` — a **repo-wide
test path**, so `yadgar/tests/core/…` is collected. Marking `perf` moves the test from the
fast tier to the serial perf job; it does **not** silently drop it from CI.

Do **not** untrack `docs/reports/releases/v5-41-5-profiling-report.md`: it is a frozen historical
artifact from a shipped release, and untracking fixes the dirty tree while leaving the CI cost
and the zero-assertion problem.
Do **not** "pin the formatter" — the formatter is right; the generator is wrong.

**Rejected-but-honest third option (decision D1):** delete the test outright. v5.41.5 shipped;
the fix it justified is in production; the committed report is the permanent record. Keeping it
as a marked perf test costs one `test-perf` slot and preserves the ability to re-measure.

---

## 2. D2 — hardcoded `docker` in a user-facing hint

### 2.1 Still literal — confirmed

`yadgar/core/update/install_methods.py:110-115`:

```python
    if method == "container":
        return (
            "docker pull docker.io/openfantasy/yadgar:latest && "
            "systemctl --user restart yadgar  "
            "# macOS: launchctl kickstart -k gui/$UID/com.openfantasy.yadgar"
        )
```

### 2.2 The "deliberately left" claim checks out

`yadgar/tests/core/test_daemon_runtime_binary.py` carries an AST guard, and its docstring
scopes it to *"anywhere in `yadgar/core/daemon/`"*. `install_methods.py` lives in
`yadgar/core/update/` — outside the guard's path by design, consistent with the report that
today's car converted 17 executed call sites and left this printed string alone.

`yadgar/core/daemon/runtime.py:39-42` records the same intent in a comment.

### 2.3 Severity is LOWER than reported — read this before fixing

The `container` branch is reached only when `detect_install_method()` returns `"container"`,
which requires `_is_docker_shim()` to be true —
`install_methods.py:75-77` reads the first 256 bytes of the `yadgar` shim and tests
`"docker run" in content`. The shell twin does the same
(`scripts/install/detect_install_method.sh:41`, `grep -q "docker run"`).

Consequence: **on a genuinely podman-only host whose shim says `podman run`, the method never
resolves to `container` at all** — so the wrong hint is unreachable there. The window where the
hint is both reached *and* wrong is narrow (a shim containing the literal `docker run` on a host
where `docker` is absent — i.e. a hand-written or stale shim).

This is worth fixing for correctness and clarity, but it is **not** the same severity class as
the executed-argv `FileNotFoundError: 'docker'` bug that motivated task:0083.

**Adjacent finding, explicitly OUT of scope for this bundle:** `_is_docker_shim` and
`detect_install_method.sh:41` are themselves runtime-blind (they only match `docker run`).
That is a real detection gap, but fixing it changes install-method classification behaviour
and needs its own tests. **Do not fold it into H1.** File it separately.

### 2.4 Options and recommendation

| Option | Cost | Assessment |
|---|---|---|
| **A. Wire `_get_runtime()`** | 1 import + 1 f-string | Architecturally *permitted* — `pyproject.toml:279-342` import-linter contracts govern only `core↔backend` and `_shared`; a `core.update → core.daemon` edge violates none. But **semantically wrong for the in-container consumer**: `control_update.py:100` runs inside the daemon container, where neither `podman` nor `docker` is on PATH, so `_get_runtime()` hits its `return "docker"` fallback (`runtime.py:66`) and prints the identical wrong string with extra machinery. |
| **B. Runtime-neutral wording** ✅ | 1 line + 1 test assertion | Correct in every reachable case, no new import edge, no container/host split to reason about. e.g. name the image and let the user apply their own runtime's pull verb. |
| **C. Leave alone + comment** | 1 comment | Defensible given §2.3, but leaves a string that reads as an instruction and is wrong for *some* reachable host. |

**Recommend B.** Note that `yadgar/tests/scripts/test_update_check.py:291-294` asserts
`"docker pull" in cmd` — that assertion must change with the string (2 files, still tiny).
Also note `yadgar/tests/core/test_update_api.py:250` uses `return_value="docker pull ..."` as a
*mock* value only; it does not constrain the real string.

---

## 3. D3 — flake.nix probes the wrong health endpoint

### 3.1 The intended contract (ADR-0019, accepted 2026-06-30)

ADR-0019's consequences clause is explicit:

> "P0 healthcheck must switch /health→/health/live in **BOTH the nix unit AND docker-compose**
> (else the fix is moot) — ships AFTER the version that has /health/live (404 otherwise)."

Rationale, from the ADR: `/health` is **readiness** and probes the backend; a transiently-busy
backend made it 503, and the P0 `--health-on-failure=kill` healthcheck SIGKILLed the core.
`/health/live` is **liveness**, answerable from the core's own loop with no outbound probe.

Both endpoints exist today: `yadgar/core/server/http.py:570` (`/health`) and `:611`
(`/health/live`), both auth-exempt (`yadgar/core/auth_middleware/auth_middleware.py:31`).
So there is no 404 risk in making the switch now.

### 3.2 Current state — TWO stale surfaces, not one

| Surface | Line | Endpoint | Status |
|---|---|---|---|
| `docker-compose.yml` | 104 | `/health/live` | ✅ done |
| `flake.nix` core service | 444 | `/health` | ❌ **stale** |
| `Dockerfile` baked `HEALTHCHECK` | 24 | `/health` | ❌ **stale — not previously reported** |

`Dockerfile:24` is `CMD curl -f http://localhost:8765/health || exit 1`. `flake.nix:17-19`
comments that podman does not honour the image-baked HEALTHCHECK ("known quirk"), which is why
the flake supplies `--health-cmd` at run time — but the baked directive is still the default for
any surface that *doesn't* override it, and it carries the same stale endpoint. Fix both.

`curl` **is** present in the core image (`Dockerfile:5-7` installs it explicitly for the
HEALTHCHECK), so the `curl -f` form in `flake.nix:444` is fine — only the path is wrong.

**Adjacent, explicitly NOT in scope:** `flake.nix:377` probes
`http://localhost:8001/health` for the **backend** service. That is the embed service's own route
(`yadgar/backend/embed_service/embed_service.py:729`), a different service with no `/health/live`.
ADR-0019 is about the *core* probing its *backend dependency*. **Do not "fix" line 377.**

### 3.3 Severity — harmless-but-wrong, or actively misleading?

The coordinator's framing ("no `--health-on-failure`, so it kills nothing") is correct as far as
it goes, but incomplete. The flake core unit is `Type = "notify"` with `--sdnotify=healthy`
(`flake.nix:411, 443`), which means **the first successful health probe is what gates `READY=1`**.
A readiness endpoint that probes the backend therefore couples *core unit startup* to *backend
readiness*, with `TimeoutStartSec = 120` and `--health-start-period 10s`.

Mitigating: the core unit declares `After`/`Wants` on `yadgar-backend.service`, and the backend
unit is itself `Type=notify --sdnotify=healthy` — so by the time the core starts, the backend is
normally already healthy and `/health` normally succeeds.

**Honest characterisation: wrong and latent, not actively breaking.** Steady state is unaffected
(no `--health-on-failure` → an unhealthy container is reported, not killed). The residual risk is
confined to the startup window and to monitoring semantics: `systemctl` reports the core unit's
health as a function of its dependency's health, which is precisely the liveness/readiness
conflation ADR-0019 exists to kill. Do not overclaim a crash-loop; do fix it, because the defect
class recurred *because nothing pinned it*.

### 3.4 Recommended fix

1. `flake.nix:444` → `/health/live`.
2. `Dockerfile:24` → `/health/live`.
3. Add a static guard test asserting no core-port health probe in `flake.nix` or `Dockerfile`
   targets `:8765/health` without `/live`. This is the missing pin — an accepted ADR consequence
   sat unfinished for a month with nothing to catch it.

---

## 4. D4 — no clean-venv wheel-install + entry-point test (SEPARATE CAR)

### 4.1 The gap is real, and wider than "console scripts"

Existing coverage, verified:

| Test | What it actually does |
|---|---|
| `yadgar/tests/scripts/test_v5_46_10_wheel_bundle.py` | `uv build --wheel`, then **walks the zip** asserting filenames present. Never installs, never imports. |
| `yadgar/tests/scripts/test_v5_46_11_pipx_cli_invocation.py` | Static regex over `scripts/install/yadgar-setup.sh`. Its own docstring says "static analysis". |
| `yadgar/tests/scripts/test_v5_46_3_sbom_wheel_install.py` | Static grep over `.forgejo/workflows/ci-release.yaml` text. |

Nothing installs the wheel and imports anything.

**Critical correction to the framing:** the invocation surface is **not** just
`[project.scripts]`. `pyproject.toml:77-80` declares three console scripts
(`yadgar`, `yadgar-nightly-cycle`, `yadgar-setup`), but the `safe_start` regression cited as
motivation is **not a console script** — it is invoked as a module:

- `entrypoint-backend.sh:156` — `python3 -m yadgar.backend.safe_start preflight …`
- `entrypoint-backend.sh:209` — `python3 -m yadgar.backend.safe_start recover …`

A test that only imported the three console-script targets **would not have caught the regression
it exists to prevent.** The assertion list must be the union of console scripts and every
`python -m yadgar…` target invoked by shipped shell/unit files.

### 4.2 Why CI cannot catch this class today — the load-bearing finding

Every test job installs with **`uv pip install --system --no-deps -e .`**
(`ci-pr.yml:77, 132, 187, 266, 325, 419, 534`), against dependencies **baked into the
`yadgar-ci` image from `uv.lock`** (ADR-0089 lock parity). The comment in the workflow states the
intent plainly: *"nothing re-resolves at run time, so CI cannot drift past the lock."*

The consequence is the flip side of that guarantee: **no CI test job ever resolves the
dependency list declared in `pyproject.toml`.** A dependency that is present in the image but
missing from `[project.dependencies]` is invisible to every test job. That is exactly the
`ModuleNotFoundError: No module named 'surrealdb'` shape (the dep is declared today at
`pyproject.toml:70-71`, promoted from dev-only in v5.10.2 — i.e. this class already bit once).

The one job that *does* perform a real resolving install is `build-sbom`:

```yaml
      - name: Install yadgar with sbom extra
        run: |
          pip install --upgrade pip
          pip install "dist/yadgar-${{ … }}-py3-none-any.whl[sbom]"
```

(`.github/workflows/ci-release.yml:334-339`) — and that job is declared
**`continue-on-error: true`** (`ci-release.yml:310`, under `build-sbom:` at `:307`). So the single surface that would catch a
packaging/dependency break is explicitly non-blocking.

### 4.3 Honest cost assessment

The cost is **much lower than "add a clean-venv CI job"**, because the install already exists:

- Cheapest viable version: remove `continue-on-error` from `build-sbom` (or add a small blocking
  sibling job reusing the same downloaded wheel) and append a step that runs each entry point's
  `--version`/`--help` and `python -c "import yadgar.backend.safe_start"` for the module targets.
  Order-of-10 lines of YAML.
- The real cost is **the CI mirror tax**: `.github/workflows/` and `.forgejo/workflows/` both
  exist, `ci-pr.yml` and `ci-pr.yaml` **differ** (verified by `diff -q`), and the repo's own tests
  assert against `.forgejo` (`test_v5_46_3_sbom_wheel_install.py:13`). Any CI change is two files
  plus a decision about which mirror is canonical.
- A pytest-side variant (`venv.create` + `pip install <wheel>` in a temp dir) is possible and
  mirror-free, but it downloads the full dependency set from PyPI on every run — genuinely slow
  and network-dependent. **Not fast-tier material.**

### 4.4 Verdict — worth it, release tier, own car

**Yes, worth doing.** The `--no-deps` + `continue-on-error` combination means the
dependency-resolution and entry-point surface is currently untested end-to-end, and this class has
shipped to a real install at least twice.

**Tier: release gate, not fast tier.** Packaging breaks at release, not at PR time; a wheel build
plus resolving install on every PR is the expensive version of this test and buys little.

**Car: its own (H2).** It needs a new test authored, an enumerated assertion list (§4.1), and two
CI mirror edits. Bundling it with three one-liners is a false economy.

---

## 5. Cars

### Car H1 — three one-line hygiene fixes + their pins

| Step | File | Change |
|---|---|---|
| H1.1 | `yadgar/tests/core/test_wiki_handler_phase0_profile.py` | `@pytest.mark.perf` + write to `tmp_path` |
| H1.2 | `yadgar/core/update/install_methods.py:110-115` | runtime-neutral upgrade hint (option B) |
| H1.3 | `yadgar/tests/scripts/test_update_check.py:291-294` | update the `"docker pull" in cmd` assertion |
| H1.4 | `flake.nix:444` | `/health` → `/health/live` |
| H1.5 | `Dockerfile:24` | `/health` → `/health/live` |
| H1.6 | new static guard test | assert no `:8765/health` (without `/live`) in `flake.nix` / `Dockerfile` |

Order: H1.1 first (it makes the tree stay clean while the rest of the car is developed —
otherwise every local test run re-dirties the tree and risks the exact `git add -A` accident
this bundle exists to prevent). Write H1.6 RED before H1.4/H1.5.

### Car H2 — wheel-install + entry-point gate

Blocked on decisions **D3** and **D4** below. Sketch:

1. Enumerate the invocation surface: `[project.scripts]` ∪ every `-m yadgar` target found by a
   **repo-wide** grep — not a hand-listed path set. A path list scoped to
   `entrypoint*.sh`/`scripts/install/`/`flake.nix`/`docker-compose.yml` would miss the hook
   scripts the installer renders into `~/.claude/hooks/`, whose templates live inside the
   package rather than in those directories. (Today's repo-wide grep yields
   `yadgar.backend.safe_start` ×2 in `entrypoint-backend.sh` plus the `__main__.py` modules
   themselves — but the *instruction* must stay repo-wide so the enumeration can't inherit the
   §4.1 blind spot.)
2. Add the assertions to the release pipeline's existing wheel install.
3. Make that job blocking (drop `continue-on-error`, or split a blocking sibling).
4. Mirror to `.forgejo/workflows/ci-release.yaml`.
5. Add a static test asserting the gate exists (matching the repo's `test_v5_46_3_*` precedent).

---

## 6. Acceptance criteria

### Car H1

- **[unit]** `pytest yadgar/tests/core/ -m 'not integration and not e2e and not perf'` completes,
  then `git status --porcelain docs/reports/` is **empty**. This is the direct reproduction of D1.
- **[unit]** `pytest yadgar/tests/core/test_wiki_handler_phase0_profile.py -m perf` collects and
  passes; the same file collects **zero** tests under `-m 'not perf'`.
- **[unit]** the exact `test-perf` command still collects it:
  `python -m pytest yadgar/tests/ -m perf -n 0 --collect-only` lists
  `test_wiki_add_phase0_profiling`. (Guards against the marker silently deleting CI coverage —
  `test-perf`'s path is repo-wide today, `ci-pr.yml:326-330`, but that is the assumption to pin.)
- **[unit]** `test_update_check.py::…::test_container_upgrade_command` passes against the new
  string and no longer asserts a runtime-specific verb.
- **[unit]** the new H1.6 guard test is **RED** on the pre-fix `flake.nix`/`Dockerfile` and GREEN after.
- **[manual]** `nix flake check` (or the repo's existing flake eval step) still evaluates —
  `flake.nix` is a string edit inside `lib.concatStringsSep`, so a syntax slip is the only risk.
- **[manual]** on the maintainer's podman host: `systemctl --user restart yadgar` after a rebuild,
  then confirm the unit reaches `active (running)` and `podman healthcheck run yadgar` reports
  healthy against `/health/live`.
- **[e2e]** *not applicable* — no behavioural surface changes in H1 other than the probe path,
  which the manual check above covers.

### Car H2

- **[unit]** static test asserting the release workflow contains a resolving wheel install that is
  **not** `continue-on-error`, and that it invokes every enumerated entry point.
- **[unit]** the enumerated-surface list in the test matches a fresh grep of
  `python -m yadgar` across shipped shell/unit files (self-checking, so a new entry point added
  without a matching assertion fails).
- **[e2e]** a release-tier run installs the built wheel into a clean environment and every
  console script answers `--version`/`--help` with exit 0, and every `-m` target imports.
- **[manual]** confirm which CI mirror actually executes for this repo before trusting the gate.

---

## 7. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Marking D1's test `perf` moves it to `test-perf`, which runs **serially** (`-n 0`) and whose comment describes it as "~3 tests, lightweight". Adding a real-model + 200-real-call test measurably changes that job's character. | medium | Measure once in `test-perf` before merging; if it dominates the job, take decision D1's delete option instead. |
| `.test_durations` (committed, read by `--splitting-algorithm least_duration`) **does** contain an entry for `test_wiki_add_phase0_profiling` (verified); removing it from the fast tier leaves a stale entry and shifts `test-core` chunk balance. | low | Harmless — `least_duration` degrades gracefully on a stale entry. Regenerate on the next durations refresh. |
| Changing the upgrade-hint string breaks a consumer that greps it. | low | Only two consumers exist (`control_update.py:100`, `cli/update.py:127`), both pass it through verbatim to the user. |
| `flake.nix` was edited by a car that merged **today** (`systemd.user.paths.yadgar-vacuum-trigger` + handler service, commits `8f000570`/`d6019378`/`8b9ae4e2`). | medium | This plan was written against the post-merge tree; line 444 verified on `feat/v5.169-install-runtime-fixes` at commit `4d21ce80`. **Re-verify the line number at implementation time** rather than trusting it. |
| The private nix repo (out of scope, read-only) may carry its own copy of the health probe. | medium | Out of scope by instruction. Flag to the maintainer that the same one-line change likely applies there; ADR-0019's "BOTH surfaces" clause suggests a third copy may exist. |
| H2's CI change lands in the wrong mirror and silently does nothing. | high | Decision D4 must be answered before implementation; add the static test asserting the gate's presence in whichever mirror is canonical. |
| The `_is_docker_shim` detection gap (§2.3) gets folded into H1 by a well-meaning implementer. | medium | Explicitly out of scope, stated in §2.3 and here. File separately. |

---

## 8. Open decisions

**D1 — profiling test: mark-perf, or delete?**
Recommendation: mark `perf` + write to `tmp_path`. Delete is defensible (v5.41.5 shipped, the
committed report is the permanent record, the assertions are trivial). Maintainer call.
*Blocks:* H1.1.

**D2 — upgrade hint: resolved runtime, or runtime-neutral wording?**
Recommendation: **runtime-neutral wording**. Import-linter permits the `core.update → core.daemon`
edge (checked: `pyproject.toml:279-342` constrain only `core↔backend` and `_shared`), so option A
is *allowed* — but `_get_runtime()` is meaningless for the in-container consumer and would fall
back to `"docker"` anyway. Wording is correct in every reachable case for one line.
*Blocks:* H1.2/H1.3.

**D3 — H2 assertion surface: console scripts only, or the full `-m` union?**
Recommendation: the **full union**. Console-scripts-only would not have caught the `safe_start`
regression that motivates the test. Needs the maintainer to confirm no additional invocation
surface exists outside the repo (e.g. in the private nix repo's units).
*Blocks:* H2 scoping.

**D4 — H2 tier and location: blocking release gate, or a new PR job?**
Recommendation: **blocking release gate**, reusing the existing `build-sbom` wheel install by
dropping `continue-on-error` (`ci-release.yml:310`). Sub-decision, and a prerequisite for any CI
edit in this repo: **which of `.github/workflows/` and `.forgejo/workflows/` is canonical?**
They both exist and `ci-pr.yml` / `ci-pr.yaml` differ; the repo's own tests assert against
`.forgejo`. This ambiguity is itself worth a task.
*Blocks:* all of H2.
