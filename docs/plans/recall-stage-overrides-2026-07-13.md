# recall `stage_overrides` — Wire-Through Fix

**Status:** DRAFT — awaiting audit
**Issue:** #58 (found during #19 dead-code sweep)
**Author:** subagent / 2026-07-13
**Affects:** core 5.116.x + backend (same release train)

---

## BLUF

The public MCP `recall()` tool accepts and documents a `stage_overrides` parameter,
but the parameter is silently dropped at every layer of the forwarding chain. External
callers who pass it get no error and no effect. The backend infrastructure to consume
it already exists; the fix is to wire the parameter through three missing seams.
Verdict: **wire it through** (not remove) — infrastructure is present, contract is already
published in the public docstring.

---

## Bug Mechanism (verified file:line)

### Layer 1 — Core MCP tool

**File:** `yadgar/core/server/tools/recall.py`

| Location | What | Line |
|---|---|---|
| `recall()` signature | `stage_overrides: dict[str, dict] \| None = None` accepted | 134 |
| `recall()` docstring | `stage_overrides: Per-call stage disable map, e.g. {"nli": {"enabled": False}}` documented | 162 |
| `_forward_to_backend()` signature | **No `stage_overrides` param** | 45–58 |
| `_forward_to_backend()` call in `recall()` | **`stage_overrides` absent from kwargs** | 254–265 |
| HTTP payload dict | **`stage_overrides` absent from payload** | 102–113 |

`recall()` receives the value, validates nothing (no early-reject guard), then calls
`_forward_to_backend()` without it. Silent no-op. The existing test file
(`tests/server/test_mcp_recall_pipeline_kwargs.py` line 8-9) even acknowledges
this: _"stage_overrides is NOT forwarded (it's a call-level override not in
RecallRequest). Actually: stage_overrides is in RecallRequest — if we forward it
later that's OK."_ That "if we forward it later" never happened.

### Layer 2 — Backend route

**File:** `yadgar/backend/embed_service/embed_service.py`

| Location | What | Line |
|---|---|---|
| `RecallRequest` model | `stage_overrides: dict \| None = None` present | 1219 |
| `recall_route` → `_fanout_recall()` call | **`req.stage_overrides` not passed** | 1352–1363 |
| `_fanout_recall()` signature | **No `stage_overrides` param** | 459–470 |

`RecallRequest` already has the field and the Pydantic model deserialises it
correctly. But `recall_route` builds the `_fanout_recall()` call without
threading `req.stage_overrides` through, so even if the core fixed its side the
backend would silently ignore it.

### Layer 3 — Backend pipeline (working correctly)

**Files:** `yadgar/backend/retrieval/core.py`, `yadgar/backend/retrieval/pipeline.py`,
`yadgar/backend/retrieval/state.py`

`recall_via_pipeline()` (`core.py:391`) accepts `stage_overrides`, passes it to
`RetrievalState(stage_overrides=stage_overrides or {})` (`core.py:420`).
`Pipeline.run()` reads `state.stage_overrides` at `pipeline.py:138` and passes
`{"stage_overrides": overrides}` to `stage.is_enabled()` at `pipeline.py:167`.
Stage-disable logic is fully implemented and tested. **This layer needs no changes.**

---

## Does the Backend Consume `stage_overrides`?

Yes — fully. The consumption path is:

```
_fanout_recall(stage_overrides=…)          ← MISSING param today
  → Retriever.recall_via_pipeline(stage_overrides=…)
    → RetrievalState(stage_overrides=…)
      → pipeline.run(state)
        → stage.is_enabled(…, {"stage_overrides": overrides})
```

The only missing piece is `_fanout_recall()` accepting the param and threading
it into `recall_via_pipeline()`. Layer 3 is complete; only Layers 1 and 2 need
fixes.

---

## Chosen Fix: Wire It Through

**Rationale:** Removing the param would be a breaking API change — it's
published in the tool docstring and in the system-prompt injection that external
callers see. The backend infrastructure is fully built. Wiring costs ~15 lines
across two files and closes the contract.

Removal would be appropriate only if the backend had no plumbing for
`stage_overrides`. It does. Wire it through.

### Changes required

#### Seam A — `_forward_to_backend()` in `yadgar/core/server/tools/recall.py`

1. Add `stage_overrides: dict[str, dict] | None = None` to the function signature
   (after `profile`, before `timeout_s`).
