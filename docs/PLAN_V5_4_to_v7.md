# Yadgar v5.3.9 → v7 Trajectory (advisor-audited 2026-05-20)

> **STATUS: HISTORICAL ROADMAP — current state is v5.25.0 (2026-05-31). Many items planned here shipped across v5.4–v5.25. Kept as architectural artifact.**

Locked plan after soak day + advisor review. Supersedes prior fragmented
roadmap entries. v5.3.9 shipped 2026-05-20 (8 commits on `feat/v5.3.9`).
Mirror lives in wiki `yadgar-roadmap-future-improvements`.

Source decisions:
- Soak observations 2026-05-20 (perf regression, viz UX, design Qs).
- Architectural invariants I1–I15 codified in `docs/ARCHITECTURE_INVARIANTS.md`.
- Advisor audit 2026-05-20 — pushed back on v5.4 scope creep, invariant inflation, missing exit criteria.

---

## Invariant scope (LOCKED)

- **Keep:** I1–I13 (existing) + I14 (logging, scoped) + I15 (fuzz, scoped).
- **Defer:** I16 (migration reversibility), I17 (hook timeout), I18 (idempotent retry). Codify only when violations surface.
- **Reject as invariant:** I19 (forward-context awareness) — becomes PR-template checklist instead. Soft enough that an invariant adds no enforcement; checklist gets the same outcome.
- **I14 scope:** structured JSON logs + content redaction + ratchet only. `trace_id` propagation is a separate v5.5 P-item, NOT folded into I14.

---

## v5.3.9 — Crash hotfix + carried cleanup (SUPERSEDES v5.3.8)

Renamed v5.3.8 → v5.3.9. Adds urgent crash-prevention from soak observation #3 + DLQ cleanup.

### Crash-prevention items (urgent, do first)

1. **systemd cascade decouple** — change `BindsTo=yadgar-backend.service` → `Wants=` on `yadgar.service`. Remove `Requires=yadgar-backend.service` (or weaken to `Wants=`). Keep `After=` for ordering. Backend death must NOT force core stop. Verified root cause via `systemctl --user cat yadgar.service` 2026-05-20 evening.
   - **Location:** nix repo `~/git/nix/modules/home/yadgar.nix` (NOT this repo). Per HARD RULE: edit nix file, hand `home-manager switch` command via MIGRATION_NOTES.md.
2. **N1. Short HTTP timeouts (5s) on backend calls.** ML client + drainer + dbsize + storage. Env-configurable. Prevents thread starvation during backend failure.
3. **N2. ASGI graceful shutdown ≤5s budget.** Drop stuck in-flight on backend-gone signal. Don't hold 30s of SIGTERM.

### Carried items from prior v5.3.8 checkpoint

4. CHANGELOG.md backfill v5.1.5 → v5.3.7 (12+ entries missing).
5. Stale wiki refresh — `mod-hooks-session-start-context`, `fn-server-py-hook-session-context`.
6. Hook empirical validation pass — SubagentStop wallpaper fix.
7. Q5b dbsize test stability — xdist 10x repeat.
8. SRI hashes on 2 CDN scripts in `index.html`.
9. Dogfood M2 — dispatch v5.3.9 agent via `agent_dispatch_prelude("cleanup", ...)`. Save prompt as `agent-prompt-cleanup-v1`.
10. **NEW:** commit `docs/ARCHITECTURE_INVARIANTS.md` + `docs/PLAN_V5_4_to_v7.md`.

### DLQ cleanup

11. **DLQ flush 16 stale wiki_add entries.** Pre-v5.0 payloads stuck since 2026-05-18 with `schema_version_too_old: got None, require >= 2`. Decide: `dlq_requeue` with schema_version=2 patch, OR explicit drop.

### Exit criteria

- Backend kill simulation: `systemctl --user stop yadgar-backend` → yadgar core stays alive (no BindsTo cascade). After backend restart, core resumes normal operation.
- All backend HTTP calls use ≤5s timeout (N1 audit via grep on httpx call sites).
- ASGI graceful shutdown completes in ≤5s under simulated backend-gone (N2 verification, simulated dbsize timeout test).
- 7 carried items merged (4–10 above).
- DLQ empty (16 stale entries handled).
- Pre-existing tests green.
- CHANGELOG complete.
- SubagentStop capture rate > 0 from a 5-dispatch smoke test.

