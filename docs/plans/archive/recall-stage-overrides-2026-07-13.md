# recall `stage_overrides` — Remove Dead Public Param

**Status:** AUDITED-ready
**Issue:** #58 (found during #19 dead-code sweep)
**Author:** subagent / 2026-07-13 (reworked 2026-07-13 after audit)
**Affects:** core only (single MCP tool file)

---

## BLUF

The public MCP `recall()` tool accepts and documents a `stage_overrides`
parameter, but the value is silently dropped at the first forwarding hop and
never reaches anything that could consume it. The audit below proved the
"wire-it-through" premise false: the only code that consumes `stage_overrides`
(`recall_via_pipeline` → `RetrievalState` → `Pipeline.run` → `stage.is_enabled`)
is **dead, test-only production code** — the live provider-fanout path
(`_fanout_recall`) has no stage-gating hook at all. Building a real
consume-on-fanout feature is out of scope for a bug fix.

**Chosen fix (user decision D1 = remove): delete `stage_overrides` from the
public `recall()` MCP tool signature + docstring.** It was never wired and it
targets dead machinery. Honest deprecation over advertising a contract the
system cannot honor. Minimal, core-only.

---

## Scope of this plan

This plan removes exactly one thing: the `stage_overrides` parameter on the
public MCP `recall()` tool. It does **not** delete the dead `recall_via_pipeline`
consumption path or the now-unfed `RecallRequest.stage_overrides` backend field
— those belong to the #19 dead-code sweep (see cross-reference below).

---

## The fix (verified file:line, read on master)

**File:** `yadgar/core/server/tools/recall.py`

| # | Change | Location |
|---|---|---|
| 1 | Delete the `stage_overrides: dict[str, dict] \| None = None,` parameter from the `recall()` signature | `recall.py:134` |
| 2 | Delete the `stage_overrides:` line from the `recall()` docstring `Args:` block | `recall.py:162` |
| 3 | If the `@observe`/`noqa: PLR0913` arg-count comment on `recall()` names an explicit count, decrement it to match the reduced signature | near the `recall()` decorator |

That is the entire behavioural change. `recall()` never forwarded the value
(`_forward_to_backend()` at `recall.py:45-58` has no `stage_overrides` param;
the payload dict and the forward call omit it), so **removing the param changes
no runtime behaviour** — the value was already a no-op. This is a
schema-visible-but-behaviour-neutral MCP surface change: external callers who
passed `stage_overrides` already got no effect; after removal they get an
`unexpected keyword argument` error instead of silent nothing, which is the
honest signal.

### Test-helper cleanup (trivial, same PR)

`yadgar/tests/server/test_mcp_recall_pipeline_kwargs.py` has a `_call_recall`
helper (`:37`) that plumbs a `stage_overrides=` kwarg (`:65-66`) into the tool.
**No test case passes it non-None** (verified: grep finds only the def + the
guarded assignment, zero call-sites supply a value). Remove the dead
`stage_overrides` plumbing from the helper (params `:37`, the `if stage_overrides
is not None` block `:65-66`) and the stale module docstring note (`:5-10`) so the
helper matches the reduced signature. This is a test-hygiene edit, not a
behaviour test — every actual test case runs unchanged.

---

## Cross-reference — #19 dead-code sweep (do NOT duplicate deletions here)

The audit surfaced two now-orphaned artifacts. Both are #19's responsibility,
not this plan's:

- **`Retriever.recall_via_pipeline`** (`yadgar/backend/retrieval/core.py:383`) +
  its ~9 test call-sites — the dead consumption path. This is **#19 Car R**
  (`recall_via_pipeline` retirement, user-gated; NOT Car B — Car B is the 8 dead
  storage/update methods). The #19 plan already routes the `stage_overrides` MCP
  param OUT of its own sweep and over to this #58 plan; in return, this plan
  leaves the dead backend consumption path (and the now-unfed
  `RecallRequest.stage_overrides` field at `embed_service.py:1219`) to #19's Car R
  mop-up. Nothing is lost between the two plans.

Removing the tool param here does not depend on #19 shipping first — the param is
independently dead. If #19 Car R never lands, the backend field simply stays as
inert additive plumbing.

---

## Acceptance criteria [unit]

