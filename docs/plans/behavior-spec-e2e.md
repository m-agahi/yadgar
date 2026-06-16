# PLAN — Behavior-spec e2e suite (catch silent rot)

Status: **PLANNED 2026-06-16.** Top-priority quality track. Motivated by repeated
"looked working, was broken for weeks" failures (6-week-dead consolidation,
nightly backup-path drift, recall wiki leak, reembed_all no-op) — all of which
passed unit CI green. ~1000 unit tests, **only ~27 e2e-ish** today.

theme: quality / test-architecture
priority: HIGHEST (above remaining cleanup plans — this is the safety net)

## Problem

Unit/component tests pass while the real system is broken, because nothing
**runs the real path end-to-end and asserts the designed behavior**. Every recent
production bug shares this: no test exercised the actual flow (real DB, real
nightly script, real recall pipeline) and checked the observable outcome.

## Core idea (user, 2026-06-16)

Drive e2e tests from the **architecture / how the system SHALL behave** — not from
the implementation. If a change breaks designed behavior, or the designed behavior
was never actually achieved, the test fails. **No bending tests to pass.**

## Design decisions

1. **Behavior contract first.** Extract a catalog of "the system SHALL …"
   statements per subsystem, derived from the arch docs (`docs/architecture.md`,
   `retrieval.md`, `memory-lifecycle.md`, `ARCHITECTURE_INVARIANTS.md`,
   `DECISIONS.md`). Writing this catalog itself surfaces "does it actually do
   this?" gaps. The catalog lives in `docs/BEHAVIOR_CONTRACT.md` and each e2e test
   references the contract id it covers.
2. **Real path, no mocking the unit under test.** Run the actual daemon / embedded
   SurrealDB / real nightly script / real recall pipeline against a seeded store.
   The v5.65 false-green (mocked "e2e") is the anti-pattern this forbids.
3. **Assert observable behavior, derived from the contract — not the impl.**
   e.g. "write→consolidate: a cold memory (heat<cold_threshold) becomes archived";
   "nightly cycle exits 0 AND produces a snapshot AND consolidation_log row";
   "recall(directory=A) returns 0 rows stamped directory=B"; "reembed_all
   re-embeds every missing-embedding row". Impl doesn't deliver → test FAILS.
4. **Anti-bending is a REVIEW RULE.** Each test states a SHALL-contract; weakening
   an assertion to go green is rejected. (Broken once this session — make explicit.)
5. **Runs LOCAL via `pre-push`, NOT in CI.** CI's embedded SurrealDB is too weak /
   flaky for real behavior. Local has the real `surreal` binary
   (`~/.local/bin/surreal`) + the `surreal_server` fixture (proven deterministic:
   39/39 ×3). `pre-push` (not `pre-commit`) because the suite takes minutes —
   fires once per push, not per commit. Gates code before it leaves the machine.
   - Caveat (accepted): pre-push is local-only → no server-side release gate. If
     pushing from another machine or hook env drifts, the gate is absent.
     Mitigated by No-Hook-Bypass discipline + deterministic suite. CI keeps the
     unit/component gate; e2e is the local pre-push gate.
   - Mechanism: a `make e2e` target + a pre-push hook that runs it with
     `PATH=~/.local/bin` + `YADGAR_CI_BRANCH`-equivalent + real surreal. Marked
     `@pytest.mark.e2e`; excluded from the CI pytest selection (`-m 'not e2e'`),
     included in the pre-push run (`-m e2e`).

## Phasing (critical paths first — these would've caught the 6-week bug)

**Phase 1 — the paths that already bit us:**
- Nightly cycle: backup → consolidate → vacuum → snapshot → backup. Assert exit 0,
  snapshot file created at the real data dir, consolidation_log row, no unhandled
  exception. (Would've caught: 6-week-dead consolidation, backup-path drift,
  GC-shutdown exit-30.)
- Consolidate → decay → archive → purge: seed memories, run the real cycle, assert
  cold→archived, old+stale derived→purged (the v5.66 recency rule), protected→spared.
- Recall scoping: seed mixed-directory corpus (real embeddings), assert
  recall(directory=A) excludes other-project memories AND wikis; drop-system holds.
- Backup / restore round-trip: snapshot → restore → data intact.
- reembed_all: seed missing-embedding rows → assert all re-embedded.

**Phase 2 — broader subsystem contracts:**
- write pipeline (surprise gate), checkpoint/restore (hippocampal replay),
  wiki write→read→scope, hooks (prompt-recall, subagent-stop, session-end capture)
  stamp correct directory, CLS promotion, sleep/dream, astrocyte, heat decay math.

**Phase 3 — coverage closure:**
- Drive remaining BEHAVIOR_CONTRACT items to ≥1 e2e each. Track contract→test
  coverage; a contract with no test is a gap (lint it).

## Feeds from the dead-code/unwired audit (2026-06-16)

The audit (see report) quantifies dead code, untested MCP tools, no-op
capabilities, scheduled paths with no test. Each "scheduled path with no test" +
"untested user-facing tool" becomes a Phase-1/2 e2e contract. Dead/unwired
functions → separate cleanup (remove or wire).

## Acceptance

- `docs/BEHAVIOR_CONTRACT.md` exists; every Phase-1 critical path has a SHALL entry.
- Phase-1 e2e tests exist, run real-path on local surreal, deterministic, grouped
  `@pytest.mark.e2e`, run by `make e2e` + pre-push hook, excluded from CI.
- Re-running the suite against the PRE-fix commits of the bugs this session
  (consolidation embedded, backup-path, recall wiki leak, reembed_all) → each
  goes RED. (Proves the suite would have caught them.)
- Contract→test coverage tracked; gaps linted.

## Related
- `[[db-audit-fix]]` — the audit feeding this.
- Anti-bending lesson: the v5.65 false-green + the v5.58 monkeypatch false-green.
- Code: `yadgar/tests/integration/`, `surreal_server` fixture, `Makefile` e2e target.