## v5.3.10 — CPU busy-loop + viz disconnect hotfix (SHIPPED)

- v5.3.10 hotfix shipped: N4 circuit breaker + viz disconnected-cluster nav. N4 forward-ported from v5.4 due to ops urgency (CPU busy-loop post-v5.3.9 deploy).
- N4 circuit breaker establishes Pattern CB-1 in `docs/ARCHITECTURE_INVARIANTS.md`. See CB-1 for the architectural rationale and banned regressions.

## v5.3.8 — SUPERSEDED by v5.3.9

(See v5.3.9 above. v5.3.8 was the pre-crash carried-cleanup bundle. Crash on 2026-05-20 evening forced renaming + adding 3 crash-prevention items + DLQ cleanup.)

---

## v5.3.8 (historical, not shipped) — Carried cleanup

Single PR. Items pre-vetted from 2026-05-19 evening checkpoint.

1. CHANGELOG.md backfill v5.1.5 → v5.3.7 (12+ entries missing).
2. Stale wiki refresh — `mod-hooks-session-start-context`, `fn-server-py-hook-session-context` (pre-v5.1.0 paths).
3. Hook empirical validation pass — SubagentStop captured ZERO from ~10 dispatches = wallpaper. Fix parser leniency OR mandate `## Yadgar findings` block in dispatch prompts.
4. Q5b dbsize test stability — passed without explicit fix = flaky. Run xdist 10x.
5. SRI hashes on 2 CDN scripts in `index.html` (v5.3.7 review-agent finding).
6. Dogfood M2 — dispatch v5.3.8 agent via `agent_dispatch_prelude("cleanup", ...)`. Save prompt as `agent-prompt-cleanup-v1`.
7. **NEW:** commit `docs/ARCHITECTURE_INVARIANTS.md` (currently untracked from soak).

**DO NOT fold v5.4 I14/I15 doc updates into v5.3.8** — keep this bundle clean per advisor.

**Exit criteria:**
- 7 items merged
- Pre-existing tests green
- CHANGELOG complete (manual diff of git log vs CHANGELOG)
- SubagentStop capture rate > 0 from a 5-dispatch smoke test

---

## v5.4 — Observability + cheap wins (NO STRUCTURAL CHANGES)

6 PRs. **None touch memorize behavior.** Per advisor split.

### Order

1. **P11. Observability v1** (FIRST PR).
   - ~20 metric instrumentation sites (write, read, embed, KG, curator, engram, LLM, MCP, DB, process, subagent, viz).
   - Decorator helper `yadgar/observability/timing.py`.
   - Grafana dashboard JSON + alert rules YAML in `docs/observability/`.
   - Test: integration assert each metric emits at least once on a representative path.

2. **P12. Complexity audit (catalog only, no decompose).**
   - Static analysis: ruff `C901` + custom AST script for LOC / nesting / file-size.
   - Output: `docs/complexity-audit.md` table with columns: `file:line`, current cyclomatic / LOC / params / nesting, hard-or-soft violation, decomposition risk per I5 (HIGH = crosses thread/async boundary or shares mutable state; MEDIUM = parameter-passing rewrite; LOW = mechanical split), proposed action.
   - Test: AST-script unit tests so cap-violation classification is reproducible.
   - **Feedback loop (per advisor):** if audit shows >20% functions violating soft caps → review I13 caps per I10 (override or relax) BEFORE shipping enforcement.

3. **F0. Backend image bloat 6.78GB → ≤1.6GB.** **Explicit P0** (no longer buried in P9).
   - Audit `Dockerfile.backend` + `.dockerignore`.
   - Find ML cache leak.
   - Verify per I11 — large layers only justified by data, not code.

4. **P9. Image partitioning audit for I11.**
   - Every layer > 100MB justified or moved.
   - Add `docker history` check to release-readiness CI.
   - Confirm no model weights / large data in core image.

