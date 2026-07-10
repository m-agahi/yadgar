# Plan: seed+safe-stop+stop-hook train — ONE PR (user 2026-07-10)

**Status:** AGREED (user: "put the seed improvement and the surreal rca fixes in one big pr" + stop-hook external file + "increase the budget as you see fit with the seed improvements"). ONE PR, one version claim (ADR-0088): core 5.123.0 + BACKEND_VERSION 5.35.0 (Car 2 touches entrypoint-backend.sh).
**Date:** 2026-07-10. **Branch:** `feat/seed-backflow-agent-prompts` (already in flight as Car 1) becomes the train branch.
**Plan lifecycle:** the car that finishes LAST does the archive-first commit for this file (ADR-0081/0082).

## Car 1 — seed backflow + prelude budget increase (agent in flight)

1. Backflow per ADR-0091: sync genesis (`core/seed/materials/agent_prompts.yaml`) with audited live-page improvements; promote battle-tested non-starter patterns into the seeded set (agent judging reusable-on-fresh-install vs project-specific).
2. **NEW SCOPE — prelude composition budget increase** (user: "increase the budget as you see fit"): the 2000-char cap drops ALL disciplines whenever the pattern is long (observed live on stacked-car-parallel-build — composition invisible at default budget). Raise: base 2000 → **3500**, with-context 4000 → **6000**. Grounds: contract ≈700 + 3 disciplines ≈1200 + typical pattern ≈1500 fits in 3500; dispatch prompts tolerate this easily (subagent context budgets are far larger; the original 2000 predates disciplines existing). Keep the overflow rule (drop disciplines last-listed-first + warn) as the safety valve. Update tests pinning the budget constants + add a fits-now regression: stacked-car pattern + its 3 disciplines must survive at the new base budget.

## Car 2 — surrealkv safe-stop fixes (input: RCA agent's docs/plans/surrealkv-safe-stop-2026-07-10.md, task #37/ADR-0090)

RCA LANDED (7b670d6a) — verdict: warning is UPSTREAM-UNCONDITIONAL (surrealkv `impl Drop for Tree` skips async close when `Handle::try_current()` fails; v3.1.5 SIGTERM tears the runtime down first; no fixed release exists — corruption class open as surrealdb#5001). Pin bump is OFF the table. Build:
1. **Option B (primary):** entrypoint-backend.sh SIGTERM ordering — stop writers first, signal surreal, WAIT for clean exit, write a torn-stop marker on abnormal exit (BACKEND_VERSION 5.35.0).
2. **Option D (belt-and-braces):** startup torn-manifest detection → auto-restore per the RCA runbook (pick restore source by newest INNER-file mtime + row counts, never dir name — dir mtime lies under os.rename).
3. **NEW from RCA — vacuum split-brain fix:** the 07-09 vacuum swap failed `check_invariants` (404) and RETAINED `.old` while the running backend kept writing to the ORIGINAL inode (= `.old`) for 16h — path/inode split-brain that made `surreal_db` a stale decoy. Fix: on invariants-fail, ROLL BACK the swap (restore original path), never retain a half-swapped state.
4. Tests: TDD at the entrypoint/script + vacuum-swap seams (subprocess harness per repo precedent); e2e stays green.

## Car 3 — stop-hook prompt → external template file (task #34)

Extract the embedded checkpoint-prompt text from `yadgar-stop-memory-checkpoint.py` (and its packaged twin under yadgar hooks) into a template file living in the SAME module/package, loaded at runtime (importlib.resources or path-relative — match how the agent-prompt schema file shipped in #180: file = law, packaged, tested). Behavior identical; path resolution robust in stdio + HTTP installs. Tests: template loads, rendered prompt byte-equal to current text (pin), missing-template fail-loud.

## Sequencing

Car 1 (running) → Car 2 (blocked on RCA plan) → Car 3 (independent, may build in parallel with Car 2 via stacked branch per [[agent-prompt-stacked-car-parallel-build]] if seams disjoint — Car 3 touches hooks/, Car 2 touches entrypoint/Dockerfile.backend: disjoint). ONE PR at end; user merges; nix bump core 5.123.0 + backend 5.35.0.

## Collisions

T2 layer-boundary train is PARKED until this PR merges (#37 is P0 before T2 — user order). No other branches in flight.

## References

ADR-0088 (one-PR trains), ADR-0090 (incident/mitigation mandate), ADR-0091 (seed backflow), task #34/#35-adjacent, #37, docs/plans/surrealkv-safe-stop-2026-07-10.md (Car 2 input, pending).
