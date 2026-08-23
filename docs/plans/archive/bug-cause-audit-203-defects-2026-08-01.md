# Bug root-cause audit — 203 shipped defects, 2026-06-03 → 2026-08-01

Question: split yadgar into microservices, or not? Settle with data.
Classify shipped bugs by root cause. Each row asks: **would service boundary have prevented it?**

Not advocacy. Counts first. Verdict after.

## Corpus + method

- Source: `docs/CHANGELOG.md`, newest backwards. 302 version sections parsed, 974 entries extracted,
  376 bug-shaped, **203 distinct defects after dedupe**.
- Dedupe rule: one defect fixed across many cars/commits = ONE bug. Sub-bullets (`**Root cause:**`,
  `**Fix:**`, `**TDD:**`) fold into parent, never counted alone. Multi-site sweeps of one wrong rule
  (4 prune passes, 13 `docker` literals, 12 `except X, Y:` sites) = one bug each.
- Range actually covered: **2026-06-03 (v5.42.2) → 2026-08-01 (v5.170.15 / `[Unreleased]`)**.
  Older sections exist; not needed to reach 200. v5.42–v5.72 headings carry no date — backfilled from
  `git tag` creatordate plus in-text evidence (v5.42.2 cites live probe 2026-06-02).
- Excluded as non-bugs: features, docs, refactors, dead-capability removals with no failure.
- One primary cause per row. Two causes coexist often. Primary = thing that had to be different for
  bug not to exist.

## Rubric (applied mechanically)

| cause | discriminating question |
|---|---|
| DUPLICATION | Same rule written in 2+ artifacts, and they drifted? |
| MISSING-INVARIANT | Rule right in one place, nothing enforced it, later edit broke it silently? |
| IN-PROCESS-COUPLING | A reached into B's internals, or shared mutable state inside ONE process broke unrelated caller? Two modules calling one function is NOT this. |
| CROSS-PROCESS-CONTRACT | Defect at EXISTING boundary — core↔backend HTTP, ↔SurrealDB, daemon↔systemd, host-CLI↔container, ↔OTLP collector, browser↔core HTTP. |
| ENVIRONMENT/INSTALL | Code right, environment/packaging/deployment wrong. |
| LOGIC | Ordinary local defect. No architectural signal. |
| OTHER | Reason stated (test isolation, prompt wording, CI policy, UX fidelity). |

`service boundary?` column:
- **YES** — hard process boundary makes bug impossible.
- **NO** — boundary orthogonal; bug survives split unchanged.
- **WOULD-HAVE-CAUSED-IT** — defect exists *because* boundary already there.

## Counts

```
LOGIC                        49   24.1%
DUPLICATION                  37   18.2%
ENVIRONMENT/INSTALL          32   15.8%
CROSS-PROCESS-CONTRACT       31   15.3%
OTHER                        29   14.3%
MISSING-INVARIANT            13    6.4%
IN-PROCESS-COUPLING          12    5.9%
```

```
NO                          160   78.8%
WOULD-HAVE-CAUSED-IT         36   17.7%
YES                           7    3.4%
```

## Trend over time

```
bucket                  N  DUPLICATI  MISSING-I  IN-PROCES  CROSS-PRO  ENVIRONME      LOGIC      OTHER
2026-06 A (1-15)      102   18 17.6%    4  3.9%    7  6.9%    9  8.8%   24 23.5%   17 16.7%   23 22.5%
2026-06 B (16-30)      44    9 20.5%    3  6.8%    2  4.5%    9 20.5%    1  2.3%   18 40.9%    2  4.5%
2026-07 A (1-15)       14    2 14.3%    3 21.4%    2 14.3%    2 14.3%    2 14.3%    2 14.3%    1  7.1%
2026-07 B (16-31)      43    8 18.6%    3  7.0%    1  2.3%   11 25.6%    5 11.6%   12 27.9%    3  7.0%
```

Viz + test-infra rows stripped (topology-neutral both ways), same buckets:

```
N = 144
bucket                   N   IN-PROC   CROSS-PROC   DUPLICATION
2026-06 A (1-15)        58    3  5.2%      5  8.6%       9 15.5%
2026-06 B (16-30)       33    2  6.1%      9 27.3%       8 24.2%
2026-07 A (1-15)        14    2 14.3%      2 14.3%       2 14.3%
2026-07 B (16-31)       39    1  2.6%     11 28.2%       7 17.9%
```

**Careful claim, because counts are small.** Coupling per bucket is n=7/2/2/1 raw, n=3/2/2/1 filtered.
That is integers, not a curve. Buckets also differ in KIND, not just time: 06-A is the install train
(v5.46.x, ~35 rows dated 2026-06-05), 06-B the viz train, 07-B the install/vacuum train. Fortnight
variation mostly reflects which train shipped, not a drift in the codebase.

What survives that objection:

- **IN-PROCESS-COUPLING is small in EVERY bucket** — 2.3–14.3% raw, 2.6–14.3% filtered. It never
  dominates, and it does not rise.
- **CROSS-PROCESS-CONTRACT exceeds coupling in every well-populated bucket** (8.6 vs 5.2, 27.3 vs 6.1,
  28.2 vs 2.6 filtered). Only the thin bucket (07-A, n=14) ties at 2–2.
- `2026-07 A` holds 14 bugs because that fortnight ran the reorg trains (ADR-0056/0060/0062) — feature
  and refactor entries, few `fix:` lines. Treat that column as noise.

No rising-coupling signal exists in this data. That is the load-bearing finding, not an arrow.

## Where pain actually is

```
tests                      25
viz                        22
install                    12
ci                         9
vacuum                     6
install templates          6
retrieval                  6
packaging                  5
hooks                      5
config                     4
backend/retrieval          4
server/tools               4
wiki                       4
daemon/systemd             3
daemon                     3
observability              3
storage                    3
server/http                3
update                     2
backend/auth               2
_shared/storage            2
server/control             2
consolidation              2
recall                     2
backend/viz SSE            2
```

Collapsed:

| subsystem | bugs | split help? |
|---|---:|---|
| install / setup / templates / packaging / CI / release | **65** | No. One deployable → N makes 65 more than 65. Biggest hotspot, and it is the *deployment* surface, not the code. |
| test infra / fixtures / xdist | 25 | No. Independent deploys make this harder. |
| viz (browser JS/CSS + its core routes) | 22 | Already across a boundary. 4 of its bugs ARE that boundary drifting. |
| retrieval + fusion + recall + storage | 21 | Only place with real coupling. Already 100% sunk to backend (ADR-0078). |
| vacuum + nightly + ops | 12 | Worse. Every one is host-CLI vs container vs systemd coordination. |
| daemon / systemd / update | 8 | Worse. These ARE the process-supervision bugs. |

**Hotspots do NOT line up with plausible service seams.** Top hotspot (install/CI/packaging, 65) is
not a service — it is the thing that gets duplicated per service. Second (viz, 22) already remote.
Retrieval stack — the one plausibly extractable service — produced 21 bugs, only 5 of them coupling.

