> ARCHIVED 2026-07-29 — executing on fix/systemd-generate-queue-base, ships with this PR

# Fix: real-install systemd/launchd backend units omit `YADGAR_QUEUE_BASE` → queue drainer dead

**Task:** harness #72 — fresh-VM QA (2026-07-28) found the end-user install path generates a backend unit with **zero** `YADGAR_QUEUE_BASE` → drainer disabled → queued writes never commit.
**Status:** DONE (v5.167.1 / backend 5.58.8) — Car 1 (the two `.in`/plist template fixes) + Car 2 (regression tests, incl. the cross-generator anti-drift test) + Car 3 (CHANGELOG + MIGRATION_NOTES.md) all shipped in one PR, per §6 option (A). Bundled the ADR-0084 `safe_start/__main__.py` fix per §5/§7. The `/admin` 503 / `StorageEngine not initialized` symptom (§5) and the consolidate-vs-patch follow-up (§6 option B) remain open, tracked separately.
**Builds on:** [[yadgar-adr-0075]] (backend queue via shared `/data`, `YADGAR_QUEUE_BASE` semantics pin), ADR-0078 (DB-isolation: drainer lives ONLY in the backend process), ADR-0084 (no-lone-files packaging — relevant to the *separate* safe_start bug below).
**Related:** `docs/plans/hook-install-hygiene-2026-07-13.md` (sister install-hygiene train, #64).

---

## 1. The problem

On an isolated Debian 13 VM, a real `yadgar-setup` install of yadgar 5.167.0 (systemd + podman) produced:

- `~/.config/systemd/user/yadgar.service` and `yadgar-backend.service` — **both had `grep -c YADGAR_QUEUE_BASE == 0`** (reverified by the orchestrator over SSH).
- Backend log: `queue_drainer_disabled — "YADGAR_QUEUE_BASE unset (production backend must set it — R3 Car 0)"`.
- Symptom A: `memory_stats.total_memories` stayed `0` after setup reported "Seeded 8 anchors" — the anchors were **enqueued** but never **drained** (drainer off).
- Symptom B (co-occurring, **separate** — see §5): some `/admin` writes (`block_create`, seed-agent-prompts) returned 503/500 with `StorageEngine not initialized`, and `safe_start` preflight logged `No module named yadgar.backend.safe_start.__main__`.

`docker-compose.yml` (the reference deployment) sets `YADGAR_QUEUE_BASE: /queue-data` and works; the generated units do not.

---

## 2. Root cause (confirmed against code — cite file:line)

### 2.1 The bug is in the unit *template*, not the shell script

The end-user install (`scripts/install/yadgar-setup.sh` → `_step_generate_units()`) runs `scripts/install/generate_systemd.sh`. That script is a **dumb `sed` renderer** — it only substitutes `@RUNTIME@ / @IMAGE@ / @BACKEND_IMAGE@ / @DATA_DIR@ / @SECRETS_ENV_FILE@` into `.in` templates (`scripts/install/generate_systemd.sh:74-81`). It adds **no** env vars of its own.

The omission lives in the template it renders:

- `scripts/install/yadgar-backend.service.in:11-22` — the backend `ExecStart` lists `SURREAL_USER/PASS`, `YADGAR_RW_USER/PASS`, `YADGAR_RO_USER/PASS` and **nothing else**. No `-e YADGAR_QUEUE_BASE=…`. It mounts only `-v @DATA_DIR@:/data` (`:14`); there is no `/queue-data` mount.

So every unit `generate_systemd.sh` renders is born without the var. `grep -c YADGAR_QUEUE_BASE scripts/install/yadgar-backend.service.in == 0` on current `master` (HEAD `805f5e4b`).

### 2.2 What consumes the var, and how it fails

`yadgar/backend/embed_service/embed_service_lifecycle.py:41-51` — `_queue_base_path()`:

```python
base = os.environ.get("YADGAR_QUEUE_BASE", "").strip()
return Path(base) if base else None
```

**No fallback** (deliberate — docstring `:44-48`: on the backend `/data` may be the read-only DB mount, and unit tests always set `YADGAR_DATA_DIR`, so a fallback would silently start a drainer where none belongs). Unset → `None` → `_start_queue_drainer()` (`:70-80`) sets the gauge to 0, logs `queue_drainer_disabled`, and returns — **fail-loud but non-fatal**: the embed/rerank service still serves. Nothing else re-enables the drainer. Result: queued `memorize`/`wiki_add`/anchor writes sit in `base/queue` forever and `memory_stats` never moves. This exactly matches the VM's Symptom A.

Core's side, by contrast, needs no `YADGAR_QUEUE_BASE`: it builds its enqueue FileQueue directly from `YADGAR_DATA_DIR` — `yadgar/core/lifecycle/lifecycle.py:61-62`:

```python
base = Path(os.environ.get("YADGAR_DATA_DIR", _settings.DATA_DIR))
fq = FileQueue(base, wiki_prefix=_settings.WIKI_SLUG_PREFIX)
```

Core's `.in` sets `YADGAR_DATA_DIR=/data` (`yadgar.service.in:24`), so core enqueues to `/data/queue`. (The `cache_epoch.py:52` `YADGAR_QUEUE_BASE or YADGAR_DATA_DIR` resolution is a separate consumer, but the write-enqueue base is `lifecycle.py:61`.) The asymmetry is the trap: core writes to `/data/queue`, backend — with `YADGAR_QUEUE_BASE` unset and no fallback — can't see it.

### 2.3 The value must be `/data` here — NOT `/queue-data`

`YADGAR_QUEUE_BASE` is the FileQueue **base**; the pending dir is always `base/queue` (ADR-0075 semantics pin). Core and backend must resolve the **same physical** `base/queue`. Two *both-correct* layouts exist in the repo:

| Generator | core mount | backend mount | `YADGAR_QUEUE_BASE` | status |
|---|---|---|---|---|
| **`.in` templates** (`generate_systemd.sh`) | `@DATA_DIR@:/data` (host bind) | `@DATA_DIR@:/data` (SAME host bind) | **MISSING → must be `/data`** | **BROKEN (this bug)** |
| **launchd plists** (`generate_launchd.sh`) | `@YADGAR_INSTALL_PREFIX@:/data` | `@YADGAR_INSTALL_PREFIX@:/data` (SAME) | **MISSING → must be `/data`** | **BROKEN (same bug, 2nd surface)** |
| Python `core/daemon/systemd.py` | `{volume}:/data` (named vol) | `{volume}:/queue-data` (SAME named vol) | `/queue-data` (`:93`) | ✅ correct |
| `core/daemon/daemon.py::start_backend` | `yadgar-data:/data` | `yadgar-data:/queue-data` | `/queue-data` (`:270-272`) | ✅ correct |
| `docker-compose.yml` | `yadgar-queue-data:/data` | `yadgar-queue-data:/queue-data` | `/queue-data` (`:62`) | ✅ correct |
| nix `modules/home/yadgar.nix` (out-of-repo) | `…:/data` (shared) | `…:/data` (shared) | `/data` | ✅ correct |

**Critical:** because the `.in` template and launchd plist bind-mount the *same host dir* into **both** containers at `/data` (there is no `/queue-data` mount), the only correct value for these two surfaces is `YADGAR_QUEUE_BASE=/data`. Copy-pasting `=/queue-data` from `systemd.py:93` would produce a unit that *still* doesn't drain — the backend would watch a container-local, unmounted `/queue-data/queue` while core writes to the shared `/data/queue`. This matches ADR-0075 (`/data`) and the nix path (`/data`).

Confirmed by a 2026-07-23 gotcha memory (id 532967): #233 wired the daemon-start + Python systemd paths; it explicitly listed `scripts/install/yadgar-backend.service.in` **and the launchd plist** as *still-unwired surfaces*. This plan closes exactly those two.

---

## 3. Proposed fix (minimal, in-place) — RECOMMENDED

### Car 1 — patch the two unwired templates [core, no version bump — install assets only]

1. `scripts/install/yadgar-backend.service.in` — add after `:20` (the `YADGAR_RO_PASS` line), before `--memory`:
   ```
       -e YADGAR_QUEUE_BASE=/data \
   ```
2. `scripts/install/launchd/com.openfantasy.yadgar-backend.plist.in:49` — insert `-e YADGAR_QUEUE_BASE=/data ` into the `run` arg string (before `--memory`).

Value `/data` for both (per §2.3). One-line each. No `.sh` change — the renderers already pass templates through untouched.

### Car 2 — regression test that RUNS the generators (see §4)

### Car 3 — CHANGELOG entry + note in `MIGRATION_NOTES.md` for existing installs

Existing broken installs need the operator to re-run the unit generator (or hand-add the var and `systemctl --user daemon-reload && restart yadgar-backend`). Document the manual remediation; do not auto-apply.

---

## 4. Regression test (the gap that let this ship)

`yadgar/tests/scripts/test_v5_45_generate_systemd.py` exists and *runs* `generate_systemd.sh` into a tmp dir — but it only asserts file existence, runtime substitution, and the nix-symlink guard. **It never asserts any env var content** (`grep -c QUEUE_BASE == 0` in that test file). That is precisely the gap.

**Proposed — run the real generator, assert the wired base is a mounted path (not a hand-written string literal):**

- `test_generate_systemd_backend_sets_queue_base` [unit] — invoke `generate_systemd.sh` (reuse the existing `_run_generate_systemd` harness), read the rendered `yadgar-backend.service`, assert `YADGAR_QUEUE_BASE=` is present **and** its value is a path that also appears as a `-v <host>:<value>` mount target in the same unit (proves the base is actually mounted, catching the `/queue-data`-with-no-mount mistake too).
- `test_generate_launchd_backend_sets_queue_base` [unit] — same assertion against `generate_launchd.sh`'s rendered backend plist (reuse `test_v5_45_1_launchd_render.py` harness).
- **Cross-generator guard** [unit] — parametrize over every in-repo backend-unit generator (`generate_systemd.sh`, `generate_launchd.sh`, and the Python `install_systemd_service`) and assert each renders a `YADGAR_QUEUE_BASE` whose value is a mount target in its own output. This is the anti-drift net: a future generator that forgets the var fails one shared test. (Out-of-repo nix is not reachable from pytest — cover it with a one-line note in `modules/home/yadgar.nix` referencing this contract.)

Because these tests execute the actual scripts and cross-check mount↔base coherence, they cannot drift from the real output the way a literal-string assertion would.

---

## 5. The `/admin` 503 / `StorageEngine not initialized` symptom is SEPARATE (do not fold in)

**Conclusion: independent of the missing `YADGAR_QUEUE_BASE`.** Evidence:

- The drainer path is designed fail-open — `_start_queue_drainer` returns `None` and the embed service *still serves* (`embed_service_lifecycle.py:66,80`). A disabled drainer cannot make `/admin` return 503 or make `StorageEngine` fail to initialize; those are a different code path (the write-forward storage init), not the queue.
- Symptom A (memory_stats stuck at 0) is *sufficiently caused* by the queue bug — writes enqueue fine, never drain. Note it is **not** the sole active blocker on that VM: even with `YADGAR_QUEUE_BASE` set, the drainer must build storage to commit (`_start_queue_drainer` calls `_es._ensure_recall_engines()` and `QueueDrainer(fq, _get_storage, …)` at `embed_service_lifecycle.py:109-125` *before* `drainer.start()`). If Symptom B is present, that construction throws → `queue_drainer_start_failed`, or starts but can't replay → `memory_stats` still 0. So the queue fix is necessary but, on the exact VM, not sufficient (see §7 gating).
- Symptom B (503/`StorageEngine not initialized`) is a backend **init** failure — the storage engine the `/admin` forward path needs never came up. Setting `YADGAR_QUEUE_BASE` will not fix it.

**Third finding (also separate, precisely diagnosed):** the VM's `No module named yadgar.backend.safe_start.__main__` is a real regression from ADR-0084's packaging. `entrypoint-backend.sh:156,209` invokes `python3 -m yadgar.backend.safe_start preflight|recover`. When `safe_start` was a flat module (`safe_start.py`), `-m` ran its `if __name__ == "__main__"` block. ADR-0084 (T2 Car D) converted it into a **package** (`yadgar/backend/safe_start/{__init__.py, safe_start.py}`) with a PEP-562 re-export `__init__`, but added **no `__main__.py`**. `python -m <package>` requires `<package>/__main__.py` — absent → the exact error observed. The `main()` entry now lives at `safe_start/safe_start.py:253` and is unreachable via `-m yadgar.backend.safe_start`.

- This is **fail-open** at preflight (`entrypoint-backend.sh:164` — "continuing (fail-open on tool error)"), so it does not by itself block startup — **but the split-brain / torn-manifest preflight guard is silently DEAD**, and the auto-restore `recover` path (`:209`) is equally dead.
- **Possible cascade into Symptom B (unproven):** if SurrealDB failed to start on the VM (e.g. torn manifest), the `recover` auto-restore would normally fix it — but `recover` hits the same missing-`__main__` failure, so surreal never recovers → `StorageEngine not initialized` → `/admin` 503. This is a *plausible link*, not confirmed; it needs the VM's surreal/entrypoint logs to settle.

**Recommendation:** track Symptom B and the `safe_start.__main__` regression as their **own tasks**, separate from this plan:
- **New task (P1, trivial, high-value):** add `yadgar/backend/safe_start/__main__.py` (`from .safe_start import main; raise SystemExit(main())`) + a test that `python -m yadgar.backend.safe_start --help` exits 0. This resurrects the split-brain guard. Likely a quick win and possibly the actual root of Symptom B.
- **New investigation task:** reproduce the `/admin` 503 / `StorageEngine not initialized` on a fresh VM with full backend+entrypoint+surreal logs; confirm/refute the cascade above.

---

## 6. Open decisions for the user

1. **Consolidate vs patch (the core question).** There are **five** in-repo "generate a backend unit" implementations (`generate_systemd.sh`+`.in`, `generate_launchd.sh`+plist, Python `install_systemd_service`, `daemon.py::start_backend`, `docker-compose.yml`) plus out-of-repo nix — independently maintained, and this is the *second* time the env-var wiring landed in some and not others (#233 fixed three, missed two). Options:
   - **(A) Minimal patch (RECOMMENDED for now):** fix the two unwired templates + add the cross-generator regression test (§3/§4). Lowest risk, unblocks real installs immediately, and the cross-generator test becomes the anti-drift net so a *future* forgotten var fails CI even without full consolidation.
   - **(B) Consolidate to a single source of truth:** have the shell/launchd renderers emit from the same spec the Python generator uses (or vice-versa). Correct long-term, but a real refactor across install surfaces with its own regression risk — over-scoped as the *only* response to a one-line bug. Recommend deferring to a dedicated plan, gated on (A) landing first.
   - Recommendation: **do (A) now; open a follow-up plan for (B)** and reference the cross-generator test as the interim guard.
2. **Volume convention: unify on `/data` vs keep both?** The `.in`/launchd/nix surfaces use the shared-`/data` convention (`QUEUE_BASE=/data`); systemd.py/daemon.py/compose use the separate-`/queue-data`-volume convention (`QUEUE_BASE=/queue-data`). Both are correct (memory 532967 warns: "Do NOT unify them blindly"). Do you want the fix to *only* wire `/data` into the two broken surfaces (keeps both conventions, minimal), or to standardize all surfaces on one convention (larger blast radius)? Recommend the former.
3. **Existing broken installs:** ship a `MIGRATION_NOTES.md` remediation snippet (re-run generator or hand-add var + `daemon-reload`/restart) — confirm you want operator-run steps rather than any auto-remediation.

---

## 7. Acceptance criteria

**True gate for THIS plan (verifiable in isolation — these define "done"):**
- [unit] `generate_systemd.sh` rendered `yadgar-backend.service` contains `YADGAR_QUEUE_BASE=/data`, and `/data` is a `-v` mount target in the same unit.
- [unit] `generate_launchd.sh` rendered backend plist contains `YADGAR_QUEUE_BASE=/data`, mounted-path-coherent.
- [unit] cross-generator parametrized test passes for all in-repo backend-unit generators (anti-drift).
- [manual] fresh-VM `yadgar-setup` install → `grep -c YADGAR_QUEUE_BASE ~/.config/systemd/user/yadgar-backend.service >= 1` (the render is present in the real installed unit).

**Gated on a healthy backend (Symptom B / safe_start resolved — NOT a gate for this plan's unit fix):**
- [manual] backend log shows `queue_drainer_started` (not `queue_drainer_disabled` and not `queue_drainer_start_failed`).
- [e2e] after fresh install + a seed write, `memory_stats.total_memories > 0` within one drain interval (proves the enqueue→drain→commit loop is live end-to-end). *This can only pass once storage initializes — see the sequencing note below.*
- (Tracked separately) safe_start `-m` invocation exits 0; `/admin` 503 root-caused with VM logs.

**Sequencing.** The `safe_start/__main__.py` fix (§5) is a trivial one-file add and is the leading candidate for the actual root of Symptom B (dead `recover` path). **Recommend it lands first, or in the same train, as this queue fix** — otherwise the [e2e]/`queue_drainer_started` criteria above remain unverifiable on the fresh VM (storage never comes up → drainer can't commit even when correctly enabled). This plan's own unit gate does not depend on that ordering; the end-to-end validation does.

## 8. Risks

- **Wrong base value.** Setting `/queue-data` on the `.in`/launchd surfaces (no such mount) would silently keep the drainer broken — the mount↔base coherence assertion in §4 is the guard.
- **SELinux/`:Z` on the bind mount.** The backend `.in` already relies on `--security-opt label=disable`; adding an env var doesn't touch mounts, so no new SELinux surface. (Prior template churn `18e970d7`/`9c27c018` dealt with `:Z` — out of scope here.)
- **Fixing only Symptom A.** This plan makes the drainer run; it does **not** fix Symptom B (`/admin` 503). A fresh-VM retest may still fail writes until the separate backend-init/safe_start bugs are addressed — set expectations accordingly.
- **Convention drift persists** if (B) is declined — mitigated (not eliminated) by the cross-generator test.
