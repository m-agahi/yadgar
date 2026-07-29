# Verify: nightly consolidation `ModuleNotFoundError: No module named 'surrealdb'`

**Task:** harness task:0070 — "Verify nightly consolidation's 'No module named surrealdb' bug still exists" (from retired anchor mem 519136, 2026-05-29).
**Status:** INVESTIGATION COMPLETE — **the reported bug is already fixed and has been since 2026-05-29.** No code change is warranted for the import itself, so **this plan proposes NO car on `feat/v5.169-install-runtime-fixes`** (see §3 — the train brief asked for one; there isn't one to have). Recommendation: **close task:0070**.
**Second finding (in scope, unresolved):** task:0070's own description also asks *"does nightly consolidation actually run"*. Answer: **not on a pipx/non-nix Linux install** — no nightly systemd unit is ever generated there. That is a real, undocumented gap and the sibling of task:0044(b). Recommendation: **file it as a NEW task cross-linked to 0044; do NOT ride this train.**
**Related:** task:0042 (vacuum `:8080` reachability — explicitly NOT merged here), task:0044(b) (same generator, missing vacuum `.path` watcher), task:0081 (the fresh-VM QA run that provided the live evidence below).

---

## 1. Verdict on the reported bug: FIXED, 2026-05-29, ~2h22m after it was reported

### 1.1 The timeline is direct, not circumstantial

| when (UTC) | what |
|---|---|
| `2026-05-29T13:49:42Z` | anchor mem 519136 recorded. Its own hypothesis: *"confirm `surrealdb` in pyproject.toml dependencies (NOT optional/dev)"* |
| `2026-05-29T16:11:45Z` | commit `21cad6cc` — `fix(deps): v5.10.2 — promote surrealdb to base deps (was dev-only)`. Body: *"StorageEngine imports surrealdb at runtime. Having it in [dev] only caused ImportError on clean installs."* |

Delta: **2 h 22 m**. The commit names the exact hypothesis the anchor wrote down, in the same direction, on the same day. The anchor was simply never updated after the fix landed — it was tagged `follow-up`, expired to `ephemeral` tier, and retired unread.

The dependency is still declared today: `pyproject.toml:70-71`

```toml
# v5.10.2 — surrealdb promoted from dev-only to base dep (StorageEngine uses it at runtime)
"surrealdb>=1.0.0",
```

`git log -L 68,72:pyproject.toml` confirms it has not been touched since except by an adjacent insertion (`d0aedbc6`, v5.152.0, added `tomlkit` below it).

The anchor's *alternate* hypothesis — "pipx editable install dropped surrealdb on a re-install" — is moot once the package sits in `[project.dependencies]`: that covers wheel and editable install shapes alike. Not a work item.

### 1.2 Live proof on a real pipx install (read-only, no mutation)

Fresh Debian 13 VM `192.168.122.101`, yadgar **5.168.0** installed via pipx (the task:0081 QA host):

```
/root/.local/share/pipx/venvs/yadgar/lib/python3.14/site-packages/surrealdb   # present
surrealdb version 2.0.0, yadgar version 5.168.0
python -c "import importlib; importlib.import_module('yadgar.core.scripts.nightly_cycle')"
  → IMPORT OK  .../site-packages/yadgar/core/scripts/nightly_cycle.py
```

The whole nightly-cycle import chain resolves inside a real pipx venv. This is the cheapest possible refutation of "still broken", and it holds.

### 1.3 The architecture has moved past needing it on this path anyway

All three `surrealdb` import sites in non-test code are **function-local and embedded-mode-only**:

- `yadgar/_shared/storage/__init__.py:314` — after the server-mode `return` at `:310`; the comment at `:313` reads *"Embedded mode (existing behavior)"*.
- `yadgar/_shared/storage/client.py:394` — inside `_restore_from_backup`, an embedded-DB corruption path.
- `yadgar/core/cli/stats.py:701` — the `_run_db_path` direct-DB fallback, already wrapped in `try/except ImportError` with a friendly message.

Note also that the nightly cycle runs under the **pipx** venv on every platform, nix included: `flake.nix:623` ExecStarts a wrapper (`flake.nix:547-550`) whose only job is to set `LD_LIBRARY_PATH` and then `exec ~/.local/bin/yadgar-nightly-cycle` — whose shebang, verified on the VM, is `#!/root/.local/share/pipx/venvs/yadgar/bin/python`. "Installed via nix" does **not** mean a nix-managed dependency closure for this path. That is both why the original 2026-05-29 failure bit a nix host, and why the single-VM pipx check in §1.2 is a representative sample rather than a narrow one.

The nightly cycle's consolidation step (`yadgar/core/scripts/nightly_cycle.py:282-321`) constructs `StorageEngine(str(db_path))` with `YADGAR_DB_URL` set — see the nix unit at `flake.nix:618`:

```nix
"YADGAR_DB_URL=http://127.0.0.1:${toString cfg.backendSurrealPort}"
```

→ StorageEngine takes the **server-mode** branch → `from surrealdb import Surreal` at `:314` is never reached. Even if the declared dependency vanished tomorrow, the nightly consolidation step would not hit it. The 2026-05-29 failure predates the core/backend split; the import was genuine *then*, and is vestigial on *this* path now.

**Do not delete the dependency.** It is still load-bearing for embedded mode generally (`StorageEngine` with no `YADGAR_DB_URL`, the corruption-restore path, `yadgar stats --db`). Removing it would trade a fixed bug for a new one.

### 1.4 Would it still be warn-only if it recurred? Yes — by design

`_step_consolidation` returns `30` on any exception (`nightly_cycle.py:306-314`, `_log.warning(... "continuing")`), and `main()` records it as `first_failure` but proceeds through vacuum → post-backup → prune (`:501-510`). So a recurrence would still be non-fatal.

**What would silently not run:** `run_nightly_consolidation` — i.e. the whole nightly consolidation pipeline (decay, episode formation, merge, CLS promotion, causal), *plus* the core tail it drives: graph-layout precompute, `check_invariants`, and auto-vacuum. That is the honest answer to *"what has not been running for two months if this is still broken"* — and the answer is **nothing**: it was not broken.

Note the warn-only policy is not universal any more — `_step_vacuum` was deliberately made red-on-rollback in P0 #37 (`:343-355`). Whether step 3 deserves the same treatment is a separate policy question, **not** part of this task, and it should not be smuggled in here.

### 1.5 Test coverage for the nightly cycle's imports

- `yadgar/tests/scripts/test_nightly_cycle_module.py` imports `yadgar.core.scripts.nightly_cycle` at module scope (`:24`), so any `ModuleNotFoundError` anywhere in that chain fails collection. Companions: `test_nightly_cycle.py`, `test_nightly_maintenance.py`, `test_v5_67_nightly_fixes.py`.
- **Gap:** those run in the dev venv (`uv sync`, all extras present), which cannot distinguish "declared dependency" from "happens to be installed". The wheel tests do not close it either — `test_v5_46_10_wheel_bundle.py` inspects the `.whl` as a **zipfile** (`:22 import zipfile`) and never installs it; `test_v5_46_3_sbom_wheel_install.py` is a static grep over `.forgejo/workflows/ci-release.yaml`; `test_v5_46_11_pipx_cli_invocation.py` is a static regex over `scripts/install/yadgar-setup.sh` (`:39 REPO_ROOT / "scripts" / "install" / "yadgar-setup.sh"`).

So **no test installs the wheel into a clean venv and imports the console-script entry points.** That is the class of gap that let the original bug ship. See §4 — but it is optional hardening for a two-month-dead bug, not required work.

---

## 2. Second finding — nightly consolidation does not run at all on a pipx/non-nix Linux install

task:0070 asks two questions. §1 answers the first. This is the second, and it is in scope.

Three installer surfaces render units. Only one omits the nightly cycle:

| surface | nightly unit rendered? | evidence |
|---|---|---|
| nix home-manager | **yes** — service + timer, 19:00 UTC | `flake.nix:606-635` |
| macOS launchd | **yes** — plist + wrapper | `scripts/install/generate_launchd.sh:110-111`, `:128`, `:158` |
| Linux pipx / `yadgar-setup.sh` | **no** | `scripts/install/generate_systemd.sh:52`, `:85-86`, `:106-107` — renders `yadgar.service` and `yadgar-backend.service` only. There is no `yadgar-nightly-cycle.service.in` or `.timer.in` in `scripts/install/` at all. |
| `yadgar daemon` Python renderer | **no** | `yadgar/core/daemon/systemd.py:25` `install_systemd_service` writes exactly two units (`:153-154`) |

The console script *exists* on every install (`pyproject.toml:82` → `/root/.local/bin/yadgar-nightly-cycle` verified present on the VM). Nothing ever invokes it on Linux-non-nix.

**Is this a documented decision or a gap?** Gap, on everything checked. `docs/CHANGELOG.md:1040` records the nix flake taking ownership of "nightly/vacuum systemd units running the pipx binary… Fully declared (no `yadgar-setup`)" — that documents what nix does, not a decision that the repo installer should omit it. `:1947` shows the macOS port explicitly mapped "All 7 nix systemd unit groups", nightly included — i.e. launchd achieved nix parity and `generate_systemd.sh` was simply never brought along.

*Scope of the search, stated precisely so a reader can extend it:* `docs/reference/decisions.md` (grep `nightly` — only consolidation-content and exit-code entries, nothing on unit generation), `docs/CHANGELOG.md` (grep `nightly` × unit/timer/systemd/nix/install), the ADR index via `adr_list` (titles), and two semantic searches over ADR **bodies** (`wiki_query`, `recall(type="wiki")`) for nightly/timer/scheduling/installer-parity. None surfaces an intent to omit. Not exhaustive over all ~175 ADR bodies — if one exists, D2 still resolves cleanly (the new task closes as wontfix).

A cron path was also ruled out: `CHANGELOG:169` says "nightly systemd timer / cron", but no `cron`/`crontab` reference exists in `scripts/install/` or in non-test Python. The systemd/launchd units are the only schedulers.

This is the **same generator, same class** as task:0044(b) ("repo non-nix systemd install ships NO vacuum `.path` watcher → `vacuum_now` inert there"). Different unit, different trigger mechanism — keep them as separate items, but they should be worked together.

Evidence note: the VM has no `~/.config/systemd/user/` at all, but `yadgar setup` never completed there (see task:0082/0083 — setup blocks on the code_graph prompt, and `daemon start` dies on a podman-only host). **The generator source is the hard evidence; the VM's missing directory is not.**

---

## 3. Recommended action — NO car required; closure only

The train brief asked for one bounded car. Honest answer: **there isn't one.** Nothing ships — no source change, no test, no version bump. A "car" whose entire content is *archive a doc + add a CHANGELOG line* is not a car; the convention in this repo (`docs/plans/archive/fix-systemd-generate-missing-queue-base-2026-07-28.md`, header "ships with this PR") is that a plan archives **alongside** real code. Here there is none, and the brief explicitly said to say so plainly rather than invent work.

What remains is closure bookkeeping, which can ride any car in the train (or land on its own):

**Files touched (exclusive):**
- `docs/plans/fix-nightly-cycle-surrealdb-import-2026-07-29.md` (this file, → `docs/plans/archive/` on merge)
- one `docs/CHANGELOG.md` entry
- task-list update for task:0070 via `wiki_write_task_list` (not a repo file)

**No source file is touched.** The seam statement is still the useful part — zero overlap with the train's existing cars:

| train car owns | this car touches |
|---|---|
| `yadgar/core/daemon/` | no |
| `yadgar/core/cli/daemon.py` | no |
| `yadgar/core/cli/setup.py` + `yadgar/core/install/` | no |
| `yadgar/core/scripts/nightly_cycle.py` | **no** |

Overlap disclosure for the §2 finding *if the user chooses to fix it in this train instead*: the fix would land in `scripts/install/generate_systemd.sh` + two new `.in` templates, and would touch a **second** renderer at `yadgar/core/daemon/systemd.py` — which **is** inside `yadgar/core/daemon/`, owned by the train's daemon car. That is a real collision and the main reason §6 recommends against bundling.

---

## 4. Optional hardening (recommend NOT doing it now)

A clean-venv wheel-install smoke test would close the §1.5 gap: build the wheel, `pip install` it into a throwaway venv with **no** dev extras, then import each `[project.scripts]` target (`yadgar.__main__`, `yadgar.core.scripts.nightly_cycle`, `yadgar.core.scripts.yadgar_setup`). It would have caught the 2026-05-29 bug pre-merge and would catch the next undeclared core-path import.

Recommend **deferring**: it guards a bug that has been dead two months, it is not what task:0070 asked for, and it adds a slow (network + build) test to a train whose other cars are unrelated. Better as a standalone item alongside the §2 work, where the install path is already the subject.

---

## 5. Explicitly NOT in scope

- **task:0042** (vacuum `:8080` reachability / weekly-vs-nightly). The anchor mentions `HTTP 307` in the same breath, but it is separately tracked and separately root-caused (`docs/CHANGELOG.md:3135` already records one fix in this area). Do not merge.
- Changing step 3's warn-only policy (§1.4).
- Removing the `surrealdb` dependency (§1.3 — still needed by embedded mode).

---

## 6. Open decisions for the user

**D1 — Close task:0070?**
Options: (a) close as "already fixed 2026-05-29, verified on live 5.168.0"; (b) keep open pending some further check.
**Recommend (a).** Two independent lines of evidence (commit timeline + live pipx import on 5.168.0) and a third structural one (the path no longer reaches the import at all). There is no further check that would change the answer.

**D2 — Where does the §2 nightly-unit gap go?**
Options: (a) new task, cross-linked to task:0044(b), worked later on an install-focused train; (b) a car on `feat/v5.169-install-runtime-fixes` now; (c) fold into task:0044.
**Recommend (a).** It is 0044's sibling, not 0070's child — 0070 is about an import, this is about scheduling. And (b) collides with the train's daemon car over `yadgar/core/daemon/systemd.py` (§3). (c) muddies 0044's specific `.path`-watcher scope.
*User input genuinely needed here* — this is a "does nightly maintenance run on your non-nix machines" question, and only the user knows whether any such machine exists in practice or whether nix + macOS covers the whole real fleet. If the fleet is nix-only, this is documentation, not a bug.

**D3 — Build the clean-venv wheel-import test (§4)?**
Options: (a) defer to the §2 work; (b) build now.
**Recommend (a).**

**D4 — CHANGELOG entry for a no-ship verification, y/n?**
**Recommend yes** — one line, so a future reader hitting mem 519136 lands on this page instead of re-running the investigation. Archiving the plan is not optional either way; only the CHANGELOG line is the question.

---

## 7. Acceptance criteria

Given the recommendation is "close the task", the criteria are verification criteria, not build criteria.

- **[manual] AC-1** — `pyproject.toml` contains `surrealdb>=1.0.0` under `[project.dependencies]` (not an extra). ✅ verified: `pyproject.toml:71`.
- **[manual] AC-2** — on a real pipx install of the current release, `python -c "import surrealdb"` in the pipx venv succeeds. ✅ verified: VM 192.168.122.101, surrealdb 2.0.0 / yadgar 5.168.0.
- **[manual] AC-3** — on the same install, importing `yadgar.core.scripts.nightly_cycle` succeeds. ✅ verified.
- **[unit] AC-4** — `yadgar/tests/scripts/test_nightly_cycle_module.py` collects and passes (its module-scope `import yadgar.core.scripts.nightly_cycle` is itself the assertion). ✅ verified: `uv run pytest … --noconftest -q` → **23 passed in 1.12s**. Pure-mock (patches `subprocess.run`), no DB — safe to run alongside the task:0081 QA under the mem 531755 concurrency rule; confirmed no other pytest/e2e was running.
- **[manual] AC-5** — task:0070 marked `completed` with the timeline + live-verification evidence in its description; §2 finding filed per D2.
- **[manual] AC-6** — this plan moved to `docs/plans/archive/`; CHANGELOG line added per D4.
- **[e2e] AC-7 — DEFERRED, not part of this car.** Clean-venv wheel install imports every `[project.scripts]` entry point without error. Gated on D3.

---

## 8. Risks

| # | risk | severity | mitigation |
|---|---|---|---|
| R1 | Closing 0070 hides a *different* nightly failure the user actually experiences | med | The §2 finding is the likely real cause of "nightly doesn't seem to run" on non-nix hosts. Filing it (D2) is what keeps the concern alive after 0070 closes. Do not close 0070 without resolving D2. |
| R2 | Scope creep — the §2 finding becomes a de-facto car mid-train | med | §3 states no car is required and §6/D2 recommends a separate task. If the user picks D2=(b), re-plan it properly against the daemon car's ownership of `yadgar/core/daemon/systemd.py`. |
| R3 | Someone later "cleans up" the now-vestigial-looking `surrealdb` dep | low | §1.3 records why it stays (embedded mode, corruption-restore, `stats --db`). |
| R4 | Live evidence came from one VM whose install is known-incomplete (task:0082/0083) | low | The pipx venv contents and the Python import are independent of whether `yadgar setup` finished — pipx installs the venv, setup only writes units/config. AC-2/AC-3 are unaffected. Unit-generation claims (§2) rest on generator **source**, not on that VM. |
| R5 | Two-month-old anchors keep generating dead investigations | low | Meta-observation, not this car's job: the anchor was tagged `follow-up` and expired without ever being reconciled against the fix that landed 2 h later. Worth a line in whatever anchor-hygiene work task:0043 produces. |