`code_graph` digest (project `tmp-yadgar-code-graph-6pn2qfho-wt`, from the injected memory block;
live `code-graph query` no longer resolves that slug — index rotated, so these are 2026-07-28 digest
figures, not a fresh query): `_shared` fan-in 4674, `core` 4740 in / 253 out, `backend` 1914 in / 532
out. Freshly measured layer sizes: core 167 files / 43.9k LOC, `_shared` 106 / 30.7k, backend 138 / 26.8k.

One correction to the digest's face value: it reports `_shared` fan-out 0, but ADR-0057 records that
enforcement surfaced **~15 real `_shared`→core violations**, resolved partly by reclassifying modules
and partly by **waiving** the composition-root `lifecycle`/`state`→core-daemon edges via import-linter
`ignore_imports`. So `_shared` is not a pure leaf — the boundary is convention-plus-waiver, and the
digest's 0 reflects lint config, not graph truth. Directionally the layering is still clean and
mechanically enforced (4 HARD contracts, ADR-0062 I34); it is just not proof of a perfect DAG.

Biggest single coupling point in the tree is `runtime_config_client.get` at fan_in=1350 — **config
reads**. A service split does not decouple that. Every service still needs config.

## The tax, as a rate

31 CROSS-PROCESS-CONTRACT defects over ~5–6 existing seams (core↔backend HTTP, ↔SurrealDB
embedded/server, daemon↔systemd/launchd, host-CLI↔container, ↔OTLP collector, browser↔core HTTP).
Roughly **6 shipped defects per boundary per 2 months**. Plus 5 ENVIRONMENT/INSTALL rows whose verdict
is WOULD-HAVE-CAUSED-IT (credentials + startup ordering that exist only because a second process must
be fed them). Total **36 boundary-caused, 17.7%**.

Stress-test that number — not all 36 multiply:

```
WOULD-HAVE-CAUSED-IT total: 36
  multiplies per new service: 24
  fixed-cost seam (browser/DB/collector), does NOT multiply: 12
  fixed-cost rows: U-18, 5102-1, 5097-1, 5072-3, 5070-1, 5063-1, 5012-2, 5012-3, 5010-1, 5004-1, 5004-2, 4606-4
conservative ratio (multiplying tax : prevented) = 24:7 = 3.4:1
headline ratio  (all boundary tax : prevented)   = 36:7 = 5.1:1
```

- **24 multiply per new service**: unit file, image tag, credential plumbing, health probe, readiness
  gate, independent version. Each new service replays these.
- **12 are fixed-cost seams that already exist and would not be replicated**: browser↔core HTTP/SSE,
  core↔SurrealDB transport, ↔OTLP collector. Counting them against the split would be dishonest.

Conservative ratio: **24 multiplying-tax defects vs 7 the split would prevent = 3.4 : 1 against.**
Headline ratio, all boundary tax: 5.1 : 1 against. Use 3.4 : 1.

## Steelman: strongest case FOR splitting

State it at full strength.

1. **Coupling bugs that exist are the nastiest.** `5056-4` (`retrieval/core.py` FTSParams arg order
   broken by the yellow-batch refactor) is verbatim the user's complaint — refactor one module,
   silently break a caller. `5097-2` (fusion `mem.pop("embedding")` starving MMR downstream) is one
   pipeline stage mutating a shared dict another stage depends on. `5086-4` (`resolved_by` edges never
   produced — extractor/handler type mismatch) shipped a feature that silently produced nothing for an
   unknown span. All three are untyped in-process dict/positional passing. Wire contract kills all three.
2. **Process-global mutable state is real and invisible.** `5090-2` (unguarded `_query_cache`, circuit
   breakers, `_enrichment_pipeline` double-init under concurrent tools), `5094-1` (unbounded default
   executor — one leaked recall starves the loop), `5059-2` (rerank merge injecting foreign dict shapes
   into a shared result list). Symptom always far from cause. Address-space boundary makes each impossible.
3. **User's pain is a real category, not phantom.** 12 is not zero. "A refactor cascades" describes
   `5056-4`, `5097-2`, `5086-4` exactly.
4. **The split already done works.** ADR-0078 sank retrieval fully into backend; changelog records
   "no dead in-core retrieval path exists — `_st._retriever` is `None` in core." Clean execution.
   Evidence the team CAN split.

## Case against

1. **Volume.** 7 prevented vs 24 multiplying-tax created.
2. **Direction.** No rising-coupling signal in any bucket. Boundary defects are 2–4x coupling in every
   well-populated bucket.
3. **DUPLICATION is the dominant architectural cause (37, 18.2%)** — and nearly every instance is
   multi-artifact drift across *deployment* surfaces: two systemd generators, shell template vs Python
   renderer, registry default vs code default, CI env var vs a daemon that never read it, `.in`
   templates vs `daemon.py`, four install paths mounting `/data` four ways. **A split multiplies exactly
   this class.** See `U-08`, `U-09`, `U-12`, `U-15`, `U-16`, `4612-1`, `4620-1`, `5167-1` — already
   shipped repeatedly with only two services.
4. **Recurrence evidence is explicit, and it is not coupling.** Changelog names repeats itself: `U-08`
   is "the **third** instance of one class" (hardcoded runtime in a generated artifact); `U-16` is "the
   THIRD instance in two days" (install-generated artifact missing an auth credential, ADR-0180). Both
   are cross-artifact duplication at a deployment boundary.
5. **Two services already ship version-skew bugs.** `U-09`, `U-12`, `4612-1`: core and backend version
   independently, something pulled or tagged only one. N services makes it N-way.
6. **Named exemplars hold up.** Config PTC core-side only, `Requires=` cascade (`U-08`), vacuum
   stop-the-world / rollback family (`U-05`, `U-21`, `U-24`, `U-25`, `5072-2`) — all boundary tax, all
   counted, all WOULD-HAVE-CAUSED-IT.

## Verdict

**Data does not support a microservice split. It argues against, and points at a different fix for the
same pain.**

Premise ("roles tangled, refactor cascades") = 5.9% of shipped bugs, no rising trend. Tax the split
multiplies = 24 defects in 2 months, 3.4x what it would prevent. Dominant architectural cause —
duplication across deployment artifacts, 18.2% — gets **worse** with more deployables.

Pain real. Diagnosis wrong. What evidence points at:

1. **Typed contracts at existing in-process seams, no new processes.** All three severe coupling bugs
   were untyped dict/positional passing. Dataclasses or TypedDicts at the retrieval-stage seams buy the
   serialization discipline of a service boundary at zero deployment cost. Honest 80% of the split's
   benefit.
2. **Kill duplication, do not add more.** One generator, not four. One credential resolver, not three.
   Project already does this well — `test_admin_token_cross_generator.py`,
   `test_backend_unit_queue_base_cross_generator.py`, `test_vacuum_trigger_cross_generator.py` each
   retired a recurring class. Keep building those. That is what 37 DUPLICATION bugs ask for.