| # | Criterion | How to verify |
|---|---|---|
| AC-1 | `stage_overrides` is gone from the `recall()` tool signature | grep `stage_overrides` in `recall.py` returns 0 hits |
| AC-2 | `stage_overrides` is gone from the `recall()` docstring | same grep; docstring `Args:` no longer lists it |
| AC-3 | Existing recall tests pass unchanged (all real test cases) | run `test_mcp_recall_pipeline_kwargs.py` + recall suite; every case green (only the `_call_recall` helper plumbing is trimmed — no test-case assertion changes) |
| AC-4 | No external-caller contract left dangling | grep `stage_overrides` across `yadgar/` — remaining hits are only the dead `recall_via_pipeline`/`RetrievalState` path (#19 Car R) and the backend `RecallRequest` field; **zero live callers pass it into the tool** |

---

## Version impact

- **Core:** patch bump. Behaviour-neutral (the param was already dropped);
  the only change is the public tool surface + a test-helper trim.
- **Backend:** untouched by this plan. `RecallRequest.stage_overrides` stays as
  inert additive plumbing until #19 Car R removes it.
- **Changelog entry:** `fix(recall): remove never-wired stage_overrides param
  from the public recall() MCP tool — it targeted a dead test-only consumption
  path (#58; see #19 Car R for the backend/pipeline mop-up)`.

---

## AUDIT (2026-07-13)

**Status: NEEDS-REWORK.** The plan's central premise — "the backend consumes
`stage_overrides` end-to-end; only three forwarding seams are missing" — is
**false against observed state**. The consumption machinery the plan calls
"Layer 3, working correctly" is on a **dead, test-only path**. Wiring the three
named seams would forward the value into a function that never reaches it.

### Verified findings (file:line, read on master)

The four broken forwarding seams the plan identifies are **real and correctly
diagnosed**:

- `recall.py:134` — `recall()` accepts `stage_overrides`. ✅ (as claimed)
- `recall.py:45-58` — `_forward_to_backend()` signature has NO `stage_overrides`. ✅ broken (as claimed)
- `recall.py:102-113` — HTTP payload dict omits `stage_overrides`. ✅ broken (as claimed)
- `recall.py:254-265` — `recall()`→`_forward_to_backend()` call omits it. ✅ broken (as claimed)
- `embed_service.py:1219` — `RecallRequest.stage_overrides` field present; `extra="forbid"` at `:1228`. ✅ (as claimed — additive/wire-compat claim holds)
- `embed_service.py:1352-1363` — `recall_route` → `_fanout_recall` call omits `req.stage_overrides`. ✅ broken (as claimed)

### The load-bearing defect the plan missed

**`recall_via_pipeline()` is RETIRED from production. It is called ONLY from
tests.**

- `core.py:383` defines `recall_via_pipeline()`; `:391` accepts `stage_overrides`;
  `:420` passes it to `RetrievalState(stage_overrides=...)`. That part is real.
- **But `grep 'recall_via_pipeline('` finds all call sites in tests
  (`tests/_shared/test_retrieval_pipeline.py`, `tests/server/*`). ZERO production
  callers.** It is a test-only artifact.
- **`_fanout_recall()` — the actual live fanout — does NOT call
  `recall_via_pipeline` and does NOT call the monolithic `Retriever.recall()`.**
  Its body (`recall_pipeline.py:459+`) builds provider tasks via
  `_build_provider_tasks()` → `_gather_provider_candidates()` →
  `fuse_candidates()`. The `Pipeline.run()` / `RetrievalState` / `stage.is_enabled()`
  stage-disable machinery is **not on this path at all.**

Consequence: the plan's data-flow diagram (arrow chain `_fanout_recall →
recall_via_pipeline → RetrievalState → pipeline.run → stage.is_enabled`) is
**fiction for the live path**. Seam B's instruction describes a call site **that
does not exist**. Wiring 3 seams lands `stage_overrides` in `_fanout_recall`'s
signature with **nowhere to consume it** unless the fanout/provider path grows
its own stage-gating — which is a real feature, not a ~15-line wire-through.

### Secondary defects

1. **Bug-table Layer 2 location was wrong.** The plan's bug table cited
   `_fanout_recall` at `embed_service.py:459-470`. It is actually at
   `recall_pipeline.py:459`.

2. **A second live recall entry point (stdio/in-core, ADR-0046 open) also drops
   `stage_overrides`.** The plan claimed blanket "end-to-end" coverage it does
   not have.

3. **Test-path mismatch.** ACs that assert the value reaches `recall_via_pipeline`
   would prove **nothing about production** (dead path).

### Required rework — RESOLVED below

### REWORK (2026-07-13): applied — REMOVE-PARAM (user decision D1)

The verdict was re-derived, not patched. **User chose D1 = remove the param**
(honest deprecation), NOT the consume-on-fanout feature. The plan body above is
rewritten accordingly:

- Fix changed from "wire 3 seams" → **remove `stage_overrides` from the public
  `recall()` tool signature (`recall.py:134`) + docstring (`recall.py:162`)** —
  it was never wired and targets the dead `recall_via_pipeline` path.
- The dead `recall_via_pipeline` deletion + the now-unfed `RecallRequest`
  backend field are **cross-referenced to #19 Car R** (corrected from an earlier
  "Car B" reference — Car B is the 8 storage methods; Car R is the
  `recall_via_pipeline` retirement) — this plan does not duplicate those deletions.
- Acceptance is unit-level: param gone from signature + docstring; existing
  recall tests pass unchanged; no live external-caller contract dangling
  (grep-verified: zero test/prod call-sites pass `stage_overrides` into the tool).
- D2 (stdio/in-core scope) and D3 (`recall_via_pipeline` deletion) are both
  subsumed: removal makes the whole param moot on every transport, and the
  backend/pipeline dead-code deletion is #19 Car R's job.

Version: core-only patch. Scope: tiny. **Status → AUDITED-ready.**