2. Add `"stage_overrides": stage_overrides` to the `payload` dict (lines 102–113).
3. Pass `stage_overrides=stage_overrides` in the `_forward_to_backend()` call
   from `recall()` (lines 254–265).

The `@observe` decorator comment (`# noqa: PLR0913 — 12 args match full recall
signature`) needs updating to 13 args.

#### Seam B — `_fanout_recall()` in `yadgar/backend/retrieval/recall_pipeline.py`

4. Add `stage_overrides: dict | None = None` to the `_fanout_recall()` signature
   (append after `deadline`; update the `# noqa: PLR0913` comment to the new
   arg count).
5. Thread it into the `Retriever.recall_via_pipeline()` call inside
   `_fanout_recall()`. Confirm the call site exists and wire
   `stage_overrides=stage_overrides or {}`.

#### Seam C — `recall_route` in `yadgar/backend/embed_service/embed_service.py`

6. Pass `stage_overrides=req.stage_overrides` in the `_fanout_recall()` call
   at lines 1352–1363 (the fanout branch; landscape branch ignores it — correct,
   no pipeline stages in that path).

No schema changes: `RecallRequest.stage_overrides` already exists (line 1219)
and the field validator allows `None`.

Note: `_fanout_recall` internally calls `MemoryProvider` → `Retriever`. Verify
that `Retriever.recall_via_pipeline` is the actual call site being used
(not the monolithic `recall()` fallback) so stage_overrides reaches the pipeline.
This is a **pre-implementation verification step**.

---

## Acceptance Criteria

| # | Criterion | How to verify |
|---|---|---|
| AC-1 | `recall(stage_overrides={"nli": {"enabled": False}}, ...)` reaches `_forward_to_backend` with `stage_overrides` in kwargs | Unit test: spy on `_forward_to_backend`, assert captured kwarg |
| AC-2 | `stage_overrides` appears in the HTTP payload sent to `/recall` | Unit test: assert captured kwargs match payload dict |
| AC-3 | `RecallRequest` deserialises `stage_overrides` correctly (already true; regression guard) | Existing model test or new parametrised test |
| AC-4 | `_fanout_recall(stage_overrides={"fts": False}, ...)` threads the value to `recall_via_pipeline` | Unit test: mock `recall_via_pipeline`, assert stage_overrides kwarg |
| AC-5 | `recall_route` passes `req.stage_overrides` to `_fanout_recall` | Integration test: POST /recall with stage_overrides, assert pipeline receives it |
| AC-6 | `recall(stage_overrides=None, ...)` (default) is unchanged — no regression | Existing test suite passes without modification |
| AC-7 | Stage actually fires/skips correctly end-to-end | Optional: extend existing `test_retrieval_pipeline.py` stage-disable tests with a fanout path |

---

## Test Plan

### Unit tests (new, in `tests/server/test_mcp_recall_pipeline_kwargs.py`)

**Test class: `TestStageOverridesForwarded`**

```python
def test_stage_overrides_forwarded_to_backend():
    """stage_overrides passed to recall() must appear in _forward_to_backend kwargs."""
    overrides = {"nli": {"enabled": False}}
    _, captured = _call_recall(query="test", stage_overrides=overrides)
    assert captured.get("stage_overrides") == overrides

def test_stage_overrides_none_forwarded_as_none():
    """stage_overrides=None (default) must forward as None, not omitted."""
    _, captured = _call_recall(query="test")
    assert "stage_overrides" in captured
    assert captured["stage_overrides"] is None

def test_stage_overrides_in_payload():
    """stage_overrides must be included in the HTTP payload dict."""
    # Mock httpx.post directly and inspect json= kwarg
    overrides = {"ce": {"enabled": False}}
    # ... assert payload["stage_overrides"] == overrides
```

The `_call_recall` helper in the existing test file already accepts
`stage_overrides` as a parameter (line 37, 65-66) — wire it through to the spy
instead of just passing it to `recall_fn`.

### Unit test (new, in `tests/_shared/test_retrieval_pipeline.py` or
`tests/server/test_recall_fanout.py`)

**Test: `_fanout_recall` threads `stage_overrides` to `recall_via_pipeline`**