5. **P4. C4 conflict resolver gate hoist.**
   - Check `YADGAR_CONFLICT_RESOLVER` at module import time.
   - If off: class is no-op stub. No `httpx.Client` built.

6. **P7. Reinjection becomes opt-in.**
   - `YADGAR_REINJECT_ON_WRITE` (default OFF).
   - Drop reinjection from hot write path.

7. **I14. Structured JSON logging** — first slice.
   - JSON format with `ts, level, component, action, outcome, latency_ms?, error?`.
   - Content redaction: never log memory `content`, tokens, passwords, or user-supplied strings in metric labels.
   - **Ratchet:** new code conforms; old code conforms when touched; full conformance is v5.6.
   - **NO `trace_id` propagation in v5.4** — that's a v5.5 separate P-item.

### Crash hardening (revised post-verification 2026-05-20 evening)

Verifications done; scope corrected:

- **N1 + N2 + systemd-cascade-fix MOVED to v5.3.9** (urgent — user just hit failure). See v5.3.9 above.
- **`Restart=on-failure` for yadgar.service — DROPPED.** It's already set with RestartSec=10. Verified `cat ~/.config/systemd/user/yadgar.service`. Not the bug.
- **F5 scope corrected.** Docker stats showed backend at 768MB idle / 4.3GB limit. OOM was a LOAD-INDUCED SPIKE during `/rerank` activity, not a steady-state leak. F0 lean image WILL NOT FIX THE OOM — different problem.

What remains in v5.4 for backend resilience:

8. **N3. Backend liveness gauges** (folds into P11): `yadgar_backend_reachable{endpoint=ce/nli/pair/dbsize/storage}` (gauge), `yadgar_backend_memory_pressure` if backend exposes it.
9. ~~N4. Circuit breaker~~ — shipped in v5.3.10 (see CB-1 in Patterns Library).
10. **F5. Backend OOM root-cause investigation report.** Identify spike trigger via instrumented `/rerank` load test. Outcome: a 1-page report drives the fix design. Likely candidates: (a) lazy-load rerankers (CE/NLI/pair load on first call, evict after idle window), (b) cap concurrent inference batch size, (c) bump cgroup to 6G as workaround (per I12, only if (a)+(b) prove insufficient). Fix lands in v5.5 (F3 blue-green addresses the cascade angle separately).

### Workflow integration (side-bundles in v5.4)

- Add invariants checklist to PR template (`.github/pull_request_template.md` or `.codeberg/...`).
- Update `agent_dispatch_prelude` (M2) to inject `docs/ARCHITECTURE_INVARIANTS.md` into every planning subagent.

### Exit criteria

- Drainer cycle metric emits + dashboard shows current baseline (recorded as `v5.4-baseline.json` committed to `docs/observability/`).
- Backend image ≤ 1.6GB confirmed via `docker history` in CI.
- Backend survives 5-minute ML inference stress without OOMKill (F5 verification).
- Core auto-restarts on backend-induced failure (Restart=on-failure verified via simulated backend stop).
- All backend HTTP calls use ≤5s timeout (N1 audit).
- ASGI graceful shutdown completes in ≤5s under simulated backend-gone scenario (N2 verification).
- Backend liveness gauge flips to 0 within 5s of backend stop, returns to 1 within 5s of recovery (N3 + N4 verification).
- `YADGAR_REINJECT_ON_WRITE` default OFF; old behavior gated.
- `docs/complexity-audit.md` committed.
- New log lines (post-v5.4 code) are valid JSON, pass redaction lint.
- PR template merged + agent_dispatch_prelude injects invariants.
- If P12 audit shows > 20% functions violating soft caps → I13 review per I10 BEFORE any decompose PR.

---

## v5.5 — Structural + soak items

Informed by v5.4's data. Memorize split lives HERE, not v5.4.

### Shipped in v5.5

- ~~**V1a. Backend /metrics endpoint.**~~ **SHIPPED v5.5.0.** `yadgar/embed_service_metrics.py` + GET `/metrics` on embed_service app. Unauthenticated. F5-A semaphore observability + model gauges + process metrics. Backend 5.0.3 → 5.1.0; core 5.4.5 → 5.5.0.