3. **Ban process-global mutable state in library code.** Lint forbidding `os.environ[...] =` and
   `logging.disable` outside entrypoints kills 4 of the 12 coupling bugs outright.
4. **If any split is warranted, finish the one underway.** ADR-0060/0062 core=router / backend=compute
   / `_shared`=contracts is coherent and import-linter enforced. Finish that. Do not start a new topology.

If the user still wants the split: honest framing is that it is a bet the *severity* of 12 coupling
bugs exceeds the *volume* of 24 boundary bugs it creates. Defensible on a much larger team. Not what
this data shows for this codebase.

## Honest limitations — what this cannot see

1. **Narrator is the defendant.** CHANGELOG root causes written by the same process that fixed the bug,
   same session. Bug fixed by breaking an import edge gets described as coupling; bug fixed by adding a
   cross-generator test gets described as duplication. **Fix shape biases stated cause**, and this
   classification inherits that bias whole. Most likely **understates** IN-PROCESS-COUPLING — coupling
   is the least flattering thing to write down and easiest to re-describe as "wrong argument order".
2. **Survivorship.** Only shipped-and-described bugs here. Caught-in-review, fixed-in-same-commit, and
   never-written-up bugs are invisible. Refactor-cascade bugs are exactly the class most likely fixed
   silently mid-branch and never reaching a CHANGELOG line. Systematic bias against the user's
   hypothesis that cannot be corrected from this corpus.
3. **Severity unweighted.** Every row counts 1. A 68-second stop-the-world equals a stale CSS selector.
   If a coupling bug costs 5x an install bug in debugging time, the ratio narrows sharply. Time-to-fix
   not measured; corpus does not carry it.
4. **Viz + test buckets dilute the denominator.** 59 of 203 are browser JS/CSS and pytest fixtures,
   neither affected by backend topology. Excluding both (N=144): coupling 5.6% (8), boundary-caused
   22.2% (32), YES 7. Conclusion strengthens, does not reverse.
5. **IN-PROCESS-COUPLING vs LOGIC is the judgement call that moves the answer.** Applied strictly per
   the brief ("two modules both call this function" is not coupling). Looser reading moves ~6–10 LOGIC
   rows (e.g. `5129-1`'s N+1 via a callee's default flag) into coupling → ~9–11%. Still below boundary
   tax, but the gap narrows. This is the boundary a skeptical reader should probe first.
6. **YES verdicts applied conservatively.** Four coupling bugs whose *observed* manifestation was
   pytest-worker globals or browser global scope (`5545-17`, `5545-18`, `4607-7`, `5008-1`) are marked
   NO — a BACKEND service split does not fix either. A reader who counts them YES gets 11 not 7, and
   the ratio 24:11 = 2.2:1, still against.
7. **`code_graph` is a stale digest, not a live query**, and its `_shared` fan-out 0 is contradicted by
   ADR-0057's waived edges (see above). Layer LOC counts are freshly measured.
8. **`[Unreleased]` spans 55 releases.** Dates inside inferred from car numbers and git commit dates,
   not section headings. Bucket edges ±1–2 days. 06-A is inflated by ~35 v5.46.x rows all dated
   2026-06-05 and 18 v5.54.5 rows all dated 2026-06-13 — see the trend caveat.

## Full table (203 rows, newest first)