```python
def test_fanout_recall_threads_stage_overrides():
    """_fanout_recall must pass stage_overrides to recall_via_pipeline."""
    overrides = {"nli": {"enabled": False}}
    with patch.object(retriever_instance, "recall_via_pipeline") as mock_rvp:
        mock_rvp.return_value = []
        _fanout_recall(query="q", ..., stage_overrides=overrides)
    mock_rvp.assert_called_once()
    _, kwargs = mock_rvp.call_args
    assert kwargs.get("stage_overrides") == overrides
```

### Integration / route test (new or in `tests/backend/test_recall_route.py`)

**Test: route passes `req.stage_overrides` to `_fanout_recall`**

```python
def test_route_passes_stage_overrides():
    overrides = {"fts": {"enabled": False}}
    with patch("...._fanout_recall") as mock_fanout:
        mock_fanout.return_value = []
        client.post("/recall", json={..., "stage_overrides": overrides})
    _, kwargs = mock_fanout.call_args
    assert kwargs.get("stage_overrides") == overrides
```

### Regression guard

Run the full existing test suite (particularly `test_mcp_recall_pipeline_kwargs.py`
and `test_retrieval_pipeline.py`) without modification. All must pass unchanged.

---

## Scope

| Area | Changed | Notes |
|---|---|---|
| `yadgar/core/server/tools/recall.py` | Yes | `_forward_to_backend` signature + payload + forward call |
| `yadgar/backend/retrieval/recall_pipeline.py` | Yes | `_fanout_recall` signature + thread to `recall_via_pipeline` |
| `yadgar/backend/embed_service/embed_service.py` | Yes | `recall_route` → `_fanout_recall` call site |
| `yadgar/backend/retrieval/core.py` | No | Already correct |
| `yadgar/backend/retrieval/pipeline.py` | No | Already correct |
| `yadgar/backend/retrieval/state.py` | No | Already correct |
| `RecallRequest` model | No | Field already present |
| MCP docstring | No | Already accurate |
| Tests (new) | Yes | 4–5 new test cases across 2–3 files |

---

## Version Impact

- **Core:** patch bump (e.g. 5.116.x → 5.117.0) — behavioural change at the
  forwarding layer; previously silently dropped, now forwarded.
- **Backend:** same release train — `_fanout_recall` signature change is
  additive (new keyword arg with default `None`); `recall_route` change is
  additive (passes existing field). Wire-compatible with older cores that omit
  `stage_overrides` from the payload (Pydantic field has `None` default).
