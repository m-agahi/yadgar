# TDD Hardening Pipeline

- **Status:** ACCEPTED — codified v5.150.0 (#213)
- **Date:** 2026-07-18
- **Theme:** dev-process / quality

## Why

LLM agents write weak tests by default. Red→green is theater if tests are vacuous:
a test that errors (ImportError, SyntaxError) instead of asserting has not been shown
to test anything. A test that asserts a mock return you just set is not testing behavior.
A green badge produced by hollow tests gives false confidence and masks real regressions.

The hardening pipeline closes four gaps that standard TDD leaves open:

1. **Red-verify gap** — unverified red: agents claim "red" without checking failure reason.
2. **Critic gap** — no adversarial review of tests before implementation.
3. **Mutation gap** — tests pass on the implementation but miss equivalent-but-wrong mutations.
4. **Fuzz gap** — property violations invisible to example-based tests.

## The 5-Phase Pipeline

Phases are **RISK-TIERED**: run all 5 for load-bearing app/lib logic; run phases 1+3+5 only
for trivial/mechanical diffs (config, infra `.nix/.yaml`, one-liners, version bumps, docs, codemods).

### Phase 1 — Tests First + RED-VERIFY

Write tests before implementation, one per acceptance criterion. Run them. Confirm they FAIL
**for the right reason**: an assertion failure or `NotImplementedError`, NOT `ImportError` /
`SyntaxError` / collection error. A test that ERRORS instead of failing has not been shown
to test anything. Fix until the failure is a genuine assertion miss.

### Phase 2 — ADVERSARIAL TEST-CRITIC (load-bearing only)

Review the tests adversarially (assume they are weak) against this rubric:

- Real assertions — not `assertTrue(True)`, not tautological, not asserting a mock return you just set.
- Tests BEHAVIOR/contract, not implementation detail.
- Every acceptance criterion AND every non-trivial branch has a test.
- Edge + error cases present (empty, boundary, malformed, exception paths).
- No over-mocking that stubs out the unit under test.

Fix gaps. Loop **MAX 2 rounds**; still weak → STOP + report (do not spin).

### Phase 3 — Implement → Green

Minimal code to pass. Red → green.

### Phase 4 — Post-Green Harden (load-bearing only)

**Mutation testing (mutmut):**
- Scope: CHANGED module(s) ONLY.
- Time-box: ~10 minutes (`timeout`-wrapped).
- Exhaustive per module (NOT sampled) — mutation is exhaustive, not statistical.
- A surviving mutant = a bug your tests miss → add a killing test.
- Loop **MAX 2**; a genuinely-equivalent mutant may be allowlisted with a one-line justification comment.

**Property/fuzz (hypothesis):**
- For pure functions + parsers only.
- Invariant-driven: never-raises / idempotent / round-trip / conserves-invariant.
- `max_examples >= 200` (`>= 500` for critical paths).
- A fuzz failure → fix the code, PIN with `@example`, keep as regression test.

### Phase 5 — Gates + Commit

Run available checks (lint, types, complexity, observe-coverage, e2e). Fix root cause;
surface pre-existing failures separately. Same fix fails 2× → stop + report.
No `--no-verify` / hook bypass. No `Co-Authored-By`. Then commit.

## Risk Tier

| Diff type | Phases |
|-----------|--------|
| Load-bearing app/lib logic (non-trivial branching) | 1 → 2 → 3 → 4 → 5 (full) |
| Trivial/mechanical (config, infra, one-liner, doc, codemod) | 1 → 3 → 5 (skip 2+4) |

**Tier or it's waste.** Running mutation on a version-bump PR is theatre.

## Tool Pins

| Tool | Purpose |
|------|---------|
| `mutmut` | Mutation testing |
| `hypothesis` | Property/fuzz testing |
| `pytest` | Test runner |

Versions are not pinned at the pipeline level — pin in `pyproject.toml` / lockfile.

## Iteration Caps

Every fix-loop is bounded to prevent infinite spin:

| Loop | Cap | On breach |
|------|-----|-----------|
| Adversarial critic | 2 rounds | STOP + report |
| Mutation killing | 2 rounds | STOP + report |
| Fuzz fixing | 2 rounds | STOP + report |
| Phase 5 check fixes | 2 attempts per issue | STOP + report |

## Sample Budgets

| Scope | Mutation box | Fuzz max_examples |
|-------|-------------|-------------------|
| Small module (<500 loc) | ~2–4 min | 200 |
| Medium module (500–2000 loc) | ~5–8 min | 200 |
| Critical path / parser | ~10 min (full box) | 500 |

Mutation is **exhaustive per module, not sampled** — the time-box is the scope control.
Narrow the target dir, not the mutant set.

## Brutal Honesty Caveats

- **Mutation is slow.** Exhaustive on a large module will hit the time-box. Keep scope tight
  (changed modules only). Do not try to mutation-test the whole repo in CI — it won't fit.
- **Fuzz needs real invariants.** A fuzz test with a weak invariant (e.g. "doesn't raise") on
  a function that can't raise is a no-op. Write invariants that could actually be violated.
- **Tier or it's waste.** Phases 2+4 on a one-line config change add cost with zero signal.
  The TIER RULE is not optional.
- **Bounded loops or it never terminates.** Without caps, an agent in a "fix the surviving
  mutant" loop can spin indefinitely if the mutation is equivalent. Two rounds, then escalate.

## Operational Form

Codified into the `implement-tdd` agent-prompt pattern (live wiki + genesis seed). The weekly
mutation-sweep workflow (`.forgejo/workflows/mutation-sweep.yaml`) runs a scheduled health signal
over a core module subset — non-blocking, reports surviving mutants for triage.