| # | id / version | date | one-line description | primary cause | service boundary? |
|---|---|---|---|---|---|
| 1 | U-01 / v5.170.15 | 2026-08-01 | detect_install_method() returned 'unknown' for stock modern pipx (XDG PIPX_HOME) -> self-update unreachable | ENVIRONMENT/INSTALL | NO |
| 2 | U-02 / v5.170.14 | 2026-08-01 | test-capped.sh failed OPEN when systemd-run absent; six Makefile targets never called it | MISSING-INVARIANT | NO |
| 3 | U-03 / v5.170.13 | 2026-08-01 | podman arm of _readiness_directives set no TimeoutStartSec; docker arm already had 180/120 | DUPLICATION | NO |
| 4 | U-04 / v5.170.12 | 2026-08-01 | Type=notify units had no READY=1 source on docker (no sd_notify proxy); .in templates hardcoded podman socket | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 5 | U-05 / v5.170.9 | 2026-08-01 | container-only host could never vacuum: side build needs `surreal` binary that exists only inside backend image | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 6 | U-06 / v5.170.9 | 2026-08-01 | .pre-vacuum-* snapshots pruned only in _vacuum_finalize, never on abort -> disk filled -> permanent silent skip | LOGIC | NO |
| 7 | U-07 / v5.170.x | 2026-07-31 | vacuum_export_*.surql scratch leaked on every abort path (1.4 GB accumulated) | LOGIC | NO |
| 8 | U-08 / v5.170.11 | 2026-08-01 | every generated systemd unit hardcoded `docker` (13 sites); Python generator diverged from shipped .in templates | DUPLICATION | NO |
| 9 | U-09 / v5.170.10 | 2026-07-31 | `yadgar upgrade` pulled only the core image (backend versions independently) and hardcoded podman | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 10 | U-10 / v5.170.4 | 2026-07-31 | unclosed urllib HTTPError/response leaked file wrappers -> ResourceWarning fatal under py3.14 gate | LOGIC | NO |
| 11 | U-11 / v5.170.8 | 2026-07-31 | config registry default YADGAR_BACKEND_VOLUME named a volume that never existed; I25 compared presence not values | DUPLICATION | NO |
| 12 | U-12 / v5.170.2 | 2026-07-31 | `daemon pull` fetched only the core image -> `daemon start` had no working recovery path | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 13 | U-13 / v5.170.3 | 2026-07-31 | vacuum ':8080' fallback literal in 3 modules disagreed with config_registry's 8000 | DUPLICATION | NO |
| 14 | U-14 / v5.170.5 | 2026-07-31 | code_graph render_digest used one shared body budget + tail cut -> endpoints section starved / cut mid-line | LOGIC | NO |
| 15 | U-15 / v5.170.1 | 2026-07-31 | `daemon start` mounted backend DB in a named volume; three install paths mounted /data three ways | DUPLICATION | NO |
| 16 | U-16 / v5.170.0 | 2026-07-31 | backend unit EnvironmentFile populated the UNIT env; container never saw admin token -> every /admin call 503 | ENVIRONMENT/INSTALL | WOULD-HAVE-CAUSED-IT |
| 17 | U-17 / v5.170.x | 2026-07-31 | core.cli imported core.server transitively -> OTLP/server import tax on every short CLI invocation | IN-PROCESS-COUPLING | YES |
| 18 | U-18 / v5.170.x | 2026-07-31 | OTLP exporter teardown blocked short-lived CLI exits when collector unreachable | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 19 | U-19 / v5.170.x | 2026-07-31 | three Python call sites probed readiness /health instead of liveness /health/live; pin covered only 3 non-Python surfaces | DUPLICATION | NO |
| 20 | U-20 / v5.170.x | 2026-07-31 | PyYAML imported by three shipped loaders but never declared as a dependency | ENVIRONMENT/INSTALL | NO |
| 21 | U-21 / v5.170.x | 2026-07-31 | vacuum verified via POST /api/check_invariants — a route registered nowhere (MCP tool only) -> permanent 404 -> silent rollback | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 22 | U-22 / v5.170.x | 2026-07-31 | after_bytes captured before finalize -> a fully rolled-back vacuum reported the saving it had discarded | LOGIC | NO |
| 23 | U-23 / v5.170.x | 2026-07-31 | _log_consolidation_row enumerates its fields -> new keys silently dropped while /sql still returned 200 | MISSING-INVARIANT | NO |
| 24 | U-24 / v5.170.x | 2026-07-31 | vacuum aborts stopped both units but restarted only the backend -> core left down until a human noticed | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 25 | U-25 / v5.170.x | 2026-07-31 | vacuum_now() wrote a trigger file no host-side watcher read; flake shipped no .path unit | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 26 | U-26 / vbackend 5.23 | 2026-07-31 | CacheStatsCollector hardcoded {ce,embed}; three recall caches fired into the CORE registry, invisible at backend /metrics | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 27 | 5167-1 / v5.167.1 | 2026-07-29 | install-generated backend units set no YADGAR_QUEUE_BASE -> backend drainer never started, writes never drained | ENVIRONMENT/INSTALL | WOULD-HAVE-CAUSED-IT |
| 28 | 5167-2 / v5.167.1 | 2026-07-29 | yadgar/backend/safe_start/ had no __main__.py after package conversion -> `python -m` preflight+recover silently dead | LOGIC | NO |
| 29 | 5167-3 / v5.167.1 | 2026-07-29 | `install --client claude-code` resolved MCP auth token from os.environ only -> headerless ~/.claude.json | ENVIRONMENT/INSTALL | NO |
| 30 | 5167-4 / v5.167.1 | 2026-07-29 | agent could Edit the hook-exception allowlist and revert to conceal it (no write guard) | OTHER: process/security guard, not a code defect | NO |
| 31 | 5166-1 / v5.166.1 | 2026-07-27 | install orchestrator read hooks result key 'path' while claude_code emitter returns 'settings_file' | DUPLICATION | NO |
| 32 | 5165-1 / v5.165.0 | 2026-07-23 | admin bearer token compared with != (timing side-channel) | LOGIC | NO |
| 33 | 5165-2 / v5.165.0 | 2026-07-23 | YADGAR_ALLOW_ROOT auth bypass not scoped to pytest -> could leak into production | LOGIC | NO |
| 34 | 5165-3 / v5.165.0 | 2026-07-23 | fusion belief branch blanket except silently discarded every belief on a config/storage error | LOGIC | NO |
| 35 | 5165-4 / v5.165.0 | 2026-07-23 | getattr(self._settings,...) fallbacks across retrieval hid Settings renames behind silent defaults | MISSING-INVARIANT | NO |
| 36 | 5165-5 / v5.165.0 | 2026-07-23 | SurrealQL bind-parameter key names interpolated without validation | LOGIC | NO |
| 37 | 5165-6 / v5.165.0 | 2026-07-23 | daemon.py+systemd.py never mounted the shared queue volume -> backend drainer processed nothing | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 38 | 5154-1 / v5.154.0 | 2026-07-20 | galaxy edges rendered with AdditiveBlending @0.9 -> dense core saturated to a white hairball | LOGIC | NO |
| 39 | 5151-1 / v5.151.0 | 2026-07-19 | stale dual-renderer CSS rule hid all View-menu panels forever after galaxy-only ADR-0138 | LOGIC | NO |
| 40 | 5150-1 / v5.150.0 | 2026-07-18 | update_active_work empty-state nudge routed agents to memorize instead of harness task tracking | OTHER: prompt wording | NO |
| 41 | 5150-2 / v5.150.0 | 2026-07-18 | _worktree_canonical_root lru_cache not cleared in _reset_server_state (its two sibling caches were) | DUPLICATION | NO |
| 42 | 5147-1 / v5.147.0 | 2026-07-17 | shipped viz galaxy/config/traces did not match the mockups that are the visual source of truth | OTHER: UX fidelity | NO |
| 43 | 5145-1 / v5.145.0 | 2026-07-16 | backend-emitted SSE events (heat_updated/memory_added/wiki_added) never reached core's /api/graph/events clients | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 44 | 5140-1 / v5.140.1 | 2026-07-15 | stop-hook template told the model to wiki_add without branch_hint; router rejects that in a git dir -> task-list mirror never persisted | DUPLICATION | NO |
| 45 | 5137-1 / v5.137.1 | 2026-07-14 | stop-hook template pre-authorized dropping maintenance under length pressure | OTHER: prompt wording | NO |
| 46 | 5139-1 / v5.139.0 | 2026-07-14 | insert_consolidation_log whitelisted its SET clause and silently dropped memify_pruned/cls_promoted | MISSING-INVARIANT | NO |
| 47 | 5129-1 / v5.129.0 | 2026-07-12 | restore() N+1 storm: _detect_isolated_entities called _get_adjacent per entity with default with_names=True (~5,345 round-trips) | LOGIC | NO |
| 48 | 5105-1 / v5.105.0 | 2026-07-04 | datetime.utcnow() removed in Python 3.14 | ENVIRONMENT/INSTALL | NO |
| 49 | 5102-1 / v5.102.0 | 2026-07-03 | recall side-effects fired 2 sequential SurrealDB round-trips per memory | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 50 | 5101-1 / v5.101.0 | 2026-07-03 | histogram top finite bucket 10000ms clamped recall p95 at 10s while real cold recalls reach ~75s | LOGIC | NO |
| 51 | 5097-1 / v5.097.0 | 2026-07-02 | fusion hydrated fused candidates with a per-id loop (N+1 against the DB) | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 52 | 5097-2 / v5.097.0 | 2026-07-02 | fusion's mem.pop('embedding') forced MMR to re-fetch every candidate embedding | IN-PROCESS-COUPLING | YES |
| 53 | 5095-1 / v5.095.0 | 2026-07-01 | phantom knobs: ~20 consumers read os.environ only while config.yaml/UI showed and wrote the same knobs | DUPLICATION | NO |
| 54 | 5095-2 / v5.095.0 | 2026-07-01 | TOOL_POOL_WORKERS default 8 on a --cpus 1 core -> daemon froze | ENVIRONMENT/INSTALL | NO |
| 55 | 5095-3 / v5.095.0 | 2026-07-01 | RECALL_HEAVY_CONCURRENCY must be strictly < pool workers or the rerank fan-out gate is a no-op; nothing enforced it | MISSING-INVARIANT | NO |
| 56 | 5095-4 / v5.095.0 | 2026-07-01 | offload_enabled() had been reverted to an env-only read, so config.yaml offload_tools:true was ignored | MISSING-INVARIANT | NO |
| 57 | 5094-1 / v5.094.0 | 2026-07-01 | hook recalls ran on the unbounded default executor -> a leaked recall cascaded into event-loop starvation | IN-PROCESS-COUPLING | YES |
| 58 | 5091-1 / v5.091.0 | 2026-06-30 | timeout budgets unreconciled: wait_for could cancel mid-rerank and leak an uncancellable backend worker | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 59 | 5090-1 / v5.090.0 | 2026-06-30 | /health did not 503 on a saturated worker pool -> a wedged pool read as healthy to the container healthcheck | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 60 | 5090-2 / v5.090.0 | 2026-06-30 | unguarded shared mutables under concurrent tool execution (_query_cache, breakers, _stale_count_cache, _enrichment_pipeline double-init) | IN-PROCESS-COUPLING | YES |
| 61 | 5089-1 / v5.089.0 | 2026-06-30 | config POST wrote the value into os.environ right after the yaml save -> env-locked the knob (409, un-editable) | LOGIC | NO |
| 62 | 5089-2 / v5.089.0 | 2026-06-30 | config tab blank on refresh: boot path called the tab renderer before the deferred module defined it | LOGIC | NO |
| 63 | 5089-3 / v5.089.0 | 2026-06-30 | View menu iterated only one of five floating overlays | LOGIC | NO |
| 64 | 5088-1 / v5.088.0 | 2026-06-29 | overlay-body pointer-events:none -> heat slider dead and drags rotated the 3D graph | LOGIC | NO |
| 65 | 5087-1 / v5.087.1 | 2026-06-28 | camera auto-zoom-fit fired at tick 80 but cooldownTicks capped at 60 -> blank canvas on load | LOGIC | NO |
| 66 | 5087-2 / v5.087.0 | 2026-06-28 | hiding an edge type set only linkVisibility; the d3 link force still bound the nodes | LOGIC | NO |
| 67 | 5087-3 / v5.087.0 | 2026-06-28 | every reload ran a full ~15s cold force layout from a spiral | LOGIC | NO |
| 68 | 5087-4 / v5.087.0 | 2026-06-28 | Semantic edge type dead (O(n^2) KNN) but still in legend + backend compute path | OTHER: dead capability | NO |
| 69 | 5086-1 / v5.086.0 | 2026-06-27 | force-graph render loop ran at 60fps even focused-idle | LOGIC | NO |
| 70 | 5086-2 / v5.086.0 | 2026-06-27 | exact-title matches dropped out of the WRRF top-5 and lit the wrong node | LOGIC | NO |
| 71 | 5086-3 / v5.086.0 | 2026-06-27 | legend carried a stale hardcoded 'Semantic' fallback alongside the dynamic role-grouped legend | DUPLICATION | NO |
| 72 | 5086-4 / v5.086.0 | 2026-06-27 | resolved_by edges were never produced — extractor/handler type mismatch between knowledge_graph and graph_api | IN-PROCESS-COUPLING | YES |
| 73 | 5086-5 / v5.086.0 | 2026-06-27 | adr_add rendered multi-line field values flush-left, so an embedded '## ' poisoned the ADR id scan | LOGIC | NO |
| 74 | 5084-1 / v5.084.0 | 2026-06-25 | stale_wiki_count query result set omitted source_file | LOGIC | NO |
| 75 | 5084-2 / v5.084.0 | 2026-06-25 | stop-hook ADR-capture schema edge cases in branch_hint resolution | LOGIC | NO |
| 76 | 5084-3 / v5.084.0 | 2026-06-25 | consolidation scan did SELECT * fan-out on large stores | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 77 | 5083-1 / v5.083.0 | 2026-06-24 | /health always returned 200 even when degraded -> container curl -f read a db/embed outage as healthy | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 78 | 5081-1 / v5.081.0 | 2026-06-23 | wiki_set_metadata reached only the single row _resolve_page_id_by_slug returned, not all rows of a slug | LOGIC | NO |
| 79 | 5080-1 / v5.080.0 | 2026-06-22 | fusing a single non-empty pool CE-reranked an already-ranked pool a second time (MRR 0.84 -> 0.74) | LOGIC | NO |
| 80 | 5080-2 / v5.080.0 | 2026-06-22 | fan-out recall path early-returned before heat/metamemory/SR bookkeeping the legacy path performed | DUPLICATION | NO |
| 81 | 5080-3 / v5.080.0 | 2026-06-22 | fan-out path did not mirror the legacy _is_episodic_query gate, so episodic queries blended wiki | DUPLICATION | NO |
| 82 | 5079-1 / v5.079.0 | 2026-06-21 | benchmarks/run_eval.py called retriever.recall() directly -> every `make eval` gate was vacuous | MISSING-INVARIANT | NO |
| 83 | 5073-1 / v5.073.0 | 2026-06-20 | uv served a stale 600s-cached PyPI /simple listing -> home-manager switch failed on a fresh version | ENVIRONMENT/INSTALL | NO |
| 84 | 5072-1 / v5.072.0 | 2026-06-19 | nightly consolidation hardcoded a local EmbeddingEngine; on a host with no [ml] extra encode() returned None -> every action-log memory stored with embedding=None | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 85 | 5072-2 / v5.072.0 | 2026-06-19 | atomic-vacuum side backend spawned with hardcoded root/root while the HTTP client sent env creds -> 401 | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 86 | 5072-3 / v5.072.0 | 2026-06-19 | host nightly flooded logs and hung ~10s at exit trying to reach the container OTLP collector | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 87 | 5072-4 / v5.072.0 | 2026-06-19 | YADGAR_CACHE_SNAPSHOT_DIR not isolated per test -> e2e gate flake | OTHER: test isolation | NO |
| 88 | 5070-1 / v5.070.1 | 2026-06-18 | nightly opened StorageEngine in embedded mode: surrealdb SDK 2.0.0 vs server 3.0.5 surrealkv format skew -> exit 30 every night | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 89 | 5069-1 / v5.069.0 | 2026-06-17 | nightly stopped only one unit and raced the canonical DB with the other still live -> exit 30 | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 90 | 5068-1 / v5.068.0 | 2026-06-17 | PROFILE_SEARCH_WEIGHT never defined in Settings; AttributeError swallowed by a bare except -> profiles never in recall | MISSING-INVARIANT | NO |
| 91 | 5067-1 / v5.067.0 | 2026-06-16 | nightly derived db_path from Settings.DB_PATH (stale legacy config.yaml value) instead of paths.DB_PATH | DUPLICATION | NO |
| 92 | 5067-2 / v5.067.0 | 2026-06-16 | _gc_callback touched module globals during interpreter shutdown after CPython tore them to None | LOGIC | NO |
| 93 | 5067-3 / v5.067.0 | 2026-06-16 | reembed_all passed None-content rows to encode_batch -> the whole batch failed and returned all-None | LOGIC | NO |
| 94 | 5066-1 / v5.066.0 | 2026-06-16 | four prune passes used access_count!=0 as an immortality guard -> any once-recalled derived memory was never purged | DUPLICATION | NO |
| 95 | 5065-1 / v5.065.0 | 2026-06-16 | recall()/wiki_query() silently enabled legacy all-pass mode when directory was omitted -> cross-project leak | MISSING-INVARIANT | NO |
| 96 | 5065-2 / v5.065.0 | 2026-06-16 | hook_prompt_recall extracted ?directory= but used it only for throttle keys, never for scoping | DUPLICATION | NO |
| 97 | 5065-3 / v5.065.0 | 2026-06-16 | recall's wiki-blend branch bypassed is_directory_eligible() while the memory branch applied it | DUPLICATION | NO |
| 98 | 5065-4 / v5.065.0 | 2026-06-16 | _fts_search supplement query used directory_context != $dir -> injected every OTHER project's memories | LOGIC | NO |
| 99 | 5065-5 / v5.065.0 | 2026-06-16 | project_brief _build_wiki_pages called list_wiki_pages with no directory arg -> cross-project wiki leak | DUPLICATION | NO |
| 100 | 5065-6 / v5.065.0 | 2026-06-16 | 'system' was an always-eligible sentinel and became the mis-stamp sink surfacing junk everywhere | LOGIC | NO |
| 101 | 5064-1 / v5.064.0 | 2026-06-16 | three write sites hardcoded directory_context='system' (strengthen, cls promotion, dream) | DUPLICATION | NO |
| 102 | 5063-1 / v5.063.0 | 2026-06-15 | nightly failed EVERY run in embedded mode: batch_writes raised 'server mode only' and type::record int ids were rejected by the embedded SDK | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 103 | 5063-2 / v5.063.0 | 2026-06-15 | the E2E test monkeypatched the failing production primitive, hiding the bug behind a false green | MISSING-INVARIANT | NO |
| 104 | 5062-1 / v5.062.0 | 2026-06-15 | directory= was a no-op in recall; measured 37.5% cross-project noise | MISSING-INVARIANT | NO |
| 105 | 5059-1 / v5.059.0 | 2026-06-15 | heat decay persisted no watermark -> every cycle re-multiplied the full elapsed span (quadratic over-decay) | LOGIC | NO |
| 106 | 5059-2 / v5.059.0 | 2026-06-15 | recall heat-boost loop raised KeyError on synthetic profile/belief dicts injected into the shared result list by the rerank merge | IN-PROCESS-COUPLING | YES |
| 107 | 5057-1 / v5.057.2 | 2026-06-14 | ci-release change detection skipped a needed backend build -> phantom 404 image tag | ENVIRONMENT/INSTALL | NO |
| 108 | 5057-2 / v5.057.1 | 2026-06-14 | generate_sbom.sh invoked cyclonedx-bom; the package installs its entry point as cyclonedx-py | ENVIRONMENT/INSTALL | NO |
| 109 | 5057-3 / v5.057.1 | 2026-06-14 | tag-and-release listed build-sbom in needs -> an SBOM failure blocked the git tag and release | OTHER: CI job coupling | NO |
| 110 | 5057-4 / v5.057.0 | 2026-06-14 | check_complexity.py enforced over tests/ and scripts/ while I30's allowlist was production-only | DUPLICATION | NO |
| 111 | 5056-1 / v5.056.0 | 2026-06-13 | stale MCP server module state leaked across xdist workers | OTHER: test isolation | NO |
| 112 | 5056-2 / v5.056.0 | 2026-06-13 | SurrealDB HTTP-fallback wipe nuked module-scoped corpora shared across the session | OTHER: test isolation | NO |
| 113 | 5056-3 / v5.056.0 | 2026-06-13 | pytest timeout used the thread method, which cannot interrupt blocking C extensions | ENVIRONMENT/INSTALL | NO |
| 114 | 5056-4 / v5.056.0 | 2026-06-13 | retrieval/core.py FTSParams caller argument order broken by the yellow-batch refactor | IN-PROCESS-COUPLING | YES |
| 115 | 5545-01 / v5.054.5 | 2026-06-13 | gp_weight read without float coercion -> TypeError when settings is a MagicMock | OTHER: test-double fragility | NO |
| 116 | 5545-02 / v5.054.5 | 2026-06-13 | BACKEND_VERSION in __init__.py drifted from server.json | DUPLICATION | NO |
| 117 | 5545-03 / v5.054.5 | 2026-06-13 | run_install had 16 positional params, over the hard cap | OTHER: lint/complexity | NO |
| 118 | 5545-04 / v5.054.5 | 2026-06-13 | OTLP timeout test pinned the pre-fix value of 10 after config default moved to 3 | DUPLICATION | NO |
| 119 | 5545-05 / v5.054.5 | 2026-06-13 | KNOWN_MEMORY_FIELDS did not list graph_prior/cofire_prior added by v5.54.1/.2 | DUPLICATION | NO |
| 120 | 5545-06 / v5.054.5 | 2026-06-13 | stop-hook tests used tmp_path/.local/state while the conftest redirects XDG_STATE_HOME elsewhere | OTHER: test path | NO |
| 121 | 5545-07 / v5.054.5 | 2026-06-13 | viz smoke asserted #stats-btn after the 5.50.x tab rework removed the button | DUPLICATION | NO |
| 122 | 5545-08 / v5.054.5 | 2026-06-13 | publish-pypi job gate expectation stale vs PD-45 dev mode | OTHER: CI policy | NO |
| 123 | 5545-09 / v5.054.5 | 2026-06-13 | monkeypatch.addfinalizer is not a real MonkeyPatch method -> stale lru_cache across xdist workers | OTHER: test API misuse | NO |
| 124 | 5545-10 / v5.054.5 | 2026-06-13 | test patched _st in memorize.py after the refactor moved it into _memorize_phases | IN-PROCESS-COUPLING | NO |
| 125 | 5545-11 / v5.054.5 | 2026-06-13 | autouse isolate_yadgar_paths set YADGAR_LOG_DIR, installing a file handler on every test | OTHER: test isolation | NO |
| 126 | 5545-12 / v5.054.5 | 2026-06-13 | uv absent from the CI container image -> 18 wheel_bundle + Validate failures | ENVIRONMENT/INSTALL | NO |
| 127 | 5545-13 / v5.054.5 | 2026-06-13 | viz polled a debug-gated /api/logs/poll and logged each 403 as a console error | LOGIC | NO |
| 128 | 5545-14 / v5.054.5 | 2026-06-13 | launchd test helper substituted ${VAR} while the templates use the @VAR@ sed convention | DUPLICATION | NO |
| 129 | 5545-15 / v5.054.5 | 2026-06-13 | vacuum-cleanup tests counted tmp_path.iterdir() including conftest-injected dirs | OTHER: test isolation | NO |
| 130 | 5545-16 / v5.054.5 | 2026-06-13 | yadgar-setup.sh _step_config_sync lacked the YADGAR_DIR variable the invariant test requires | OTHER: convention | NO |
| 131 | 5545-17 / v5.054.5 | 2026-06-13 | init_replay_lightweight called logging.disable(CRITICAL) — a process-global flag that silenced every downstream xdist test | IN-PROCESS-COUPLING | NO |
| 132 | 5545-18 / v5.054.5 | 2026-06-13 | control-API route mutates os.environ directly; YADGAR_VIZ_NODE_SIZE_3D leaked into an unrelated test | IN-PROCESS-COUPLING | NO |
| 133 | 5012-1 / v5.050.12 | 2026-06-12 | viz detail panel did not reset shared elements before branching -> WIKI header over MEMORY body | LOGIC | NO |
| 134 | 5012-2 / v5.050.12 | 2026-06-12 | backend SSE memory_added payload omitted the `type` field -> nodes rendered UNKNOWN | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 135 | 5012-3 / v5.050.12 | 2026-06-12 | frontend had no SSE handler for wiki_added/wiki_updated/wiki_deleted the backend emitted | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 136 | 5012-4 / v5.050.12 | 2026-06-12 | node.type compared with inconsistent casing/whitespace across branch selection, header, colour and mesh gate | LOGIC | NO |
| 137 | 5010-1 / v5.050.10 | 2026-06-11 | a dead OTLP collector made the final BatchSpanProcessor flush retry past the systemd stop timeout -> SIGKILL 137 on every restart | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 138 | 5009-1 / v5.050.9 | 2026-06-11 | tabs.js VALID_TABS gained `debug` but the inline _VALID sets in index.html (the live router) did not | DUPLICATION | NO |
| 139 | 5008-1 / v5.050.8 | 2026-06-11 | the debug drawer's switchTab() shadowed the main tab router in shared global scope | IN-PROCESS-COUPLING | NO |
| 140 | 5007-1 / v5.050.7 | 2026-06-11 | Info tab repo link pointed at a wrong repository URL | LOGIC | NO |
| 141 | 5006-1 / v5.050.6 | 2026-06-11 | wiki node mesh used transparent:true -> implicit depthWrite:false -> face-ordering artifacts (the earlier revert's root cause) | LOGIC | NO |
| 142 | 5005-1 / v5.050.5 | 2026-06-11 | version-history rail was nested inside the preview, so clicking a version removed the rail | LOGIC | NO |
| 143 | 5005-2 / v5.050.5 | 2026-06-11 | another bare #tab-* id-specificity trap (#tab-stats) — same class as v5.50.3 | DUPLICATION | NO |
| 144 | 5004-1 / v5.050.4 | 2026-06-11 | viz tab data-mappers used invented field names instead of the real API response shapes -> every value rendered as an em dash | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 145 | 5004-2 / v5.050.4 | 2026-06-11 | the Info tab called GET /api/info, a route that did not exist (404) | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 146 | 5004-3 / v5.050.4 | 2026-06-11 | a duplicate Stats system (toolbar modal) collided with the Stats nav tab and the floating overlays | DUPLICATION | NO |
| 147 | 5003-1 / v5.050.3 | 2026-06-11 | bare #id{display} selectors outranked .tab-pane{display:none} -> every tab pane rendered stacked | LOGIC | NO |
| 148 | 4622-1 / v5.046.22 | 2026-06-05 | yadgar.service.in used ${YADGAR_DB_USER} while daemon.py had already moved to the RW-first chain | DUPLICATION | NO |
| 149 | 4621-1 / v5.046.21 | 2026-06-05 | YADGAR_HOST=127.0.0.1 is unreachable through podman's port forward; the daemon must bind 0.0.0.0 inside the container | ENVIRONMENT/INSTALL | NO |
| 150 | 4621-2 / v5.046.21 | 2026-06-05 | the v5.46.20 wheel was built before its own fix commit landed -> shipped without it | ENVIRONMENT/INSTALL | NO |
| 151 | 4620-1 / v5.046.20 | 2026-06-05 | YADGAR_MCP_AUTH_TOKEN loaded from secrets.env via EnvironmentFile but never forwarded into the container -> RuntimeError on every daemon start | ENVIRONMENT/INSTALL | WOULD-HAVE-CAUSED-IT |
| 152 | 4620-2 / v5.046.20 | 2026-06-05 | :Z relabel insufficient on Rocky 9 with admin_home_t on /root/.yadgar | ENVIRONMENT/INSTALL | NO |
| 153 | 4620-3 / v5.046.20 | 2026-06-05 | _wait_for_daemon 30s default too short for embed-model load + schema migration on cold start | ENVIRONMENT/INSTALL | WOULD-HAVE-CAUSED-IT |
| 154 | 4620-4 / v5.046.20 | 2026-06-05 | _step_pull_images pulled without stopping running containers -> upgrade left a stale container on the old image | ENVIRONMENT/INSTALL | NO |
| 155 | 4619-1 / v5.046.19 | 2026-06-05 | volume mounts lacked the :Z private-relabel flag -> container_file_t SELinux denial on RHEL-family hosts | ENVIRONMENT/INSTALL | NO |
| 156 | 4619-2 / v5.046.19 | 2026-06-05 | regenerated units were not restarted on a reinstall, so the new unit never took effect | ENVIRONMENT/INSTALL | NO |
| 157 | 4619-3 / v5.046.19 | 2026-06-05 | container first-run mkdir of the logs dir failed on SELinux-enforcing filesystems | ENVIRONMENT/INSTALL | NO |
| 158 | 4618-1 / v5.046.18 | 2026-06-05 | __version__ returned 'unknown' when the package was not installed | LOGIC | NO |
| 159 | 4618-2 / v5.046.18 | 2026-06-05 | setup.sh version detection relied on shim-shebang parsing because the CLI had no --version | ENVIRONMENT/INSTALL | NO |
| 160 | 4617-1 / v5.046.17 | 2026-06-05 | credential variable naming drifted across bootstrap_secrets.sh, daemon.py's unit template and the vacuum client (DB_ vs RW_ vs SURREAL_) | DUPLICATION | NO |
| 161 | 4616-1 / v5.046.16 | 2026-06-05 | 12 sites carried Python-2 `except X, Y:` syntax; embed_service.py:434 let Exception escape uncaught in the ML shutdown handler | LOGIC | NO |
| 162 | 4615-1 / v5.046.15 | 2026-06-05 | cli/seed.py still imported the pre-SurrealDB SQLite get_db path | LOGIC | NO |
| 163 | 4615-2 / v5.046.15 | 2026-06-05 | setup.sh seeded anchors before the daemon was reachable | ENVIRONMENT/INSTALL | WOULD-HAVE-CAUSED-IT |
| 164 | 4614-1 / v5.046.14 | 2026-06-05 | _locate_install_assets used bare `python3 -c` instead of the pipx venv interpreter | ENVIRONMENT/INSTALL | NO |
| 165 | 4613-1 / v5.046.13 | 2026-06-05 | _step_config_sync ran `config sync` without ever running `config init` when config.yaml was absent | ENVIRONMENT/INSTALL | NO |
| 166 | 4612-1 / v5.046.12 | 2026-06-05 | setup.sh and the Makefile tagged the backend image with the CORE version | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 167 | 4611-1 / v5.046.11 | 2026-06-05 | `python3 -m yadgar` and `python3 -c 'import yadgar'` resolved to system python on Rocky/Debian, not the pipx venv | ENVIRONMENT/INSTALL | NO |
| 168 | 4610-1 / v5.046.10 | 2026-06-05 | the wheel shipped only yadgar-setup.sh, not the nine helper scripts and unit templates it calls | ENVIRONMENT/INSTALL | NO |
| 169 | 4610-2 / v5.046.10 | 2026-06-05 | a missing helper fell through silently to an unhelpful error instead of failing fast | MISSING-INVARIANT | NO |
| 170 | 4609-1 / v5.046.9 | 2026-06-05 | CI-level YADGAR_CI_BRANCH leaked into tests that mock detect_branch->None to assert rejection | OTHER: test isolation | NO |
| 171 | 4609-2 / v5.046.9 | 2026-06-05 | _fake_memorize lacked branch_hint, so production's call raised TypeError, was swallowed, and stored=0 | DUPLICATION | NO |
| 172 | 4609-3 / v5.046.9 | 2026-06-05 | Dockerfile.ci lacked bsdmainutils -> `make help` failed on a missing `column` binary | ENVIRONMENT/INSTALL | NO |
| 173 | 4608-1 / v5.046.8 | 2026-06-05 | tag pushes fired the full CI/build/release matrix unintentionally | ENVIRONMENT/INSTALL | NO |
| 174 | 4607-1 / v5.046.7 | 2026-06-06 | YADGAR_CI_BRANCH was added to the workflows in v5.46.3 but the daemon never consumed it -> all four write tools returned missing_branch on every CI run | DUPLICATION | NO |
| 175 | 4607-2 / v5.046.7 | 2026-06-06 | tests hardcoded /home/max/git/yadgar paths | OTHER: test portability | NO |
| 176 | 4607-3 / v5.046.7 | 2026-06-06 | admin_dbsize's db_path.exists() guard was unreachable in tests, hiding _walk_db_sizes | OTHER: test seam | NO |
| 177 | 4607-4 / v5.046.7 | 2026-06-06 | Makefile pre-setup ran container-runtime detection in CI runners with no podman/docker | ENVIRONMENT/INSTALL | NO |
| 178 | 4607-5 / v5.046.7 | 2026-06-06 | test_transport session-count test raced the Starlette ASGI lifespan | OTHER: test race | NO |
| 179 | 4607-6 / v5.046.7 | 2026-06-06 | test_export_duckdb hit a SurrealDB unique-index violation on repeated runs | OTHER: test isolation | NO |
| 180 | 4607-7 / v5.046.7 | 2026-06-06 | viz_daemon_health test patched the wrong get_settings, so the LRU cache refilled between clear and run | IN-PROCESS-COUPLING | NO |
| 181 | 4606-1 / v5.046.6 | 2026-06-05 | RemoteMLClient._CircuitBreaker used the real monotonic clock instead of the injected time_fn | LOGIC | NO |
| 182 | 4606-2 / v5.046.6 | 2026-06-05 | spy patched the source module rather than the bound name imported in __init__ | OTHER: test binding | NO |
| 183 | 4606-3 / v5.046.6 | 2026-06-05 | surrealdb missing from the test extra -> StorageEngine import error blocked five test modules | ENVIRONMENT/INSTALL | NO |
| 184 | 4606-4 / v5.046.6 | 2026-06-05 | SurrealDB 2.x embedded does not round-trip directory_context='' in equality comparisons | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 185 | 4606-5 / v5.046.6 | 2026-06-05 | migration_016 ASSERT rejected wiki_page INSERTs that omit directory_context | LOGIC | NO |
| 186 | 4605-1 / v5.046.5 | 2026-06-05 | hook_db_lockdown_check was migrated to a standalone script in v5.20.0 but a test still imported the removed function | DUPLICATION | NO |
| 187 | 4605-2 / v5.046.5 | 2026-06-05 | consolidate_now sleep_cycle key is emitted only by mode='full'; the test called the default light mode | OTHER: test expectation | NO |
| 188 | 4604-1 / v5.046.4 | 2026-06-05 | wiki_page INSERT fixtures omitted directory_context that the schema now requires | OTHER: test fixture | NO |
| 189 | 4604-2 / v5.046.4 | 2026-06-05 | export fixtures used embedding_dim=4 against a 384-dim contract | OTHER: test fixture | NO |
| 190 | 4604-3 / v5.046.4 | 2026-06-05 | project_brief emitted roadmap_update_lag_hours=-1.0 sentinel instead of omitting the key | LOGIC | NO |
| 191 | 4604-4 / v5.046.4 | 2026-06-05 | test_harness_hardening hardcoded the repo path | OTHER: test portability | NO |
| 192 | 4604-5 / v5.046.4 | 2026-06-05 | migration test asserted _MIGRATIONS[-1] identity instead of membership | OTHER: test brittleness | NO |
| 193 | 4604-6 / v5.046.4 | 2026-06-05 | file_queue DLQ test payloads omitted branch/directory and never reached the retry mechanics under test | OTHER: test fixture | NO |
| 194 | 4603-1 / v5.046.3 | 2026-06-05 | build-sbom installed from PyPI rather than the local wheel, so the SBOM did not describe the release artifact | ENVIRONMENT/INSTALL | NO |
| 195 | 4602-1 / v5.046.2 | 2026-06-05 | detect_runtime.sh printed a stale 'Run: yadgar install' message and offered no OS-aware hints | LOGIC | NO |
| 196 | 4600-1 / v5.046.0 | 2026-06-05 | pyproject license classifier said MIT while LICENSE is Apache-2.0 | DUPLICATION | NO |
| 197 | 4430-1 / v5.043.0 | 2026-06-04 | wiki.add() omitted branch from its returned page dict, so wiki_approve lost branch propagation | LOGIC | NO |
| 198 | 4426-1 / v5.042.6 | 2026-06-04 | migration 016 'WHERE directory_context IS NONE' missed field-absent rows and type::record() failed silently on ints | LOGIC | NO |
| 199 | 4425-1 / v5.042.5 | 2026-06-03 | _resolve_page_id_by_slug resolved from the daemon's own CWD — meaningless inside the container | CROSS-PROCESS-CONTRACT | WOULD-HAVE-CAUSED-IT |
| 200 | 4425-2 / v5.042.5 | 2026-06-03 | agent_prompt_save bypassed the _wiki.add() machinery and stored no directory_context | DUPLICATION | NO |
| 201 | 4425-3 / v5.042.5 | 2026-06-03 | block tools accepted scope='project' with no directory instead of erroring | MISSING-INVARIANT | NO |
| 202 | 4424-1 / v5.042.4 | 2026-06-03 | six call sites defaulted the branch to the literal 'master' instead of None (the canonical slot) | DUPLICATION | NO |
| 203 | 4422-1 / v5.042.2 | 2026-06-03 | writer asymmetry: the queue drainer wrote branch='master' while wiki_check_duplicate searched {None} — four prior fixes had targeted the wrong layer | DUPLICATION | NO |