- **Wire compatibility:** core 5.116.x sends `stage_overrides: null` in the
  HTTP payload. Older backends with `RecallRequest(extra="forbid")` at line 1228
  already accept the field (it's in the model). No multi-version breakage.
- **Changelog entry:** `fix(recall): forward stage_overrides through to backend
  pipeline (was silently dropped since Phase 2a; #58)`.

---

## Pre-Implementation Checklist

- [ ] Verify `_fanout_recall` → `Retriever.recall_via_pipeline` call site exists
      (not the monolithic `Retriever.recall()` fallback). If the monolithic path
      is used, `stage_overrides` must be threaded there instead (different signature).
- [ ] Confirm `noqa: PLR0913` count update on `_forward_to_backend` and
      `_fanout_recall` after adding the new param.
- [ ] Check no other callers of `_fanout_recall` pass positional args that would
      be displaced by a new parameter (use grep `_fanout_recall(`).
- [ ] Landscape branch in `recall_route` correctly omits `stage_overrides`
      (landscape uses `_run_landscape_backend`, no pipeline stages).

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
- **But `grep 'recall_via_pipeline('` finds 7 call sites, ALL in
  `tests/_shared/test_retrieval_pipeline.py` (lines 591, 627, 635, 646, 654, 666,
  669). ZERO production callers.** It is a test-only artifact.
- **`_fanout_recall()` — the actual live fanout — does NOT call
  `recall_via_pipeline` and does NOT call the monolithic `Retriever.recall()`.**
  Its body (`recall_pipeline.py:459+`) builds provider tasks via
  `_build_provider_tasks()` → `_gather_provider_candidates()` →
  `fuse_candidates()`. The `Pipeline.run()` / `RetrievalState` / `stage.is_enabled()`
  stage-disable machinery is **not on this path at all.**

Consequence: the plan's data-flow diagram (BLUF §"Does the Backend Consume
`stage_overrides`?" and the arrow chain `_fanout_recall → recall_via_pipeline →
RetrievalState → pipeline.run → stage.is_enabled`) is **fiction for the live
path**. Seam B's instruction ("thread it into the `Retriever.recall_via_pipeline()`
call inside `_fanout_recall`") describes a call site **that does not exist** —
`_fanout_recall` contains no such call. The plan's own pre-impl checklist item
("verify `_fanout_recall → recall_via_pipeline` call site exists… If the
monolithic path is used, thread there instead") resolves to: **neither exists;
the provider-fanout path is used, which has no stage-gating hook whatsoever.**

This is the single reason the verdict flips. "Wire it through" assumed the
consuming end was built and live. It is built but **dead**. Wiring 3 seams lands
`stage_overrides` in `_fanout_recall`'s signature with **nowhere to consume it**
unless the fanout/provider path grows its own stage-gating — which is a real
feature, not a ~15-line wire-through.

### Secondary defects

1. **Bug-table Layer 2 location is wrong.** The plan's bug table cites
   `_fanout_recall` at `embed_service.py:459-470`. It is actually at
   `recall_pipeline.py:459`. (Seam B's fix section names the right file — the
   plan is internally inconsistent.) Confirmed by the `recall_backend_bypass`
   test fixture (`tests/conftest.py:1299`), which imports `_fanout_recall` from
   `yadgar.backend.retrieval.recall_pipeline`.

2. **A second live recall entry point is unaddressed (scoping gap).** Per
   ADR-0046 (status: **open**, unshipped) and `recall.py:367-379`, recall under
   the **stdio** transport runs an **in-core** unified fanout (LocalMLClient,
   backend-less by design) — a second path that also drops `stage_overrides`.
   The `recall_backend_bypass` fixture signature (conftest.py:1299) likewise has
   no `stage_overrides` param, confirming the non-HTTP path drops it. Deployed
   transport is HTTP (anchor mem 531868), so this may be out of scope — but the
   plan claims blanket "end-to-end" and must instead **explicitly scope itself to
   the HTTP path** (and state stdio is out / dropped per ADR-0046), not imply full
   coverage.

3. **Test-path mismatch.** AC-4/AC-7 assume `stage_overrides` threads into
   `recall_via_pipeline`. Since that method is test-only and off the live path,
   an AC that asserts the value reaches `recall_via_pipeline` would prove
   **nothing about production**. Tests that pass by exercising the dead path are
   worse than no test — they green a non-fix.

### Wire-compat / additive claims — these HOLD

- `RecallRequest` already has the field with `None` default; `extra="forbid"`
  is not a blocker (field is in the model). ✅
- Appending a keyword arg with `None` default to `_fanout_recall` / `_forward_to_backend`
  is positional-safe: all call sites use keyword args (verified — `_fanout_recall`
  prod call at `embed_service.py:1352`, plus test harnesses, all kwarg-style). ✅

### Required rework before this can be AUDITED-ready

The verdict must be re-derived, not patched. Two honest options:

- **(Preferred) Re-scope to "consume it in the fanout path."** Decide what
  `stage_overrides` should DO on the provider-fanout path (which stages exist
  there? CE rerank / NLI / MP are applied where?), then wire the 3 forwarding
  seams **and** add the consumption hook in `_fanout_recall` /
  `fuse_candidates` / provider tasks. This is a feature, re-estimate accordingly.
  The stage-gating semantics on fanout differ from the retired pipeline's
  `stage.is_enabled()` — do not assume parity.
- **(Alternative) Remove the parameter.** If no one is consuming
  `stage_overrides` and the fanout path has no stage-gating concept, the
  docstring/signature advertise a contract the system cannot honor. Removing it
  (with a deprecation note) is defensible — the plan's own rationale ("removal
  appropriate only if backend had no plumbing") is satisfied, because the live
  path has none; the plumbing that exists is dead test-only code.

Either way, "wire 3 seams, ~15 lines, Layer 3 already works" is not the fix.

### User decisions needed

- **D1:** Consume-on-fanout (feature) vs. remove-the-param (honest deprecation)?
  The plan cannot proceed until this is chosen — they are opposite directions.
- **D2:** Is the stdio/in-core path in scope, or explicitly excluded (HTTP-only)?
- **D3:** Should the retired `recall_via_pipeline` + its 7 tests be deleted as
  dead code (relates to #19 dead-code sweep, which surfaced this issue), or kept?
