# Drift-Axis Sweep — systematic multi-axis drift ratchet

**Date:** 2026-06-30
**Status:** scoping (plan-only; no code in this doc)
**Slug:** `drift-axis-sweep`
**Author:** drift-audit dispatch (Opus orchestrator + 3 Explore agents + advisor)

---

## Problem

We invested heavily in doc/feature correctness and **still keep discovering gaps
and dead code by accident.** Diagnosis: **drift is multi-axis.** We ratcheted ONE
axis hard — config knobs (`Settings ↔ FIELD_META ↔ config_registry ↔
configuration.md`, via **I25** + a phantom-doc guard) — but the recent discoveries
live on OTHER, un-ratcheted axes:

- **Dead code / dead config** — `idle_threshold` knob deleted v5.76.0, yet an
  orphan `config.yaml` line survived and `docs/reference/architecture.md:107` still describes
  it. The silent-breakage audit (2026-06-16) hand-found **10 dead functions** and
  **15 dead config fields** that no automated check catches.
- **Producer↔consumer data flow** — the consolidation orchestrator computes
  `memify_pruned`, `cls_promoted`, … (14 keys) → `insert_consolidation_log`
  **DROPS them** before the DB write → the viz shows 0. "Stored ≢ shown."

Each axis surfaces only when someone trips on it. **Goal: stop tripping. Sweep +
ratchet deliberately.**

### Organizing spine (not N disconnected new checks)

**I29 already owns most of this territory** — *"no dead capability: stored ≡ used
≡ shown."* But its enforcement (`scripts/check_dead_capability.py`) covers exactly
**ONE** of the K axes I29 implies: graph **edge types**. The consolidation 14-key
drop **is** an I29 coherence violation (shown=0 ≠ used). The 10 dead functions
**are** I29 (built ≢ used). So the sweep is not "mint ten new invariants" — it is:

> **I25 owns the config axis. I29 owns the capability/stats/dead-code axes — but is
> enforced on only 1 of its implied surfaces. Extend I29's enforcement surface,
> axis by axis.**

The axis table below keeps the full enumeration the problem demands; the "check to
add" column is organized around **extending I25/I29** rather than proliferating
invariants.

### What this doc is NOT