### Order

1. **P3. asyncio.to_thread wrap.**
   - Survey: `grep -n "\.encode(" yadgar/ -r` and adjacent ML calls.
   - Every async-context call → `await asyncio.to_thread(model.encode, ...)`.
   - Data-driven via v5.4 P11 numbers: only wrap call sites that show up in `recall_stage_ms{stage}` or `drain_stage_ms{stage}` tail.

2. **F1. Async embed model load.** `/health` returns 200 fast; model warms in background.

3. **F2. Pre-pull model in build.** No runtime download. Implements I11 spirit.

4. **P1 + P2 + P8. Memorize split bundle.**
   - P1: split memorize tool into `_memorize_enqueue` (~50 LOC, request) + `_memorize_apply_lean` (~150 LOC, drainer) + `_memorize_apply_consolidation` (deferred).
   - P2: ConsolidationScheduler fast-tier sub-cycle (5–15s) picks memories with `last_consolidated IS NONE`.
   - P8: idempotency markers — `consolidation_state` field (NULL / drainer-done / consolidation-done).
   - **Test:** characterization test pins current memorize behavior pre-split; split must produce provably-equivalent output.
   - Informed by v5.4 P12 audit (knows complexity-hot spots in advance).

5. **I15. Fuzz infra + initial tests** (Hypothesis property-based).
   - Scope: parsers (SurrealQL builder, queue payload deserialization, hook payload parse), validators (memorize/recall/wiki_query inputs), migrations (#004–#007), queue+DLQ replay (random ordering / partial corruption).
   - Target ≥ 10 fuzz tests passing in CI.
   - Failures block merge.

6. **Viz V5.5-S2.1–S2.4** (4 viz UX fixes from soak).
   - S2.1: 3D node heat color propagation (THREE.js material color attr).
   - S2.2: wiki vs memory shape distinction (distinct geometries — torus/cube/octahedron for wiki, sphere for memory).
   - S2.3: semantic search wiring (verify viz proxy bearer + front-end fetch handler).
   - S2.4: CPU + dbsize stats animation (SSE/poll loop or backend endpoint).

7. **Trace-id propagation** as its own P-item (deferred from I14).
   - MCP → core → drainer → backend HTTP context plumbing.
   - Possibly OpenTelemetry-flavored. Effort estimate before commit.

8. **F3. Blue-green backend swap** (RE-PRIORITIZED from optional/deferred → P1 in v5.5).
   - Triggered by soak observation #3 (2026-05-20): backend OOM cascades to core kill.
   - New backend container comes up healthy BEFORE old one removed; core never observes DNS drop.
   - Requires systemd refactor: two backend units (yadgar-backend-blue + yadgar-backend-green), traffic-switch via container alias or proxy.
   - Justifies the cost now that real evidence shows the cascading-failure mode.

8. **v5.5-features parallel set** (only if soak clean post-v5.5 core ships):
   - `StopFailure` hook → `_incident` memory on API errors.
   - `PermissionDenied` hook → auto-mode telemetry.
   - `UserPromptExpansion` hook → slash-cmd usage tracking.
   - Orchestrator default: use `agent_dispatch_prelude` (v5.3.6 M2) for ALL subagent dispatches.

### Exit criteria

- Drainer cycle p99 ≤ 50ms (measured via v5.4 P11).
- Memorize p50 ≤ 5ms (I9 budget).
- Viz 4 items pass smoke (heat color renders in 3D, shapes distinguishable, search returns results, stats animate).
- ≥ 10 fuzz tests passing in CI.
- No I1–I15 regressions per pre-commit + CI.
- trace_id present on every log line in `yadgar/server/`.
- F3 blue-green proven: simulated backend kill → core stays alive + serves reads from cache + new backend takes over within 30s.

---

## v5.6 — Steady-state cleanup (P12-driven)

Outline only. Content from v5.4 P12 audit output.

- Decomposition bundles: ~5 LOW-risk functions per PR with characterization-test parity.
- Fuzz tests expansion: cover every validator + parser + migration added in v5.4 / v5.5.
- I14 logging full conformance — ratchet closes; CI fails on non-JSON log lines.
- PR template enforcement audit — verify every PR since v5.4 used the checklist.

### Exit criteria

- ≥ 80% functions within I13 soft caps.
- ≥ 50 fuzz tests total covering all validators + parsers + migrations.
- I14 ratchet 100% conformed in `yadgar/server/`.

---

## v6 — LLM curator (GATED on post-v5.5 soak)

Skeleton in `docs/PLAN_V6.md`. Two-tier consolidation (existing Tier 1 + nightly Tier 2 LLM pass). Six task types. `qwen3:8b` FAST + `deepseek-r1:8b` REASONING. Safety: scope limit + circuit breaker + soft-delete + llm_synthesized exclusion.

Two open design forks (per `yadgar-v5-stabilize-strategy-tldr-gap-analysis`):
- Write-time conflict resolution (Mem0, v5.3.4 C4 opt-in shipped) vs nightly batch — both layers or one?
- Depth saturation chunking strategy (SleepGate paper: ~15 collapse) — chunking design MUST land BEFORE first nightly run.

Both forks must close before v6 dispatches.

### Exit criteria

TBD post-v5.5 soak. Skeleton stays in `docs/PLAN_V6.md` until then.

---

## v7 — Real-time synthesis (HINT, post-v6)

`recall(synthesize=True)`, `wiki_query(synthesize=True)`, `ask()` tool. Target < 10s e2e.

Prerequisite: faster quantized model bench (current `deepseek-r1:8b` benchmarked at 69s — too slow for concurrent use).

Defer until v6 ships + bench proves feasible.

---

## Test plan additions (per advisor)

Cross-cutting:
- **P11** needs integration tests asserting each metric emits on a representative call path.
- **P12** needs AST-script unit tests for cap-violation classification reproducibility.
- **P1** needs a characterization test pinning current memorize behavior pre-split (math.isclose rel_tol=1e-9 parity for any numeric outputs; deepdiff for structured outputs).
- **I15** is itself the cross-cutting test invariant; new fuzz tests per new validator/parser/migration land WITH the feature, not after.

---

## Workflow integration (concrete, in v5.4)

These ship as side-bundles, not new versions:

1. **PR template** — add invariants checklist section linking each I1–I15. PR author confirms or justifies per-invariant.
2. **`agent_dispatch_prelude`** — inject `docs/ARCHITECTURE_INVARIANTS.md` into every planning subagent's prompt. Already partial via anchored memory; make it explicit.
3. **Pre-commit hooks** — enforce I13 (complexity), I14 (log format check), I15 (fuzz tests exist for new validators).
4. **CI** — enforce I8 (metrics emit asserts), I13 (hard caps block merge), I14 (JSON log validity), I16-future (migration round-trip when added).

No separate "workflow doc" — `docs/ARCHITECTURE_INVARIANTS.md` IS the workflow.

---

## Advisor caveats logged

- v5.4 originally bloated to 12 workstreams → split per advisor into v5.4 (observability + cheap wins) and v5.5 (structural).
- I13 cap numbers may need revision post-P12 audit (advisor flagged feedback loop, codified in v5.4 exit criteria).
- I14 `trace_id` propagation NOT a one-line invariant — moved to v5.5 own P-item.
- F0 priority was buried in P9 — now explicit P0 in v5.4 (item 3 of order).
- Advisor stress-test result: plan went from "v5.4 = infra ONLY" to a defensible, advisor-vetted, two-version split.

## Sync TODOs (when yadgar MCP back online)

1. Mirror this file into wiki page `yadgar-roadmap-future-improvements` (currently lags this doc).
2. Anchored project-scoped memory with `_v5.4-must` tag pointing to this file.
3. Update checkpoint with current_task = "v5.3.8 → v7 trajectory locked, ready to dispatch v5.3.8 first after soak closes".

Source: soak 2026-05-20 + advisor audit 2026-05-20.
