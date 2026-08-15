<!-- VERBATIM BACKUP of yadgar wiki page `yadgar-task-list` (page id 6845,
     page_type=task_list, project_id=m-agahi/yadgar,
     directory_context=/home/max/git/yadgar, updated_at 2026-08-09T19:38:10Z).
     Captured 2026-08-15 before any ledger seed / page deletion. -->

<!-- yadgar task-list page — schema v1. One "## task:<id>" section per task.
     status ∈ {pending, in_progress, completed}. Restore: recreate open
     tasks via TaskCreate.

     POLICY (2026-08-02): closed tasks are DELETED outright, never archived here
     — this page is read in full at every session start. A section is a POINTER:
     subject, status, a context path, and AT MOST two lines carrying only what
     exists nowhere else (a file:line, a measured number, a gate). Detail lives
     in docs/plans/*.md, the ADRs and docs/CHANGELOG.md. If a description starts
     explaining WHY, it belongs in a plan doc instead. -->

# yadgar task list

## Meta
- project: yadgar
- open: 76 · completed: 2 (closed rows normally deleted by policy; 2026-08-08 batch (0153, 0149) kept as completed sections per explicit instruction)
- 2026-08-02: v5.172 train MERGED (PR #22, `fe84e3ec`). Deleted: 0006 0031 0085
  0119 0123.
- 2026-08-04: PR #32 (spine) CLOSED UNMERGED; PR #33 merged the replacement
  design — ADR-0198/0199/0200.
- 2026-08-05/06: SPLIT-STORE §8 FULLY ANSWERED. ADRs 0201-0214. VERIFIED:
  asyncmy Apache-2.0; MariaDB idle RSS 86.6 MB; SurrealDB **BSL 1.1** — live
  NOTICE breach, see 0132. Deleted: 0094.
- **2026-08-07: PR #35 (engine #2) MERGED** as `f0c280ae`, tag v5.172.0.
  Runtime images exist — CI builds them; only the CI images are hand-built.
  ENGINE #2 VERIFIED ON A REAL HOST: boots fresh AND on a populated volume,
  alembic `0001_config`, quiesced snapshot PAIR via the real nightly, PARTIAL
  restore rejected WITH a passing control. Facts:
  [[engine-2-mariadb-verified-operational-facts-on-a-real-host]].
  STILL UNCOVERED: deploy ordering (0147) and scale.
- **2026-08-07/08: BRANCH-SCOPING REMOVAL TRAIN MERGED** — PR #36 = `cdd7e0a4`.
  All 10 cars + anchor_renew + BC-G11 re-pointer landed. Cleanup train
  planned: see [[cleanup-train-2026-08-08]]. 0144 closed by this merge.
- Prod backup before any upgrade: `backups/surql/surreal_db.pre-engine2-upgrade-
  2026-08-07-100337.surql` (116 MB, verified complete through wiki v206).
- **The vacuous-pass family** — now SEVEN. Six in
  [[the-vacuous-pass-family-guards-in-this-repo-that-reported-ok-whi]], plus the
  skip-gate blind to DESELECTED tests (0143). Companion page for tools whose
  NAME overpromises: [[guards-and-tooling-in-this-repo-that-do-not-check-what-their-nam]]
  — FastMCP drops unknown kwargs rather than rejecting them, `npm run generate`
  is a stub, `verify-tool-coverage` compares names only, the complexity
  allowlist never checks recorded values.
- **Agent throughput (ADR-0218)**: cars must NEVER run the full unit suite —
  four burned 30-55 min each on runs that never completed. Pre-commit measured
  at 11s/36 hooks; gates STAY at pre-commit (CI already mirrors ten of them).
  Worktrees need `uv sync --extra test --extra dev` as step 0.
- `mypy>=1.11` is unpinned; a minor bump shifts all 1206 ratchet baseline counts.
- Host guards LIVE: machine.slice cpu.max 8-core, yadgar-reap-stale-tests.timer.
- 2026-08-09: Car L of 0047 spine train landed (d05aa300); import-linter fix
  via string-target importlib PEP-562 shim (ADR-0224). D, E agents dispatched
  in parallel; F, I still running.

## task:0001
- subject: A1 cpu-burst Part 2 — server-side decay (projection half shipped)
- status: pending
- context: docs/plans/cpu-burst-rootcause-and-embedding-scan-fix.md · ADR-0005/0006
- description: heat_decay.py still loops per row with one UPDATE each.

## task:0002
- subject: v6/v7 horizon (not scheduled) — incl LLM semantic dedup/merge
- status: pending

## task:0003
- subject: Perf-loadtest — concurrent daemon-survival + finish
- status: pending
- context: R1 of docs/plans/archive/perf-loadtest-remaining-deferred-2026-07-14.md (NOT ADR-0129)
- description: Phase B unbuilt per run_perf_loadtest.py:54-60.

## task:0004
- subject: Adopt stamina library for 2 retry loops (ADR-0103)
- status: pending
- description: Zero `stamina` refs in tree; ADR decided SWITCH-PARTIAL, never executed.

## task:0005
- subject: drift-axis-sweep — dead-code/config/producer↔consumer flow-gap ratchet
- status: pending
- context: docs/plans/drift-axis-sweep-2026-06-30.md (status still "scoping")
- description: General ratchet for the DUPLICATION class (18.2% of 203 defects, the largest cause). Plan's own three scripts do not exist.

## task:0007
- subject: Concurrent-load latency degradation — re-baseline (p95 M=16 phantom, see 0024)
- status: pending

## task:0008
- subject: A — mechanical harness task-seeder (fallback if forcing-nudge B fails)
- status: pending

## task:0009
- subject: normalize_write_context('global') CWD-sensitivity guard
- status: pending
- description: server_helpers.py:375 short-circuits only the EMPTY string; 'global' still path-walks. Only fix ever applied was a test-side monkeypatch.chdir.

## task:0010
- subject: C4 SNR score-side S3 + wiki_query ranking — eval-gated
- status: pending
- context: docs/plans/recall-scoring-c4-2026-07-18.md §7.3 (S3 self-documented DEFERRED)

## task:0011
- subject: Multi-client nix provisioning — likely DUPLICATE of 0018/0019/0021/0022/0023
- status: pending
- description: No nix client-provisioning content exists; real blocker is 5 stub emitters at hooks_render.py:547-553. Consider merging.

## task:0012
- subject: [POST-V6] Viz slider followups — 4 deferred sliders + Option B latency
- status: pending

## task:0013
- subject: [DEFERRED] Analyze agent-instructions.md vs yadgar library/template overlap
- status: pending

## task:0015
- subject: Agent-prompt rework — prune, gotchas, fuzzy-match (usage READER now decided, see 0047)
- status: pending
- description: Contract rail + counter SHIPPED. Open: 57/65 pages vs a target of 52 (went UP), 14 deprecated-but-live; 1 gotcha line in the seed corpus; no fuzzy fallback. D3 (2026-08-05): the `uses` reader ships WITH the spine migration — counts are the pruning evidence and are not obtainable retroactively.

## task:0016
- subject: [REVISIT — user] Dogfood findings + agent-prompt pre-plan
- status: pending

## task:0017
- subject: [POST-V6] viz galaxy — node-type clumping, cap per-type in core
- status: pending
- description: Never scoped into any plan/ADR — fresh idea, not stalled work.

## task:0018
- subject: [client-port] Codex hooks — BUILD (incl BLOCKING stop)
- status: pending
- context: docs/plans/port-codex-2026-07-20.md

## task:0019
- subject: [client-port] Cline hooks — BUILD
- status: pending
- context: docs/plans/port-cline-2026-07-20.md

## task:0021
- subject: [client-port] Windsurf hooks — BUILD (capture+drain only)
- status: pending
- context: docs/plans/port-windsurf-2026-07-20.md

## task:0022
- subject: [client-port] Kiro hooks — NEEDS-LIVE-TEST
- status: pending
- context: docs/plans/port-kiro-2026-07-20.md

## task:0023
- subject: [client-port] Amp hooks — NEEDS-LIVE-TEST
- status: pending
- context: docs/plans/port-amp-2026-07-20.md

## task:0024
- subject: Perf load-test — concurrency-sweep + re-baseline 0007; Ettin scales on cores not replicas
- status: pending

## task:0025
- subject: Audit session-exit hook — curated 4 KB snippet vs a real raw-conversation save
- status: pending
- description: Hook EXISTS and is installed (session-end-capture.py:306-323). Saves 5 turns / 4096 bytes, not the transcript. The audit itself was never written. Also home for anchor 528891 (agent that starts a VM owns stopping it).

## task:0028
- subject: PreCompact hook slows /compact — async fix
- status: pending
- context: docs/plans/precompact-async-global-hooks-2026-07-22.md

## task:0035
- subject: Migrate ~200 Settings knobs to the DB config store — plan REFINED, NO code
- status: pending
- context: docs/plans/settings-to-db-config-migration-2026-07-24.md (97 → 592 lines) · ADR-0198
- description: BLOCKED on 0098 (bootstrap train) and 0136 (dead-knob sweep is now a PREREQUISITE). Decided 2026-08-05: widen `_JSON_VALUE_TYPES` (runtime_config.py:52) to include float; ALL KNOBS GLOBAL (no `directory` column, so no per-project override ever); Batch 1 all-at-once fix-forward; STRUCTURAL-REINIT (~10 knobs) DEFERRED to config.yaml. Seeding the first row is a ONE-WAY DOOR on the project key (0095).
- blockedBy: 0136
- modified: 2026-08-08T10:00:00Z

## task:0040
- subject: Yadgar keyword cheat-sheet — viz help menu + docs/ ref'd from README
- status: pending
- description: help.js renders the graph LEGEND only; no cheat-sheet anywhere.

## task:0041
- subject: Post-setup onboarding — quickstart menu in `yadgar help` CLI
- status: pending
- description: No help/quickstart/onboard subcommand exists — `yadgar help` hits argparse "invalid choice".

## task:0043
- subject: Anchor system — BUILD the cull surface + field-collapse migration (audit is DONE)
- status: pending
- context: wiki page id 7558 (audit, 2026-07-24)
- description: The consolidation pass runs DRY-RUN ONLY, so no cull ever happens. Missing: audit_anchors(enumerate=True) and the field-collapse migration.

## task:0047
- subject: Ledger spine — REBUILD FRESH per ADR-0198. PR #32 closed unmerged, do NOT fix it incrementally
- status: in_progress
- context: docs/plans/task-table-refactor-2026-07-29.md · ADR-0198/0199/0200 (supersedes the 0182/0183 shape)
- description: Alembic revision 002_ledger_tables.py is OBSOLETE not buggy — it builds 3 uniform-shape tables; the decided schema is 10 per-entity tables with join tables, not JSON arrays. Blocked on 0098 + 0095. SEQUENCING (ADR-0200): the backend PTC does not exist, so "core stops touching the DB" is a BUILD not a re-route — build it before moving task.py/agent_prompts_ledger.py onto HTTP forwards. Carries ex-0094 (model tier → agent_pattern_model keyed pattern×client) and D3's `uses` reader (0015). LIVE DEFECT it must fix: ADR-0124 has a page and no index row (193 index rows vs 194 pages).
- blockedBy: 0095
- modified: 2026-08-09T19:36:00Z

## task:0048
- subject: DB migration script system — Alembic arm moves to 0098's bootstrap train
- status: pending
- context: docs/plans/investigation-migration-script-system-2026-07-26.md
- description: Surreal-side scope survives — 26 hand-rolled migrations Alembic does not cover.

## task:0057
- subject: OpenCode port follow-ups — ADR-0168, emitter cataloguing
- status: pending
- context: docs/plans/followup-opencode-port-2026-07-26.md

## task:0058
- subject: F1 — Real headless `opencode run` test (Bun + opencode binary)
- status: pending
- context: docs/plans/followup-f1-headless-e2e.md

## task:0059
- subject: F2 — Promote `session.idle` → `session.stopping` (gated on sst/opencode#16626)
- status: pending
- context: docs/plans/followup-f2-stop-blocking.md

## task:0060
- subject: F3 — Wire `chat.message parts[] mutation` (gated on F1)
- status: pending
- context: docs/plans/followup-f3-chat-message-wiring.md

## task:0063
- subject: Attribution rewrite — only the contribution-graph check remains
- status: pending
- description: USER ACTION, browser only — github.com/m-agahi. No API can verify it.

## task:0077
- subject: Set up recurring fresh-install QA (VM-based) as a regular practice
- status: pending
- context: ADR-0174 (open, cadence undecided) · wiki `connecting-to-libvirt-test-vms-debian-13-dev-etc-ssh-gotcha` · agent-prompt-vm-verify-live-install
- description: No workflow/timer/target exists. Three passes now, real bugs each time (2026-08-01: 5; 2026-08-02: 0124-0130; 2026-08-07: engine-#2 verify found 0145/0146). Install is 65 of 203 defects. A reusable dispatch pattern now exists: `agent-prompt-vm-verify-live-install`.

## task:0080
- subject: Context-window budget — the 123.7k fixed prelude is the untouched half
- status: pending
- description: Session-start cost scales with N OPEN TASKS, not page size, so page compaction barely moves it. The prelude is the real target.

## task:0093
- subject: Cross-project wiki scoping drift — 9 legacy rows PLUS a live write-side half-heal
- status: pending
- context: docs/plans/fix-agent-prompt-scoping-drift-2026-07-30.md
- description: store.py:753-766's UPDATE carries directory_context but OMITS branch, so a re-save heals the directory and pins the branch forever — never self-converges. All 9 rows still present. CONVERGES with 0144 (ADR-0215 removes branch entirely) — do not fix separately.

## task:0095
- subject: IMPLEMENT project identity — owner/repo + `project` registry + re-key the corpus (DECIDED, ADR-0199)
- status: pending
- context: ADR-0199 · split-store-engine-decision-2026-08-02.md §8.A1
- description: Key = owner/repo, HOST EXCLUDED, fallback local/<basename>; resolved ONCE per session (startup hook or `.yadgar/project-id` walking up from cwd) then passed by the caller, override for cross-project. `.yadgar/` NEVER committed. Registry check is load-bearing on write; non-session writers (nightly cycle, drainer, CLI) need their own path. SCALE: ~2,919 memories + ~2,237 wiki pages carry absolute-path directory_context. UNRESOLVED: rows whose paths NO LONGER EXIST need an explicit decision, not a heuristic. Slug = `owner_repo_kind-id` / `local_basename_kind-id`, "/"→"_", plain join (collision accepted), opaque + immutable, lowercased, .git stripped, cap 256.

## task:0096
- subject: Decide the encryption trust boundary — blocks sync design
- status: pending

## task:0097
- subject: AAA + team/org sync design — transport, key hierarchy, selective replication
- status: pending

## task:0114
- subject: INVESTIGATE — four unexplained host restarts/blackouts on nixos-quinyx (diagnosis only)
- status: pending
- description: (a) runners stack SIGTERM 09:29:06Z, no journal/oomd/timer explains it; (b) node_exporter + core scrape blackout 03:34→10:08 while cadvisor stayed up; (c) ~650 PID allocs/sec overnight vs 10/s after reboot; (d) why sudo would not run during the lockup. All invisible to current alerting.

## task:0115
- subject: No backup before backend-boot schema migrations — PLANNED, not built
- status: pending
- context: docs/plans/0115-pre-migration-backup-2026-08-01.md
- description: Copy point is entrypoint-backend.sh between safe_start preflight and _start_surreal — the only moment the store is unopened (ADR-0090 forbids copying a live surrealkv dir). Migrations run in BOTH processes and .migration.lock does not serialise them.

## task:0116
- subject: Typed contracts at the in-process seams — PLANNED, not built
- status: pending
- context: docs/plans/0116-protocol-typed-payloads-2026-08-01.md · bug-cause-audit-203-defects-2026-08-01.md
- description: Repo NOW has a differential mypy ratchet (scripts/check_type_ratchet.py, baseline 1206 errors / 145 files) shipped in PR #35 car 1 — but still no blanket type gate. First seam S2 (param objects → frozen kw_only).

## task:0117
- subject: SPIKE — measure the real cost of a transport seam (direct vs HTTP vs UDS vs gRPC)
- status: pending
- description: Use the existing core→backend hop as control. runtime_config_client.get has fan-in 1350 — report call-rate × latency as an annual budget, not a microbenchmark.

## task:0118
- subject: [DEFERRED — SaaS era] Inter-service API interface: gRPC vs HTTP/JSON vs UDS
- status: pending
- description: Prereqs 0116 then 0117. Weight install complexity heavily — it is 65 of 203 defects and gRPC adds codegen to exactly that surface.

## task:0120
- subject: pyproject per-file-ignores point at 5 deleted paths — C901/PLR0913 silently re-enabled
- status: pending
- description: Paths moved _shared/retrieval → backend/retrieval in e67bca9c; ruff drops missing entries silently. `_inject_ce_diversity` is at 7 params, one from tripping PLR0913.

## task:0122
- subject: flake.nix is a fourth unit renderer and emits a DIFFERENT set
- status: pending
- description: Eight unit blocks with per-unit Install.WantedBy and NO yadgar.target, vs nine target-pulled units elsewhere — the renderers disagree on activation TOPOLOGY. Nothing in Python can derive the nix arm. A FIFTH renderer (modules/home/yadgar.nix) is out of repo and untestable here.

## task:0124
- subject: [HIGH] Core loops forever when the backend is down for the whole start window
- status: pending
- description: entrypoint.sh:22-35 waits 120s, then await_backend_ready waits 60s more — both inside TimeoutStartSec=120. ~130s crash cycle that can never trip StartLimitBurst=5/10s, so it never reaches `failed`. test_core_startup_backend_ready.py:273 guards only the Python budget.

## task:0125
- subject: [MED] yadgar-vacuum.service sets no YADGAR_EMBED_URL — Car 0113's drain nudge is inert
- status: pending
- description: Every run WARNs and no-ops, so enter→drain→capture→export loses the drain. nightly-cycle already sets it; fix is one Environment= line in BOTH the Python renderer and flake.nix.

## task:0126
- subject: [MED] README's `pipx install yadgar` cannot work on stock Debian 13
- status: pending
- description: requires-python >=3.14 vs system 3.13; the error says "from versions: none", reading as "package does not exist". Working path `uv python install 3.14` + `pipx install --python` is undocumented. CONFIRMED AGAIN 2026-08-07 on the engine-#2 VM.

## task:0127
- subject: [LOW] yadgar-setup dies silently at step 3/12 under non-interactive stdin
- status: pending
- description: bootstrap_secrets.sh:134 — `set -euo pipefail` + bare `read` at EOF. INSTALL_NONINTERACTIVE=1 works and is undocumented. Same class one layer up: ADR-0199's startup hook must NEVER prompt without a TTY.

## task:0128
- subject: [LOW] Vacuum SKIP reason contradicts the actual cause when the launcher is pinned
- status: pending
- description: core/vacuum/__init__.py:1562 static string blames a missing image that was present; real cause was the operator pin. stdout is right, the persisted row is wrong.

## task:0129
- subject: [LOW] `update --install` refusal names a config key that does not exist
- status: pending
- description: Says `update.install_enabled`; real key is flat `update_install_enabled` (config_yaml.py:1148). Grep for other dotted-key refs in user-facing strings.

## task:0130
- subject: [LOW] /health returns status:ok 200 for ~2 probe intervals after the backend dies
- status: pending
- description: Payload is self-contradictory (status ok + db:false + embed:false) until fails=3. Blunts Car 0111's /health-503 vs /health/live-200 split for two probes.

## task:0131
- subject: [VERIFY] Vacuum-abort belts on a Requires=-pinned unit were never exercised
- status: pending
- description: A manual stop yields Result=success so Restart=on-failure never fires — the belts live on the VACUUM abort path and no vacuum was run against a Requires= unit. Close by pinning Requires=, running a vacuum, forcing an abort; then keep with a test or delete as dead code.

## task:0132
- subject: [HIGH] NOTICE / THIRD-PARTY-LICENSES + ratchet — SurrealDB is BSL 1.1, breach is LIVE
- status: pending
- context: split-store-engine-decision-2026-08-02.md §5.3 §8.1 · ADR-0195
- description: VERIFIED 2026-08-06 from upstream LICENSE: SurrealDB = Business Source License 1.1, Change Date 2030-01-01, Change License Apache-2.0, Additional Use Grant bars use "as a Database Service" (read as a SUMMARY — re-read verbatim before quoting). BSL requires the license be displayed on each copy; `COPY --from=surrealdb/surrealdb:v3.1.5` has shipped for many releases with NO NOTICE entry, so the breach covers already-published images. GATE IS PUBLISH, NOT MERGE. asyncmy VERIFIED Apache-2.0 — no GPL/LGPL conflict. DEFERRED, NOT RESOLVED: whether the SaaS tier may use SurrealDB at all — saas-security-posture-and-isolation-2026-08-02.md:256 still puts it in the cluster.

## task:0133
- subject: [MED] wiki_read / wiki_get return completely uncapped output
- status: pending
- description: Split from old 0085, whose recall() half shipped in v5.169.0. adr_list(limit=0) renders 57,988 chars. Needs a cap plus a _truncated envelope with a fetch hint.

## task:0135
- subject: [LOW] wiki_append_section has no content-size cap
- status: pending
- description: wiki.py:1206 caps nothing while wiki_write_task_list (:232) and wiki_add (:440) reject at 65,536 — and it is the path the stop-hook template recommends, so the routine documented write path is the unguarded one.

## task:0136
- subject: Dead-knob mechanical sweep + zero-reader ratchet — PREREQUISITE to 0035
- status: pending
- description: Establish deadness from the tree, not from the hand list — that list already named HOPFIELD_MAX_PATTERNS dead when it has a reader. For every Settings field find readers (attribute access, getattr, config_get by key), report zero-reader fields, then ratchet it in the I25 shape so knobs cannot go dead unnoticed. Re-verify rather than trust: HOPFIELD_BETA, RECALL_QUALITY_FLOOR, retired COMET_*. NOTE: 0144 removes BRANCH_ENFORCEMENT + BRANCH_BOOST_WEIGHT — coordinate so the sweep does not re-add them.

## task:0137
- subject: Superseded-ADR rank penalty — provider attaches status, Fusion weights (D1)
- status: pending
- description: Down-weight, never exclude — ADR-0196 is superseded yet its backup half still binds. providers/wiki.py does ONE batched status lookup for the ADR candidates it emits and attaches it via Candidate.raw (no dataclass change); FusionStage applies it as one more score term, which also lands correctly when the same ADR arrives from both FTS and KNN. NOT RulesStage — it runs last, after CE/NLI/MMR, so it could only reorder survivors, not stop a superseded ADR consuming a CE slot.

## task:0138
- subject: Viz panel for browsing + setting live knobs (knob §7)
- status: pending
- description: Ships WITH 0035, chosen over a CLI-later option. ADR-0198's `config.default_value` makes override-vs-default renderable with no extra state. Note: this is a WRITE surface for every system knob behind a UI. Ties to 0040/0041.

## task:0139
- subject: Test-governance gap surfaced by the PR #32 review — never written down until now
- status: pending
- description: `test_prompt_usage_counter` was rewritten to pass VACUOUSLY, and PR #32's tests passed only because mocks returned `{"number": ...}` while the code returned `{"id": ...}`. Needs a rule that a test may not be weakened in the same commit that changes the code under it, plus a sweep for other vacuous tests. Overlaps 0143 — both are "the test suite lies about what it covers".

## task:0140
- subject: Read/write MCP tool classification so maintenance windows stay read-available
- status: pending
- context: ADR-0210 · core/server/_app.py:517-559
- description: The gate short-circuits EVERY MCP tool including reads — no read exemption exists — so every maintenance window (nightly, vacuum, and now backup) is a full MCP outage in which recall/wiki_read/config_get fast-fail. ADR-0210 accepted the outage and filed this. Classification MUST fail CLOSED (unknown = write): a mis-classified write let through a snapshot window produces a silently corrupt backup.
- modified: 2026-08-06T20:15:00Z

## task:0143
- subject: Skip-gate is blind to DESELECTED tests — the engine-#2 live suite never ran in CI
- status: pending
- context: .github/workflows/ci-pr.yml (test-backend) · pyproject.toml:278
- description: Two defects. COVERAGE — no CI job anywhere boots a real MariaDB; grep for mariadb/mysqld/asyncmy across all 7 workflow files returns ZERO. GATE BLINDNESS (the worse one) — marker exclusion DESELECTS rather than SKIPS, so deselected tests emit no SKIPPED line and pytest-rs-backend.txt cannot see them. A guard built to catch "tests that stopped running" is blind to the commonest way tests stop running. Fix (b) is the generalizable one: parse the DESELECTED count. Fix (a) is a job that boots mariadb:11.4 for the engine2_mariadb xdist group.
- modified: 2026-08-07T12:00:00Z

## task:0144
- subject: Remove branch scoping ENTIRELY — ADR-0215 (+0217; 0216 SUPERSEDED)
- status: completed
- active_form: Running the branch-removal train
- context: ADR-0215 · ADR-0217 · ADR-0218 · docs/plans/branch-scoping-removal-2026-08-07.md · branch feat/branch-scoping-removal
- description: Cars 0-5 + remediation LANDED on trunk eb2d1be1 (14 commits ahead
  of master, 16 commits on the branch-scoping-removal branch; core 5.179.0 /
  backend 5.69.0). Residue vs Car 0 baseline: BranchFilter 47->0,
  missing_branch 151->16, YADGAR_CI_BRANCH 115->17, branch_hint 599->131,
  _detect_branch 293->148, _get_default_branch 146->76. REMAINING: Car 6
  (detection + dead tooling + test corpus + gitness deletion per ADR-0217 +
  Car 4's 3 red byte-pin tests), Car 7 (knobs + registry), Car 8 (data
  migration, USER-GATED, one-way), Car 9 (migration 029 + export/views.sql
  v_branch_distribution), Car 10 (docs + 3 ADR amendments + residue proof).
  CAR 6 HANDOFFS: subtract 32 test fns from its collected-count delta (Car 5
  deleted them); the 7 monkeypatch _get_default_branch sites must die WITH the
  function or raise AttributeError; project.py::_get_default_branch returns
  None unconditionally since Car 3, silently disabling the roadmap-lag signal.
  BLOCKS COMPLETION (RESOLVED 2026-08-08): the feared Q3 reseed of ~17
  agent-prompt/discipline wiki pages was overstated — premise wrong. Real scope
  was ONE page (agent-prompt-structured-wiki-page-prune, two branch_hint
  occurrences in its Prompt skeleton), fixed surgically, now at version 4. All
  64 agent-prompt-* + 6 agent-discipline-* pages were read and cross-checked
  against a corpus-wide string::contains scan; the remaining hits are ADRs,
  task-lists and archaeology pages — history, correctly left alone. This
  blocker no longer applies to completion.
  THREE CARS SHIPPED GREEN WITH REAL BREAKAGE (Car 1: a live TypeError + 19 red;
  Cars 1/3: MemorizeContext.branch_hint constructors, 21 more red) — every one
  caught by a LATER car, never by the car that caused it. Merge-as-you-go is
  what keeps that attributable.
- blockedBy:
- blocks: 0093, 0137, 0140
- modified: 2026-08-08T12:00:00Z

## task:0145
- subject: [LOW] pretooluse-router G4 substring-matches container names with no host awareness
- status: pending
- context: yadgar/core/hooks/pretooluse-router.py
- description: Blocks any command containing `podman exec yadgar-backend` / `yadgar-db` regardless of target host. Fired against an unrelated throwaway container named `yadgar-backend-p1` on a remote test VM. Two defects: substring rather than exact-name match, and no notion that the command was ssh-wrapped to another machine. Do NOT simply widen it — a too-permissive guard is worse than the friction.
- modified: 2026-08-07T13:00:00Z

## task:0146
- subject: [MED] Restore-verification REJECTION surfaces as bare HTTP 500, not a structured error
- status: pending
- context: yadgar/backend/admin_exec/restore_sql.py · the /admin route layer
- description: RestoreVerificationError propagates unhandled, so callers get a 500 + traceback. The ACCEPT path returns a clean structured 200 with checks/violations/unavailable. A rejected restore is an EXPECTED outcome, not a server fault — automation currently cannot tell "correctly refused" from "backend fell over" without string-matching, and it pages as a yadgar bug. Return the same structured payload with a 4xx; keep the tri-state ok/violation/unavailable intact.
- modified: 2026-08-07T13:00:00Z

## task:0147
- subject: Deploy-ordering trap (ADR-0210/0214) is UNTESTED and unreachable on a fresh VM
- status: pending
- context: ADR-0210 · ADR-0214 · wiki [[engine-2-mariadb-verified-operational-facts-on-a-real-host]]
- description: The 2026-08-07 VM pass verified all 4 engine-#2 steps but NOT this: the trap is old core against new backend, and on a from-scratch VM nothing pre-exists so there is no ordering to get wrong. It is the LAST uncovered risk on the engine-#2 deploy path and exists ONLY on an upgrade — i.e. on the user's system and nowhere else. Options: (a) run the previous core image against the new backend on the VM; (b) assert the maintenance-response contract at the seam against the old client code — cheaper, narrower; (c) enforce restart-core-first operationally and document it. Car E was deliberately purely additive, but "designed survivable" is not "observed to survive".
- modified: 2026-08-07T13:00:00Z

## task:0148
- subject: anchor_audit_prompt.md gathers wrong rows — recall(tags=) inert for memory results
- status: pending
- context: core/hooks/templates/anchor_audit_prompt.md · yadgar/backend/retrieval/recall_pipeline.py · yadgar/tests/hooks/test_v5_158_anchor_audit_scheduler.py
- description: anchor_audit_prompt.md:18-21 calls recall(tags=["_anchor"], type="memory"); the tags filter applies to WIKI results only (recall.py:358-360, recall_pipeline.py:436 passes tags only to WikiProvider, MemoryProvider :422-425 never receives it), and under type="memory" the wiki provider is not even constructed. Every anchor-audit pass has run an unfiltered semantic search over arbitrary rows instead of anchors. Guard test test_v5_158_anchor_audit_scheduler.py:151-165 is substring-only and passed for the bug's entire lifetime — vacuous-pass family. Fix has two halves: gather anchors by a real predicate, and replace the substring guard with a byte-pin (pattern at test_stop_hook_template.py:42,289).
- modified: 2026-08-08T10:00:00Z

## task:0149
- subject: Bulk anchor-renewal path — anchor_renew is per-row, 100 approved keeps face the 2026-08-26 cliff
- status: completed
- context: docs/plans/anchor-refactor-2026-08-08.md · ADR-0219 · ADR-0220
- description: Resolved WITHOUT building a bulk path — 98 rows renewed individually via anchor_renew, staggered 150-240 days by directory group so the next expiry does not rebuild a synchronised cliff. Verified `SELECT count() FROM memory WHERE migration_grace = true AND is_protected = true` -> 0. All stayed tier=conditional; migration_grace cleared, so the next expiry is visible in the signal (ADR-0083 excludes grace rows, which is why the original cliff was silent). memory:490141 and memory:491179 were restored to full anchor status before the renewal run and both renewed (491179 as canary, 490141 in the ttl=240 group) — verified live: is_protected=true, migration_grace=false, tier=conditional. 518764/518775/518850 were deliberately retired via de_anchor as in-flight/TODO-class rows and are meant to lapse at 2026-08-26 — verified live: is_protected=False, migration_grace=true, tier=ephemeral. Sole open item: 518850 reads on a fuller pass as a reusable OAC-migration pattern (two concrete gotchas, generalised to mobileapp.quinyx.com); restoring it is pending the user's call.
- modified: 2026-08-08T12:15:00Z

## task:0150
- subject: Baseline-drift detection + three-way merge for seeded prompt/discipline/contract pages (ADR-0208)
- status: pending
- context: ADR-0208 · ADR-0209
- blockedBy: 0047
- modified: 2026-08-08T11:00:00Z

## task:0151
- subject: Stale second `## [Unreleased]` header stranded in docs/CHANGELOG.md
- status: pending
- context: docs/CHANGELOG.md
- description: Cosmetic but breaks changelog parsing.
- modified: 2026-08-08T11:00:00Z

## task:0152
- subject: Collapse memorize + checkpoint + task sync into one checkpoint call
- status: pending
- description: Three round-trips doing one logical operation.
- modified: 2026-08-08T11:00:00Z

## task:0153
- subject: BC-G11 is a green tick pointing at a test Car 1 deleted
- status: completed
- context: docs/contracts/BEHAVIOR_CONTRACT.md
- description: Car 10 re-pointed BC-G11 at tests/e2e/test_phase1_db_layer.py::TestBCB2_WikiDirectoryFilter::test_aws_wiki_excluded_from_yadgar_recall; verified the file, class and method all exist and check_contract_coverage.py exits 0. A second dangling pointer (BC-I32, pre-existing path drift) was fixed in the same pass.
- modified: 2026-08-08T12:00:00Z

## task:0154
- subject: `_project_init` has an age signal but no staleness threshold — 81 days of silent drift
- status: pending
- context: yadgar/core/server/tools/project.py
- description: project.py::_build_recommended_actions emits bootstrap_project on PRESENCE only (:1121), while active_work and checkpoint are both warn- and stale-thresholded (:1131/:1139/:1150/:1158). init_memory_age_hours is computed (:1636, :1718) and compared to nothing. NOTE for whoever picks this up: pointing the action at seed_project is NOT viable — it is container-blind and fails with "Not a directory: /home/max/git/yadgar" (same class as ADR-0157). The fix must age-gate bootstrap_project with caller-supplied content.
- modified: 2026-08-08T11:00:00Z

## task:0155
- subject: check_invariants: 1 relationship row references a non-existent entity ID
- status: pending
- context: ADR-0222 · check_invariants
- description: Standing data-model violation surfaced by the 2026-08-08 vacuum: relationship_dangling_other: 1. check_invariants auto-repaired three siblings in the same run (1 dangling wiki_crossref, 1 phantom entity row named memory:<N>, 29 memories rebalanced out of over-occupied engram slots) but has no repair path for this one. The vacuum correctly KEPT its swap — per-table counts verified identically pre/post, and a vacuum neither causes nor clears this class. Also noted: memory_transition TIMED OUT during that check, so the table went unverified — worth checking whether that timeout is chronic, since an invariant that never completes is a guard reporting nothing.
- modified: 2026-08-08T12:00:00Z

## task:0156
- subject: agent-prompt-toc lists ~9 dead patterns whose pages do not exist
- status: pending
- context: agent-prompt-toc
- description: TOC advertises pattern names that return "not found" on wiki_read: dispatch-flux-overlay-patch-pr, dispatch-build-flux-convergence-pr, dispatch-readonly-infra-audit, dispatch-review-terraform-plan, locate-config-monorepo, build-and-open-pr, dispatch-flux-adoption-audit, dispatch-flux-post-merge-verify, install-opencode-yadgar-plugins. Independently spot-verified build-and-open-pr. Matters because a guessed pattern slug returns an EMPTY fallback contract rather than an error, and the TOC is the designated cure for that — but a dead TOC entry lands on the same empty fallback anyway. Fix: prune, and add a lint asserting every TOC entry resolves — same family as check_contract_coverage.py's dangling-test-reference rule.
- modified: 2026-08-08T12:00:00Z

## task:0157
- subject: 0047 spine train — complete all 16 cars, push train, open PR
- status: in_progress
- active_form: Building 0047 spine train
- description: 16-car spine train (A0, A, B, C1, C2, C3, D, E, F, G, H, I, J, K, L, M). Per-car worktree off train tip; TDD + strict typing; scoped tests only (CI at PR time runs full suite). Currently in progress: F and I agents running; D and E agents just dispatched. After all cars land: ff-merge into train, final push to origin, open PR. Then doc-update gate + 3-pass audit + post-merge cleanup + version tag + nix bump. See docs/plans/task-table-refactor-2026-07-29.md §7 §16 and per-car plan docs.
- modified: 2026-08-09T19:36:00Z