The *fix* for the consolidation-stat drop + the `idle_threshold` dead-knob already
lives in `docs/plans/consolidation-stat-recording-and-idle-cleanup-2026-06-30.md`
(commit #77). **This doc is the sweep+ratchet that would have CAUGHT those by
design** — the antidote to "discover by accident," not a re-scope of the fixes.

---

## Part 1 — The drift axes (source-of-truth ↔ derived)

Every row is a SOURCE-OF-TRUTH ↔ DERIVED pair that can silently diverge. Blast
radius classes: **SILENT-WRONG** (runtime does the wrong thing), **DEAD** (code/
config/doc that no longer matters but lingers), **MISLEAD-UI** (viz/operator sees a
wrong or zero value), **MISLEAD-DOC** (prose lies to a reader).

| # | Axis | Source of truth | Derived artifact(s) | How it diverges | Blast radius |
|---|------|-----------------|---------------------|-----------------|--------------|
| A1 | Config knob 3-way | `config.py` Settings | `config_yaml.py` FIELD_META + `config_registry.py` ConfigEntry | New Settings field not registered in yaml+registry | DEAD (invisible knob) |
| A2 | Config knob ↔ prose | `config.py` Settings | `docs/reference/configuration.md` | Knob renamed/removed, doc not updated | MISLEAD-DOC |
| A3 | Config knob ↔ example yaml | `config.py` Settings | shipped/user `config.yaml` example keys | Knob deleted (`idle_threshold` v5.76.0), orphan yaml key survives | DEAD (confusing config) |
| A4 | Dead **functions** | code call graph | the function defs themselves | Caller removed (e.g. `_maybe_sleep_cycle` never wired since v5.7.0) → def is orphaned | DEAD + SILENT-WRONG (the capability silently never runs) |
| A5 | Dead **config fields** | call sites reading a Settings field | the Settings field | Field read-site removed, field lingers (15 found: `FRACTAL_LEVELS`, `HOPFIELD_BETA`, …) | DEAD |
| A6 | **Producer→persist** stats | `consolidation/orchestrator.py` `stats` dict | `storage/ops.py::insert_consolidation_log` columns | 14 computed keys dropped at the INSERT (only 6 of 20 persisted) | MISLEAD-UI (viz shows 0) |
| A7 | **Persist→API→UI** fields | DB column / computed value | `server/http.py` `/api/metrics/*` → `static/index.html` chart fields | UI reads a key nothing produces, or producer key never reaches a panel | MISLEAD-UI |
| A8 | Declared metric ↔ writer | `metrics.py` declarations | `.set()/.inc()/.observe()` call sites | Metric (or **label**) declared, no writer (I23). Stale `consolidation_daemon` heartbeat label slips through — metric var has *other* writers | MISLEAD-UI (dead gauge) |
| A9 | Capability registry coverage | `config.py` + `server/tools/*` + `migrations.py` + `BEHAVIOR_CONTRACT.md` | `docs/CAPABILITY_REGISTRY.md` (I32) | New tool/setting/migration/BC not catalogued → I32 RED. (Coverage only — see A10.) | DEAD (uncatalogued) |
| A10 | Capability registry **accuracy** | actual runtime reachability | `CAPABILITY_REGISTRY.md` `status:` field | A `status: active` row whose capability is actually dead — I32 is coverage-not-correctness | MISLEAD-DOC |
| A11 | MCP tool list ↔ README | `server/tools/*` @_tool count (75 non-test; `_test_*` excluded) | `README.md:238` ("75 MCP tools") | Tools added, count not bumped — **currently IN SYNC (75 = 75), no drift today**; risk is future-only | MISLEAD-DOC (latent) |
| A12 | Tool ↔ description/schema | tool impl signature | tool docstring / `server.json` description | Param added/removed, description stale | MISLEAD-DOC |
| A13 | Prose docs ↔ runtime | runtime behavior | `docs/reference/architecture.md`, `roadmap/*`, `README.md` | Behavior changed (idle→nightly v5.7.0), `architecture.md:107` still describes old | MISLEAD-DOC |
| A14 | Behavior-contract ↔ e2e | `BEHAVIOR_CONTRACT.md` BC-* rows + ✅ floor | `tests/e2e/*` test refs | Contract row with no test, or assertions weakened | SILENT-WRONG (untested contract) |
| A15 | Edge type ↔ contract | `graph_api.py` + `viz_meta.py::EDGE_TYPES` | `docs/EDGE_CONTRACT.md` | Edge produced w/o contract row, or `drop`-role still produced (I29) | DEAD |
| A16 | Wiki fn pages ↔ code | source file SHA256 | auto-generated wiki page hash (`server/tools/project.py`) | Code changes, wiki page hash stale (#47) | MISLEAD-DOC |
| A17 | env var ↔ nix ↔ yaml ↔ Settings | `config.py` env aliases | nix/deploy unit env + `config.yaml` | Env-only knob (allowlist Tier-2 backlog ~100) drifts from deploy wiring | DEAD / SILENT-WRONG |
| A18 | DB schema ↔ migrations | `storage` schema / models | `storage/migrations.py` numbered migrations | Column added without migration, or migration for dropped column | SILENT-WRONG |
| A19 | Version sync | `pyproject.toml` version | `server.json`, `flake.nix`, `docker-compose.yml`, `uv.lock` | One bumped, others not | SILENT-WRONG (deploy mismatch) |
| A20 | Backend version sync | `server.json::backend_version` | `Dockerfile.backend`, `entrypoint-backend.sh`, `backend/*` | Backend build inputs change without version bump | SILENT-WRONG |
| A21 | Plan-first discovery | tagged `discovery` memories (24h) | a `PLAN_V5_*.md` mention (I27) | Discovery noticed, never tracked into a plan (`check_open_discoveries.py` **never written**) | DEAD (meta-drift: an invariant with zero enforcement) |

**21 axes** identified (the task named ~13 candidates; this extends them with A2,
A3, A8-label, A10, A12, A14, A21 surfaced by the survey).

---

## Part 2 — Classify + coverage

For each axis: **DIFFABLE→RATCHETABLE** (an invariant test can fail mechanically on
drift) vs **AUDIT-ONLY** (free-text / semantic → needs a periodic human/LLM
drift-audit, can't ratchet cleanly); current coverage **RATCHETED / PARTIAL /
NONE**; and the **specific check to add**.

| # | Axis | Class | Coverage | Existing enforcer | The specific check to add |
|---|------|-------|----------|-------------------|---------------------------|
| A1 | config 3-way | RATCHETABLE | **RATCHETED** | `test_config_three_way_sync.py` (I25) + CI `invariant-checks` | — (done) |
| A2 | config↔prose | RATCHETABLE (token-diff) | PARTIAL | phantom-doc guard (config names in `configuration.md`) | Extend phantom-guard: every `configuration.md` knob name must exist in Settings, and vice-versa for "documented knobs" |
| A3 | config↔example yaml | RATCHETABLE | **NONE** | — | Lint: load every shipped/example `config.yaml` against the pydantic Settings model with `extra="forbid"`; orphan key (e.g. `idle_threshold`) → RED |
| A4 | dead functions | RATCHETABLE-but-NOISY | **NONE** | — | `check_dead_functions.py`: AST call-graph (vulture-style) over `yadgar/`; **needs an allowlist baseline like I30** (dynamic dispatch / plugin / test-only = false positives). Day-one seed = the 10 known + provisional rationale |
| A5 | dead config fields | RATCHETABLE | **NONE** | — | Extend the dead-function lint OR a focused check: every Settings field has ≥1 read site OR an explicit `dead-pending-removal:vX.Y.Z` allowlist entry |
| A6 | producer→persist stats | RATCHETABLE | **NONE** | — | **`check_stats_parity.py`** (headline win): every key written into the `stats` dict in `consolidation/orchestrator.py` must map to a persisted `consolidation_log` column OR be in an explicit `ephemeral-stat-allowlist`. Turns the 14-key class RED |
| A7 | persist→API→UI | RATCHETABLE (harder) | **NONE** | — | Round-trip test `test_consolidation_stats_roundtrip`: produce→persist→API→assert UI-consumed key set ⊇ produced non-ephemeral set. Generalize to other `/api/metrics/*` panels |
| A8 | metric/label↔writer | RATCHETABLE | **PARTIAL** | `check_metric_writers.py` (I23) | I23 extension: also require each declared **label value** referenced in a panel to have a writer — catches the stale `consolidation_daemon` heartbeat label |
| A9 | registry coverage | RATCHETABLE | **RATCHETED** | `check_capability_coverage.py` (I32) | — (done) |
| A10 | registry accuracy | **AUDIT-ONLY** | NONE | — (I32 is coverage-only by design) | Periodic LLM drift-audit: sample N rows, verify `status:` against reachability. NOT mechanically ratchetable |
| A11 | tool count↔README | RATCHETABLE | **NONE** (but in-sync today) | — | Lint: assert `README.md` tool count == live `@_tool` count (minus `_test_*`). Currently 75 = 75 — **no drift to fix**; a cheap guard against future drift, not a present bug |
| A12 | tool↔description | **AUDIT-ONLY** (semantic) | NONE | — | Periodic drift-audit; param-presence portion is diffable (signature param ∈ description) but quality is not |
| A13 | prose↔runtime | **AUDIT-ONLY** | NONE | — | Periodic `drift-audit` agent over `architecture.md` / roadmap; the *known-removed-token* portion (grep dead knob/fn names in prose) is a cheap diffable sub-check |
| A14 | BC↔e2e | RATCHETABLE | **RATCHETED** | `check_contract_coverage.py` + `check_e2e_assertions.py` + `check_test_weakening.py` | — (done; tamper-protection #52) |
| A15 | edge↔contract | RATCHETABLE | **RATCHETED** | `check_dead_capability.py` (I29) + CI | — (done — the 1 axis I29 currently covers) |
| A16 | wiki↔code hash | RATCHETABLE | **PARTIAL** | SHA256 staleness in `server/tools/project.py`; `wiki_lint` tool | Wire `wiki_lint` staleness into the periodic drift-audit (it's a tool, not a gate) |
| A17 | env↔nix↔yaml | PARTIAL-RATCHETABLE | **PARTIAL** | `config_env_only_allowlist.txt` (I25 Tier-1/2) | Drain Tier-2 backlog (~100); cross-check env aliases against deploy unit is AUDIT-ONLY |
| A18 | schema↔migrations | RATCHETABLE | **RATCHETED-ish** | 25 sequenced migrations; survey found no orphans | Add an assertion test that every schema column traces to a migration (defensive; currently clean) |
| A19 | version sync | RATCHETABLE | **RATCHETED** | `check_versions.py` | — (done) |
| A20 | backend version | RATCHETABLE | **RATCHETED** | `check_backend_bump.py` + `ci-release.yaml` | — (done) |
| A21 | plan-first discovery | RATCHETABLE (warn) | **NONE** | — (`check_open_discoveries.py` proposed v5.29, never written) | Write the proposed stop-hook lint, or formally retire I27's enforcement clause |

### Coverage summary

- **RATCHETED (7):** A1, A9, A14, A15, A18, A19, A20.
- **PARTIAL (4):** A2, A8, A16, A17.
- **NONE (10):** A3, A4, A5, A6, A7, A10, A11, A12, A13, A21.

### Ratchetable-but-unratcheted — the quick wins

Of the 10 NONE-coverage axes, the **clean ratchet candidates** (mechanically
diffable, not free-text, low scaffolding) are **A3, A6, A21**, plus partials **A8**
and **A2**. That's **3 clean NONE-coverage quick wins** (A3, A6, A21) + 2 partial
extensions (A8, A2).

Caveats that keep this list honest:
- **A11 is in-sync today** (75 = 75 verified) — a future-proofing guard, not a
  present bug, so it is NOT counted as a quick win to *fix* (only to *guard*).
- **A7** is **RATCHETABLE-but-harder** (full producer→API→UI round-trip wiring),
  not a clean one-liner — it generalizes A6 and lands after A6 proves the pattern.
- A4/A5 are ratchetable but need an I30-style allowlist for dynamic-dispatch noise;
  A10/A12/A13 are genuinely audit-only.

### Genuinely un-ratchetable (need human/LLM audit, never a clean gate)

- **A10** registry `status:` accuracy — semantic reachability.
- **A12** tool description quality — semantic.
- **A13** prose-vs-runtime narrative — free text (only the known-dead-token grep
  sub-check is diffable).
- **A17** env↔deploy-unit cross-check — spans repos/infra.

These belong to the **periodic drift-audit** (Part 3), not CI.

---

## Part 3 — The sweep + the recurring mechanism

### Prioritized sweep order

Ranked by **drift-risk × user-facing-impact × cheapness**:

1. **A6 — `check_stats_parity.py`** *(headline)*. Mechanical + directly
   user-facing (viz shows 0). One AST walk of the `stats` dict vs the
   `insert_consolidation_log` column set + an ephemeral allowlist. Turns the 14-key
   class RED. Cheapest high-impact win.
2. **A3 — example-yaml-vs-Settings lint**. Trivial: `Settings(**yaml, extra=forbid)`
   over each example/shipped yaml. Would have caught `idle_threshold` orphan
   instantly. Tiny code.
3. **A8 — I23 label-granularity extension**. Extend the existing metric-writer
   lint to label values; catches the stale `consolidation_daemon` heartbeat label.
4. **A21 — `check_open_discoveries.py`** OR decide to retire I27's enforcement
   clause. An invariant with zero enforcement is itself meta-drift; resolve it.
5. **A4/A5 — dead-function + dead-config lint** with an I30-style allowlist seeded
   from the 10 + 15 known. Highest *count* of "discover by accident," but needs the
   allowlist scaffolding (noisy without it), so it lands after the clean wins.
6. **A7 — full producer→API→UI round-trip test**. Generalizes A6 to other
   `/api/metrics/*` panels; more wiring, do after A6 proves the pattern.
7. **A11 — README tool-count guard** (lowest priority). In-sync today (75 = 75);
   a future-drift guard, not a present fix. Cheap to add whenever convenient.

### Top-3 to do first

**A6 (stats-parity), A3 (example-yaml lint), A8 (I23 label extension)** — all
mechanical and cheap. A6 + A3 map directly to the two exact bugs we already tripped
on (the 14-key drop and the `idle_threshold` orphan); A8 closes the stale-label
gap I23 misses. (A11 is NOT in the top-3: verified 75 = 75 today, so it fixes no
present bug — it's a guard, deferred.)

### The recurring mechanism (proactive, not by-accident)

Two complementary layers — **mechanical gate** for diffable axes, **periodic
drift-audit** for semantic ones:

**(1) CI gate — extend the existing aggregation point.** CI already has the right
home:
- `.forgejo/workflows/ci-pr.yaml` → **`invariant-checks` job** already runs
  I23/I24/I25/I29/I32 as named steps. New ratchets (A3, A6, A8, A11) each get **one
  `pre-commit` hook + one line in this job**. No new orchestration needed.
- `.forgejo/workflows/validate.yaml` runs `pre-commit run --all-files`, so any new
  pre-commit hook is enforced in CI automatically. (Correction to an earlier survey
  claim: this repo's CI *does* run the drift checks — it is Forgejo Actions, not
  GitHub Actions.)
- **Recommendation:** add a `make invariant-checks` target that runs all
  `scripts/check_*.py` locally — a single developer-facing aggregate mirroring the
  CI job (currently no such target exists; checks are individually wired).

**(2) Periodic `drift-audit` agent — the AUDIT-ONLY complement.** For A10, A12,
A13, A16, A17 (semantic / cross-repo / free-text), a mechanical gate is impossible.
Recommend a **scheduled (weekly) drift-audit agent run**, NOT a stop-hook nudge:
- Stop-hook is wrong here — these audits are slow, multi-file, LLM-judgement; firing
  them every session-stop is friction-budget violation (the same reason I27's lint
  is warn-only).
- A weekly cron/scheduled agent dispatch over a fixed checklist (architecture.md
  freshness, README counts, registry `status:` sampling, `wiki_lint` staleness) is
  the right cadence. Pairs with the saved **`drift-audit` agent-prompt** (the manual
  complement) — register/refresh it via `agent_prompt_save` so the dispatch is
  reproducible.
- The audit's output feeds I27: each finding becomes a tracked `PLAN_*` stub, not
  chat-ephemera.

**(3) Tie to existing invariants — extend, don't multiply.**
- **I29** is the spine. Add a sub-section to I29 in `ARCHITECTURE_INVARIANTS.md`:
  "I29 enforcement surfaces" listing edge-types (done) + stats-parity (A6) +
  dead-functions (A4/A5) as enumerated axes, each with its own check script. This
  makes "which axes does I29 cover" explicit and prevents the silent assumption that
  green-edge-lint == I29-satisfied.
- **I25** absorbs A3 (example-yaml) and A2 (prose) as additional config surfaces.
- **I23** absorbs A8 (label granularity).
- **I27** resolve A21: implement `check_open_discoveries.py` or retire the clause.

### Honest assessment

- **Pure mechanical wins (do now):** A3, A6 — and the I23/A8 extension. These are
  diffs; a test fails on drift; zero judgement. (A11 is also mechanical but fixes no
  present bug — 75 = 75 verified — so it's a future-drift guard, deferred, not a
  "do now.")
- **Mechanical-but-needs-scaffolding:** A4/A5 (dead code) — ratchetable only with an
  I30-style allowlist+rationale to absorb dynamic-dispatch false positives. Highest
  count of past accidental discoveries, so worth the scaffolding, but not a one-liner.
- **Genuinely un-ratchetable (LLM/human audit forever):** A10 (status accuracy),
  A12 (description quality), A13 (prose narrative), A17 (env↔deploy). These need the
  weekly drift-audit; pretending a gate can cover them would produce a flaky,
  ignored check.

---

## Advisor input (incorporated)

- **Reframe as extending I29's enforcement surface**, not minting N invariants —
  the consolidation drop and the dead functions are *already* I29 violations.
  → Done: I29 is the spine of Part 1/2/3.
- **Resolve the CI location before finalizing Part 3** — Codeberg/Forgejo host, so
  CI is Forgejo Actions, not GitHub Actions. → Confirmed: `ci-pr.yaml`
  `invariant-checks` job + `validate.yaml` `pre-commit --all-files`. CI *does* run
  the checks; new ratchets land in that job.
- **Don't re-scope the fix** (commit #77 owns it) — this is the catch-by-design
  sweep. → Stated explicitly up top.
- **Add A8 (I23 label gap), A21 (I27 unimplemented), A4/A5 (dead functions)** —
  none of the 14 checks detect dead functions; the audit found 10 by hand; classify
  honestly as ratchetable-but-noisy → allowlist. → All four in the table.
- **Headline win stated concretely:** stats-parity check leading the sweep. → A6 is
  #1.

---

## Appendix — evidence pointers

- Consolidation drop: producer `consolidation/orchestrator.py:283-326` (14 keys) →
  `storage/ops.py:51-67` (`insert_consolidation_log`, 6 columns) →
  `server/http.py:1792-1809` (`/api/metrics/consolidation-log`, 6 fields) →
  `static/index.html:3618-3648` (chart, 3 metrics).
- Dead-capability lint scope (edge-types only): `scripts/check_dead_capability.py`.
- Silent-breakage hand-audit (10 dead fns, 15 dead config, stale label):
  `docs/reports/audits/silent-breakage-2026-06-16.md`.
- Invariant text: `docs/ARCHITECTURE_INVARIANTS.md` (I23 §223, I25 §260, I27 §318,
  I29 §362, I30 §384, I32-ref in `CAPABILITY_REGISTRY.md`).
- CI aggregation: `.forgejo/workflows/ci-pr.yaml:86` (`invariant-checks` job),
  `.forgejo/workflows/validate.yaml:32` (`pre-commit run --all-files`).
- The fix (not this doc): `docs/plans/consolidation-stat-recording-and-idle-cleanup-2026-06-30.md`.
- `idle_threshold` stale prose: `docs/reference/architecture.md:107`. README tool count
  `README.md:238` ("75 MCP tools") — **verified in-sync**: 75 non-`_test_` @_tool
  `def`s (the 2 `_test_*` tools excluded), matching the README. A11 is a latent
  guard against *future* drift, not a present bug.
