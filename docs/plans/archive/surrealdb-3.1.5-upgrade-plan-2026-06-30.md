> ARCHIVED 2026-07-09 — SHIPPED as b02f6397 (#136): backend+CI images on 3.1.5; prod verified live (surreal version = 3.1.5). Header was never updated at ship time (pre-ADR-0081).

# SurrealDB server upgrade 3.0.5 → 3.1.5 — implementation plan

Date: 2026-06-30
Status: implementation plan (NO code changes in this doc — plan only)
Gating: user gates implementation on `advisor` returning NO issues on this plan.
Builds on: `docs/plans/daemon-async-io-and-surreal-upgrade-2026-06-30.md` (decision doc —
concluded 3.1.5 recommended, low-risk in-place roll-forward, GraphQL-only announced break,
flagged auth/header migration as UNVERIFIED). This doc VERIFIES that open item and turns
the decision into a concrete change list.

---

## TL;DR

- Upgrade the **SurrealDB SERVER binary** from `v3.0.5` → `v3.1.5` (released 2026-06-19,
  a security-focused patch line on top of 3.1.0 "operational maturity").
- The version is pinned in **6 hard locations** (not 3 as the prior doc estimated):
  the backend image binary copy, the CI image download (version **+ SHA256 + URL**), the
  restore script, plus version strings in docs/benchmark. The CI image **digest tag**
  (`yadgar-ci:5.72.0`) and the **backend image version** must also be re-rolled because the
  surreal binary is baked into both images.
- **Auth/header surface — VERIFIED UNCHANGED** (the gated risk). yadgar's exact `/sql`
  headers match the current 3.x documented surface. The `ns`/`db` → `surreal-ns`/`surreal-db`
  rename was a **2.x** break, already absorbed (prod runs 3.0.5). No code change to the
  HTTP/auth path.
- **On-disk format — VERIFIED in-place roll-forward** (on-disk format only changes across
  MAJOR boundaries; 3.0→3.1 is minor). **Rollback = restore-from-backup, NOT binary
  downgrade** — back up before upgrade.
- **`surreal start` flags — VERIFIED unchanged** (`--no-banner --bind --user --pass --log`
  + `surrealkv://` backend all still valid in 3.x).
- **No SDK change required.** Prod speaks `httpx` `/sql` HTTP (server mode), never the
  `surrealdb` SDK. The SDK (`surrealdb>=1.0.0`) is embedded/dev/test-only and the existing
  pin already allows the latest. Optional, non-gating: bump floor to `>=2.0.0`.

---

## 1. Verification verdicts (the load-bearing risks)

### 1.1 Auth / HTTP headers — VERDICT: VERIFIED UNCHANGED (no code change)

**What yadgar sends** (from the inventory):

- `yadgar/storage/__init__.py:244-251` — server-mode httpx client default headers:
  ```
  Authorization: Basic {base64(user:pass)}
  surreal-ns: yadgar
  surreal-db: main
  Accept: application/json
  ```
- `yadgar/storage/client.py:474-476` and `:515-516` — per-`/sql`-POST adds
  `Content-Type: text/plain`, body is the raw SurrealQL text, endpoint path `/sql`.
- `entrypoint-backend.sh:102-106` and `:140-146` — bootstrap/snapshot curls send the
  identical surface: `Authorization: Basic …`, `Surreal-NS: yadgar`, `Surreal-DB: main`,
  `Content-Type: text/plain`, `POST /sql`.
- Auth model note `yadgar/storage/__init__.py:84` — root user is defined `ON ROOT` because
  "SurrealDB v3 only supports HTTP Basic auth" — i.e. the code is already written to the
  v3 auth contract.

**What 3.1.x documents** (current SurrealDB 3.x HTTP docs):
`POST /sql` requires `Accept` (response content-type), `Surreal-NS`, `Surreal-DB`, and
optional `Authorization` (Basic auth supported, demonstrated with `curl -u`). This is the
**exact surface** yadgar uses. CONFIRMED against
<https://surrealdb.com/docs/surrealdb/integration/http>.

**Why the gated risk is closed:** the historical `ns`/`db` → `surreal-ns`/`surreal-db`
header rename and the Basic-auth-only constraint were **2.x** breaking changes
(<https://surrealdb.com/docs/surrealdb/installation/upgrading/migrating-data-to-2x>),
already absorbed since prod runs 3.0.5. 3.0→3.1 is a minor bump; the documented 3.x `/sql`
header set is identical to what 3.0.5 used. **No auth/header code change is in this plan.**

Confidence: **CONFIRMED** (positive evidence — current docs show the identical surface;
the only known auth break is the already-absorbed 2.x rename), not mere absence-of-evidence.

### 1.2 On-disk / catalog compatibility — VERDICT: VERIFIED in-place roll-forward

- Official upgrade guidance: "stop the server gracefully, replace the `surreal` binary with
  the new release, then start with unchanged data paths." On-disk formats change only at
  **major** version boundaries; 3.0→3.1 is minor → datadir is untouched.
  <https://surrealdb.com/docs/surrealdb/installation/upgrading>
- The 3.1 announcement: "the catalog and on-disk layouts are unchanged from 3.0.x, so
  existing deployments roll forward in place." <https://surrealdb.com/3.1>
- yadgar's datastore is `surrealkv:///data/surreal_db` (embedded single-node KV, persisted
  on the backend volume) — exactly the in-place case.

Confidence: **CONFIRMED** (two corroborating official sources).

### 1.3 Rollback — VERDICT: restore-from-backup, NOT binary-downgrade

- The official upgrade doc recommends taking a backup before upgrade so you can revert "if
  migration or client incompatibility surfaces after deploy." It does **not** document or
  support downgrading the binary to a previous minor on the same datadir.
- Honest framing: within 3.x the on-disk format is not deliberately broken, so a downgrade
  is not *physically* format-blocked — BUT SurrealDB does not test/support 3.1→3.0 downgrade,
  and 3.1 may write metadata 3.0.5 cannot read back. **Do not rely on binary downgrade.**
- **Rollback path = restore the pre-upgrade backup** (`surreal export` logical dump and/or a
  volume/storage snapshot of `/data/surreal_db`), then redeploy the 3.0.5 images.

Confidence: **CONFIRMED that the safe rollback is restore-from-backup**; binary-downgrade is
**unsupported/untested — flagged, do not depend on it.**

### 1.4 SQL / query compatibility — VERDICT: INFERRED low-risk (verify the specific usages)

The full enumerated 3.1.x changelog is **not machine-retrievable** (GitHub release pages
render via JS; the API `body` for v3.1.0 is the stub `"Release 3.1.0"`; no top-level
`CHANGELOG.md` at the tag). So "no SQL break" is **INFERRED** from (a) the patch-release
nature of 3.1.x, (b) the 3.1 announcement enumerating only a **GraphQL** break (unused —
yadgar speaks `/sql`). It is **not** independently verified line-by-line.

What yadgar's `/sql` actually relies on, and the targeted verification done:

- `surreal start --no-banner --bind --user --pass --log` + `surrealkv://` datastore —
  **VERIFIED unchanged** in current 3.x CLI docs
  (<https://surrealdb.com/docs/surrealdb/cli/start>). The launch command needs no change.
- Version-specific SQL quirks the code already works around (these are 3.0.5 *limitations*;
  a 3.1 that lifts them stays backward-compatible with the existing workarounds — confirm in
  e2e, no code change expected):
  - `MIGRATION_NOTES.md:1725` — `WHERE col IS NONE` matches only explicit-NONE rows in 3.0.5.
  - `MIGRATION_NOTES.md:2696, :2732` — `DEFINE INDEX … WHERE` (partial unique index) **not
    supported** in 3.0.5; schema drops the unconditional unique index instead.
  - `yadgar/graph_api.py:541` — must use `IS NOT NONE`, not `IS NOT NULL`.
  - `yadgar/storage/__init__.py:84` — users defined `ON ROOT` (v3 HTTP-Basic-auth constraint).

Confidence: **INFERRED** (GraphQL-only announced break + patch nature). **The e2e suite on
3.1.5 is the verification gate** (§5) — not a doc read.

### 1.5 Startup flags / config — VERDICT: VERIFIED unchanged (§1.4 above)

`--no-banner --bind --user --pass --log` and the `surrealkv://PATH` positional argument are
all current/non-deprecated in 3.x. No change to `entrypoint-backend.sh` or
`_surreal_runner.py` launch commands.

---

## 2. Exhaustive version-reference inventory (every hit, file:line)

### A. SERVER binary / image pins — MUST change to v3.1.5

| # | File:line | Current | New |
|---|-----------|---------|-----|
| A1 | `Dockerfile.backend:20` | `COPY --from=surrealdb/surrealdb:v3.0.5 /surreal /usr/local/bin/surreal` | `…:v3.1.5 …` |
| A2 | `Dockerfile.ci:38` | `ARG SURREAL_VERSION=v3.0.5` | `ARG SURREAL_VERSION=v3.1.5` |
| A3 | `Dockerfile.ci:39` | `ARG SURREAL_SHA256=48dbeba4896765f33e07acc25224073f0850c190872052b917ebda1f7b4375cb` | **new sha256 of `surreal-v3.1.5.linux-amd64.tgz`** (compute, §3) |
| A4 | `Dockerfile.ci:41` | download URL templated on `${SURREAL_VERSION}` | no edit — follows A2 automatically |
| A5 | `scripts/install/restore.sh:153` | `"surrealdb/surrealdb:v3.0.5" \` | `"surrealdb/surrealdb:v3.1.5" \` |
| A6 | `Dockerfile.ci:7, :36` | comments `# + SurrealDB v3.0.5 baked in …` | update comment string to v3.1.5 |

### B. Images whose TAG must be re-rolled (surreal binary baked in)

| # | File:line | Current | Action |
|---|-----------|---------|--------|
| B1 | `docker-compose.yml:39` | `image: openfantasy/yadgar-backend:${BACKEND_VERSION:-5.8.0}` | bump default after backend image rebuilt with 3.1.5 (e.g. 5.9.0) |
| B2 | `nix/modules/home/yadgar.nix:18` | `yadger_backend_version = "5.8.0";` | match B1 |
| B3 | `nix/modules/home/yadgar.nix:422` | `ExecStart … ${yadgar_registry}/yadgar-backend:${yadger_backend_version}` | follows B2 automatically |
| B4 | `.forgejo/workflows/ci-pr.yaml:35` | `image: docker.io/openfantasy/yadgar-ci:5.72.0` | bump after CI image re-baked with 3.1.5 (e.g. 5.73.0) |
| B5 | `.forgejo/workflows/ci-pr.yaml:56-57` | comment `# SurrealDB v3.0.5 … baked into yadgar-ci:5.72.0` | update version + tag in comment |

### C. Python SDK dep — OPTIONAL, non-gating

| # | File:line | Current | Action |
|---|-----------|---------|--------|
| C1 | `pyproject.toml:70-71` | `"surrealdb>=1.0.0",` | OPTIONAL bump floor `>=2.0.0` (embedded/dev only; existing pin already allows 2.0.0). NOT required for the server upgrade. If bumped, regen any lock and smoke-test the embedded path. |

### D. Server launch commands — VERIFIED unchanged, NO edit

| # | File:line | Command | Action |
|---|-----------|---------|--------|
| D1 | `entrypoint-backend.sh:59-65` | `surreal start --no-banner --bind 0.0.0.0:8000 --user … --pass … --log … surrealkv:///data/surreal_db` | none (flags valid in 3.1) |
| D2 | `yadgar/_surreal_runner.py:109-123` | `["surreal","start","--no-banner","--bind","127.0.0.1:{port}","--user",…,"--pass",…,"surrealkv://{data_dir}"]` | none |
| D3 | `yadgar/tests/conftest.py:279-310` | `surreal_server` fixture → `spawn_surreal()` (uses D2) | none (re-runs on whatever `surreal` is on PATH / baked into CI image) |

### E. HTTP `/sql` + auth surface — VERIFIED unchanged, NO edit

| # | File:line | Detail |
|---|-----------|--------|
| E1 | `yadgar/storage/__init__.py:244-251` | headers `Authorization: Basic`, `surreal-ns: yadgar`, `surreal-db: main`, `Accept: application/json` |
| E2 | `yadgar/storage/client.py:474-476, 515-516` | `POST /sql`, `Content-Type: text/plain`, raw SurrealQL body |
| E3 | `entrypoint-backend.sh:102-106, :140-146` | bootstrap/snapshot curls — same surface |
| E4 | `yadgar/storage/__init__.py:84` | `ON ROOT` user (v3 Basic-auth contract) |

### F. URLs / ports — informational, NO edit (unchanged across 3.0→3.1)

| # | File:line | Value |
|---|-----------|-------|
| F1 | `docker-compose.yml:83` | `YADGAR_DB_URL: http://yadgar-backend:8000` |
| F2 | `Dockerfile.backend:29` | `YADGAR_DB_URL=http://127.0.0.1:8000` |
| F3 | `Dockerfile:17` | `YADGAR_DB_URL=http://yadgar-backend:8000` |
| F4 | `yadgar/config_registry.py:165` | `ConfigEntry("YADGAR_DB_URL","http://127.0.0.1:8000","string")` |
| F5 | `yadgar/daemon.py:288` | `f"YADGAR_DB_URL=http://{backend_name}:8000"` |

### G. Docs / config / version strings — update for accuracy (not load-bearing)

| # | File:line | Current | Action |
|---|-----------|---------|--------|
| G1 | `docs/BENCHMARK_RESULTS.md:121` | `"surreal_version": "3.0.5 for linux on x86_64"` | leave as historical record OR annotate; update on next benchmark re-run |
| G2 | `docs/V5_46_9_HOTFIX_SCOPE.md:177, :273` | references `v3.0.5` bake | historical doc — leave (records the 5.46.9 decision); do not rewrite history |
| G3 | `docs/CI_SPEEDUP_AUDIT_2026_06_06.md:38` | `surrealdb-v3.0.5` cache key | historical — leave |
| G4 | `README.md:415` | "SurrealDB … BSL 1.1" (no version) | none |
| G5 | `MIGRATION_NOTES.md:1725, :2696, :2732` | 3.0.5 SQL-limitation notes | leave (accurate for 3.0.5; add a 3.1.5 follow-up note only if e2e shows behavior changed) |
| G6 | `docs/plans/daemon-async-io-and-surreal-upgrade-2026-06-30.md` | the decision doc | no edit — this plan supersedes its open item |

### H. Test-skip / harness context — informational

| # | File:line | Detail |
|---|-----------|--------|
| H1 | `.forgejo/workflows/ci-pr.yaml:77-78` | CI runs `-m 'not integration and not e2e'` — e2e needs a local surreal binary not in CI containers |
| H2 | `Makefile:286-294` | `make e2e` runs the behavior-contract suite against the local `surreal` binary; excluded from CI |

**Net required edits: A1–A6, B1–B5, optionally C1.** Everything in D/E/F is verified-stable
and gets NO edit. G/H are accuracy/context only.

---

## 3. Implementation steps (ordered)

### Step 0 — Back up first (mandatory; rollback depends on it)
- `surreal export` logical dump of the live datadir AND/OR a volume snapshot of
  `/data/surreal_db` from the running `yadgar-backend`. Store off-box. This is the **only**
  reliable rollback (binary downgrade is unsupported — §1.3).

### Step 1 — Compute the new CI SHA256 (no terraform, no infra mutation)
- Download `surreal-v3.1.5.linux-amd64.tgz` from the GitHub releases asset and
  `sha256sum` it. The URL template at `Dockerfile.ci:41` already uses `${SURREAL_VERSION}`,
  so only the version arg (A2) + the sha (A3) change. Record the value for A3.
  - Asset: `https://github.com/surrealdb/surrealdb/releases/download/v3.1.5/surreal-v3.1.5.linux-amd64.tgz`

### Step 2 — Edit the pins (A1–A6) on a branch
- `Dockerfile.backend:20` → `v3.1.5`
- `Dockerfile.ci:38` → `ARG SURREAL_VERSION=v3.1.5`; `:39` → new sha; `:7,:36` comment text
- `scripts/install/restore.sh:153` → `v3.1.5`
- (Optional C1) `pyproject.toml:71` → `surrealdb>=2.0.0`

### Step 3 — Re-bake the CI image, bump its tag (B4/B5)
- Build `Dockerfile.ci` with the 3.1.5 args → push `yadgar-ci:<next>` (e.g. 5.73.0).
- Update `.forgejo/workflows/ci-pr.yaml:35` to the new tag, and the comment at `:56-57`.
- NOTE: image build/push is an infra action — prepare commands, hand to user via
  `MIGRATION_NOTES.md` if any registry push needs explicit approval. (No terraform involved.)

### Step 4 — Rebuild the backend image, bump its version (B1–B3)
- Build `Dockerfile.backend` (now copying `surreal:v3.1.5`) → push
  `yadgar-backend:<next>` (e.g. 5.9.0).
- Update `docker-compose.yml:39` default + `nix/modules/home/yadgar.nix:18`.

### Step 5 — Deploy sequence (ordering vs the running datadir)
1. Back up (Step 0) — already done, re-confirm freshness.
2. Stop `yadgar-backend` gracefully (the running 3.0.5 holds the surrealkv lock).
3. Start the new `yadgar-backend:<next>` (surreal 3.1.5) against the **same** datadir volume
   — in-place roll-forward, no migration command (§1.2).
4. Health-check `GET /health`, then a probe `POST /sql` (`SELECT * FROM wiki_page;`) with the
   real headers — confirms auth + read path on 3.1.5.
5. Soak. The server bump is independent of the async refactor (ship standalone first).

### Step 6 — nix apply (HAND TO USER — do not auto-apply)
- The nix changes (B2) are home-manager module edits. Per the Apply/Import hard rule, prepare
  the edits in-file and hand the user the rebuild/switch command; do not run it.

---

## 4. Deploy / migration sequence summary

```
backup datadir (export + snapshot)            ← rollback insurance
   │
compute sha256(surreal-v3.1.5.linux-amd64.tgz)
   │
edit pins A1–A6  →  re-bake CI image (B4/B5)  →  rebuild backend image (B1–B3)
   │
stop 3.0.5 backend (release surrealkv lock)
   │
start 3.1.5 backend on SAME datadir (in-place roll-forward, no migrate cmd)
   │
GET /health  +  probe POST /sql with real headers
   │
soak  →  bump compose/nix version defaults  →  hand nix switch to user
```

Backend image and CI image are **independent** rebuilds (different Dockerfiles) — can be done
in parallel; both must land before the workflow/compose tag bumps reference them.

---

## 5. Test plan

1. **e2e on 3.1.5 (the real gate).** Put `surreal v3.1.5` on PATH locally and run
   `make e2e` (`Makefile:286-294`) — the behavior-contract suite drives the real `/sql`
   surface via the `surreal_server` fixture (`conftest.py:279-310` → `_surreal_runner.py`).
   This is where §1.4's INFERRED SQL-compat becomes verified empirically.
2. **Header/auth assertion as a test.** Add/keep a test that does the live
   `POST /sql` with `Authorization: Basic`, `Surreal-NS: yadgar`, `Surreal-DB: main`,
   `Accept: application/json`, `Content-Type: text/plain` and asserts a 200 + expected JSON
   shape against 3.1.5 — codifies the §1.1 verdict so a future surreal bump that changes the
   header contract fails loudly. (TDD: this is the failing-first test for the bump.)
3. **SQL-workaround regression checks.** Re-run the suites covering the `IS NONE` semantics
   (`MIGRATION_NOTES.md:1725`), the partial-unique-index workaround (`:2696/:2732`), and
   `graph_api.py:541` `IS NOT NONE` — confirm 3.1.5 didn't silently change those.
4. **CI image smoke.** After re-bake, confirm `yadgar-ci:<next>` has `surreal` v3.1.5 on PATH
   (`surreal version`) and the test job's `-m 'not integration and not e2e'` set still passes.
5. **Embedded path (if C1 taken).** Smoke the `surrealkv://` embedded SDK path under
   `surrealdb>=2.0.0` (CBOR wire) — dev/test only.

---

## 6. Rollback plan + limitation

- **Rollback = restore the Step-0 backup**, then redeploy the 3.0.5 images
  (`Dockerfile.backend` pinned back to v3.0.5, `yadgar-backend:5.8.0`, `yadgar-ci:5.72.0`).
- **Limitation (flagged):** binary-downgrade 3.1.5 → 3.0.5 **on the same datadir** is
  **unsupported/untested**. 3.1 may persist metadata 3.0.5 cannot read. Do NOT downgrade the
  binary in place. The supported revert is restore-from-backup onto a clean datadir running
  3.0.5. This is *why* Step 0 is mandatory and non-skippable.

---

## 7. Open risks (ranked)

1. **[LOW, was the gated risk — now CONFIRMED-closed] Auth/header break.** Verified unchanged
   (§1.1). Residual: a 3.1.x patch silently tightened auth — caught by the §5.2 live-auth test
   before deploy.
2. **[LOW–MED, INFERRED] Unannounced `/sql` SQL-semantics change.** Full 3.1.x changelog not
   machine-retrievable; "no break" is inferred from the patch nature + GraphQL-only announced
   break. Mitigated by §5.1 e2e + §5.3 regression on the known SQL workarounds — empirical,
   not doc-based.
3. **[MED, operational] Rollback is restore-only.** Binary downgrade unsupported (§6). The risk
   is operational (must have a fresh backup), not a code risk.
4. **[LOW] CI SHA256 mismatch.** A3 must be the real sha of the 3.1.5 tgz or the CI image build
   fails closed (good — fail-loud). Compute in Step 1.
5. **[LOW] Image-tag drift.** B1–B5 must all move together; a half-bumped set (new backend
   image, stale compose default) deploys the old surreal. Mitigated by the §4 ordering.
6. **[LOW, optional] SDK 2.0.0 CBOR embedded path** (only if C1 taken) — dev/test only, off the
   prod hot path; smoke in §5.5.
7. **[INFO] TiKV `count() GROUP ALL` perf issue (#7358)** — yadgar uses single-node
   `surrealkv`, not TiKV. Not applicable; noted for completeness.

---

## 8. Confidence ledger (CONFIRMED vs INFERRED)

| Claim | Confidence | Basis |
|-------|-----------|-------|
| `/sql` + Basic-auth + Surreal-NS/DB headers unchanged 3.0→3.1 | **CONFIRMED** | current 3.x HTTP docs match yadgar's exact headers; the only auth break was 2.x (already absorbed) |
| On-disk format unchanged, in-place roll-forward | **CONFIRMED** | official upgrade doc + 3.1 announcement (two sources) |
| `surreal start` flags + `surrealkv://` unchanged | **CONFIRMED** | current 3.x CLI docs |
| Rollback = restore-from-backup; binary downgrade unsupported | **CONFIRMED (the safe path); downgrade flagged unsupported** | upgrade doc recommends backup-to-revert; no documented downgrade support |
| No `/sql` SQL-semantics break | **INFERRED** | patch-release nature + GraphQL-only announced break; full changelog not machine-retrievable → verified empirically by e2e (§5) |
| No SDK change needed for prod | **CONFIRMED** | prod is server-mode httpx `/sql`, never the SDK (inventory E1–E3) |

## Sources

- HTTP `/sql` headers + Basic auth (3.x): <https://surrealdb.com/docs/surrealdb/integration/http>
- `surreal start` flags + surrealkv (3.x): <https://surrealdb.com/docs/surrealdb/cli/start>
- Upgrade / in-place / backup-before-upgrade: <https://surrealdb.com/docs/surrealdb/installation/upgrading>
- 3.1 in-place roll-forward + GraphQL-only break: <https://surrealdb.com/3.1>
- 2.x header rename (already absorbed): <https://surrealdb.com/docs/surrealdb/installation/upgrading/migrating-data-to-2x>
- 3.1.5 release (2026-06-19, security patch line): <https://github.com/surrealdb/surrealdb/releases>
