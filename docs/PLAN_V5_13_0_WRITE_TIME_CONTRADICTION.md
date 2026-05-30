# PLAN v5.10.10 — Write-Time Contradiction Detection (Audit Adopt-2)

**Status:** DRAFT — pending TDD execution
**Version slot:** v5.10.10 (light wiring patch; v5.10.x already crowded — this slot is free)
**Audit reference:** `docs/competitor-audit-2026-05-30.md` Adopt item 2 ("Write-time conflict resolution")
**Audit-decisions entry to add:** under "Adopt items → 2. Write-time conflict resolution" — see end of this plan
**Related shipped code:**
- `yadgar/conflict_resolver.py` (v5.3.4, env-gated LLM resolver, default OFF — Ollama-only)
- `yadgar/curation/contradiction.py` (v5.3.x, lightweight non-LLM detector — **built but unwired**)
- `yadgar/curation/__init__.py:183` `MemoryCurator.detect_contradictions()` — public wrapper, never called
- `yadgar/server/tools/memorize.py:302-356` — LLM resolver wiring
- `docs/CONFLICT_RESOLVER.md` — full spec for LLM resolver

---

## 1. Decision: Option B — Wire The Existing Lightweight Detector (default ON)

Picked over A/C/D for reasons below.

### Why not A (default-on Ollama LLM resolver)
- Requires Ollama running locally. Most yadgar users (current author included) do not run it.
- Each call adds ~30s worst-case (timeout) to user-facing `memorize()` latency. Adds blocking I/O on the hot write path.
- Fail-soft contract degrades to `ADD` on error — so default-on with no Ollama is effectively default-off plus a noisy log line plus 30s latency penalty on first call.
- The LLM resolver's strength (semantic understanding) is real but not the audit's actual ask. The audit asked for *"lightweight write-time contradiction check (cosine similarity of incoming memory vs top-K results, mark conflicting as stale)"*. That is a non-LLM check.

### Why not C (improve discoverability of existing knob)
- The audit's gap claim ("stale-until-nightly") would persist. A documentation patch does not solve it.
- Discoverability of an opt-in feature that requires Ollama does not change the user reality.

### Why not D (REJECT — audit was wrong)
- The audit was not wrong. The LLM resolver exists but is default-off and Ollama-dependent. The lightweight detector exists but is unwired. Both gaps are real. Item 2's specific wording maps directly onto the unwired lightweight detector.

### Why B (the chosen option)
- `yadgar/curation/contradiction.py` already implements exactly the audit's described mechanism: top-K by cosine similarity (≥0.7) + negation-pattern heuristic + action-divergence heuristic + confidence decrement on the contradicting old memory.
- `MemoryCurator.detect_contradictions()` already wraps it.
- **The gap is one wire**: `curate_on_remember()` needs to call `self.detect_contradictions()` before the merge/link/create branch.
- Default ON is safe: no network I/O, no LLM, fast (O(K) where K is similar-memory count, already computed by the curator).
- LLM resolver (Option A) stays available as opt-in upgrade for users who want higher-fidelity semantic detection — both can coexist (LLM resolver short-circuits *before* curator in `memorize.py:306`; new wiring is downstream and orthogonal).

---

## 2. Verified Discriminators (before committing to B)

1. **Curator path is the universal write path?** Yes. `memorize.py:359-405` — curator runs whenever `curator is not None and embedding is not None`. The fallback (`else` at line 406) only fires when curator is missing or embedding failed. The fallback is the rare edge case; wiring the detector inside the curator covers the practical 100%.
2. **LLM resolver and lightweight detector orthogonal?** Yes. LLM resolver at `memorize.py:302-356` short-circuits with `return` on NOOP/UPDATE/DELETE. If it falls through (op=ADD, or disabled, or errored), execution reaches the curator. Inside the curator, the lightweight detector runs as one more guard. Both can be on simultaneously without conflict — the LLM call (if enabled) makes the higher-fidelity decision first; the lightweight check catches what the LLM missed or what the LLM never saw (LLM disabled case).

---

## 3. Open Question (decide before implementing)

`detect_contradictions()` mutates the contradicting old memory's `confidence` field as a side effect (`yadgar/curation/contradiction.py:61, 80`). This is *silent mutation of an unrelated row at write time*. When the call was only invoked from nightly consolidation (which it never was in practice, but was the design intent), this side effect was acceptable — nightly is the "everything gets reorganized" phase. At write time, this changes the contract: writing memory X can lower confidence on memory Y without any tool-level signal.

**Two sub-options:**
- **B1 (recommended):** Keep the existing side effect. Document it as a tradeoff. The whole point of write-time detection is to act on contradictions immediately; silent confidence decay matches the spirit of the audit ("mark conflicting as stale"). Add a metric (`yadgar_contradiction_confidence_decay_total{reason="negation_mismatch"|"action_divergence"}`) so the side effect is observable.
- **B2:** Split detection from mutation. Add a `mutate=True` kwarg to `detect_contradictions`, default `True`. Add `YADGAR_CONTRADICTION_AUTO_DECAY` env knob (default `on`) that the curator wiring respects. Detection (without mutation) is always on; mutation is separately gateable for users who want detection-only.

**Default for the plan:** B1 (simpler; matches audit intent). If a user objects in practice, B2 is a one-line env-knob upgrade later.

---

## 4. Concrete Code Changes

### 4a. `yadgar/curation/__init__.py` — wire detection into the write path

Insert into `curate_on_remember()` between `similar = self._find_similar_memories(...)` (line 85) and the threshold-check loop:

```python
# v5.10.10: write-time contradiction detection (audit adopt-2).
# Env-gated; default on. Fail-soft: any error in the detector is logged
# but never blocks the write.
if os.environ.get("YADGAR_WRITE_TIME_CONTRADICTION", "on").lower() == "on" and similar:
    try:
        from yadgar.curation.contradiction import detect_contradictions
        # similar is already list[tuple[mem_id, sim]]; pass directly.
        contradictions = detect_contradictions(self._storage, similar, content)
        if contradictions:
            logger.info(
                "write_time_contradiction: %d contradicting memories flagged "
                "(reasons=%s) for new content=%r",
                len(contradictions),
                [c["reason"] for c in contradictions],
                content[:80],
            )
            # Side effect (confidence decay) already applied inside detect_contradictions.
            # Metric increment:
            try:
                from yadgar.metrics import yadgar_write_time_contradiction_total
                for c in contradictions:
                    yadgar_write_time_contradiction_total.labels(reason=c["reason"]).inc()
            except Exception:
                pass
    except Exception as exc:
        logger.warning("write_time_contradiction detector failed (fail-soft): %s", exc)
```

Notes:
- Re-uses `similar` already computed at line 85. No extra DB query.
- Runs BEFORE merge/link/create branches — detection happens on every write that gets far enough to compute `similar`. Whether the new memory gets merged, linked, or created is decided downstream by the existing logic; contradiction handling is orthogonal.
- Import inside the `if` block: zero cost when env is off.

### 4b. `yadgar/metrics.py` — add the new metric family

```python
yadgar_write_time_contradiction_total = Counter(
    "yadgar_write_time_contradiction_total",
    "Contradictions detected at write time, by detector reason.",
    ["reason"],  # negation_mismatch | action_divergence
)
```

Match the existing pattern. No new label dimensions beyond what `contradiction.py` already emits.

### 4c. `docs/CONFLICT_RESOLVER.md` — update to cover both layers

Current doc only describes the LLM resolver. Add a new section "Lightweight write-time detector (v5.10.10)" that:
- Explains the two-layer model: lightweight detector (default on) + LLM resolver (opt-in upgrade)
- Documents `YADGAR_WRITE_TIME_CONTRADICTION` env knob (default `on`)
- Explains the silent confidence-decay side effect (B1 decision) and points to the metric for observability
- Links to `yadgar/curation/contradiction.py` for the heuristics

### 4d. `docs/CHANGELOG.md` — under `[Unreleased]`

```markdown
### Added
- **v5.10.10 — Write-time contradiction detection wired by default.** `MemoryCurator.curate_on_remember()` now calls `detect_contradictions()` on every write (gate: `YADGAR_WRITE_TIME_CONTRADICTION=on`, default on). Lightweight cosine + negation-pattern + action-divergence heuristics. Reduces "stale-until-nightly" window to zero. Closes audit-2026-05-30 Adopt-2.
- `yadgar_write_time_contradiction_total{reason}` metric.

### Changed
- `docs/CONFLICT_RESOLVER.md` documents both layers (lightweight default-on detector + opt-in Ollama LLM resolver).
```

### 4e. `MIGRATION_NOTES.md` — append v5.10.10 section

```markdown
## 5.10.10

### Write-time contradiction detection now default-on

The lightweight contradiction detector (cosine + negation + action heuristics) now runs on every memorize() call. Pre-existing memories whose content negates the new write will have their `confidence` field decremented by 0.1–0.2 (clamped at 0.1 floor).

To disable:
```
export YADGAR_WRITE_TIME_CONTRADICTION=off
```

The opt-in LLM resolver (`YADGAR_CONFLICT_RESOLVER=on`, Ollama-dependent) is unchanged and remains default-off.
```

---

## 5. TDD Test List

New test file: `yadgar/tests/test_write_time_contradiction.py`

Tests (all written failing first, then implementation makes them pass):

1. **`test_default_on_fires_detector`** — with env unset, memorize a content that contradicts an existing memory via negation pattern → existing memory's `confidence` is reduced below 1.0. Verify via `storage.get_memory(old_id)["confidence"] < 1.0`.

2. **`test_env_off_skips_detector`** — set `YADGAR_WRITE_TIME_CONTRADICTION=off`, repeat scenario from test 1 → existing memory's `confidence` is unchanged (1.0).

3. **`test_no_similar_memories_noop`** — memorize content into an empty store → no error, no detector activity. Verifies the `and similar` guard.

4. **`test_detector_exception_does_not_block_write`** — monkeypatch `detect_contradictions` to raise `RuntimeError("boom")` → memorize still returns success, memory still inserted. Verifies fail-soft.

5. **`test_metric_increments_on_contradiction`** — verify `yadgar_write_time_contradiction_total{reason="negation_mismatch"}._value.get()` increments by exactly 1 after a single contradicting write.

6. **`test_llm_resolver_short_circuit_bypasses_lightweight`** — set `YADGAR_CONFLICT_RESOLVER=on`, mock LLM to return `NOOP` → memorize returns early (`stored: False`); the lightweight detector should NOT run (because the curator is never reached). Verifies orthogonality contract.

7. **`test_no_negation_no_action_change_no_decay`** — write content semantically similar but with no negation and same action verb → detector returns empty; old memory's `confidence` unchanged. Guards against false-positive decay.

Update existing tests if needed:
- `yadgar/tests/test_curation.py::test_contradiction_detection` already exercises the wrapper directly; no changes needed.
- `yadgar/tests/test_conflict_resolver.py` — no changes; LLM resolver path untouched.

---

## 6. Acceptance Criteria

- [ ] All 7 new tests pass; existing test suite green (`uv run pytest` from project root, no `-x`).
- [ ] `YADGAR_WRITE_TIME_CONTRADICTION` unset → detector runs (verified by test 1).
- [ ] `YADGAR_WRITE_TIME_CONTRADICTION=off` → detector skipped (verified by test 2).
- [ ] No new direct DB queries added to the write path (re-uses `similar` already computed).
- [ ] Metric `yadgar_write_time_contradiction_total{reason}` appears in `/metrics` output even at zero count.
- [ ] `docs/CONFLICT_RESOLVER.md` describes both layers with the relationship explicit.
- [ ] `CHANGELOG.md` and `MIGRATION_NOTES.md` updated.
- [ ] Manual end-to-end check: with `YADGAR_LOG_LEVEL=INFO`, write a contradictory memory → log line `write_time_contradiction: 1 contradicting memories flagged` appears.
- [ ] Bench check (informational only): p95 latency of `memorize()` does not regress >5% on a populated store (compare against pre-change run).

---

## 7. Effort Estimate

**~Half day.** Most code exists:
- Wiring: ~20 lines in `curation/__init__.py`.
- Metric: ~5 lines in `metrics.py`.
- Tests: ~150 lines (new file).
- Docs: ~50 lines across CHANGELOG / MIGRATION / CONFLICT_RESOLVER.

Risk: low. The detector itself is unchanged; only its invocation site moves from "latent API" to "called by default". Test 6 (orthogonality with LLM resolver) is the only nontrivial test.

---

## 8. Revisit Triggers

- If users complain about silent `confidence` decay → switch to sub-option B2 (split detection from mutation, add `YADGAR_CONTRADICTION_AUTO_DECAY` env knob).
- If lightweight heuristics produce high false-positive rate in practice (observable via the new metric vs. user-reported wrongness) → revisit by either (a) tightening the cosine threshold above 0.7, (b) requiring both negation AND action divergence rather than either, or (c) promoting the LLM resolver to default-on for users with Ollama detected at startup.
- If benchmarks from Adopt-1 (LongMemEval / LoCoMo) become available and show the write-time check materially improves retrieval-quality scores → consider promoting the LLM resolver to opt-out (default on when Ollama is reachable, fall back to lightweight when not).
- If the curator path becomes optional or replaceable (Refactor R2 from the same audit, "modularize 8-stage pipeline") → re-evaluate where the detector wiring lives.

---

## 9. Proposed `AUDIT_DECISIONS.md` Entry

To be added under "Adopt items" once this plan is approved/landed. Append-only style.

```markdown
#### 2. Write-time conflict resolution
- **Recommendation:** Add lightweight write-time contradiction check (cosine similarity of incoming memory vs top-K results, mark conflicting as stale). Fixes the "stale-until-nightly" 24h window.
- **Decision:** ADOPT (sub-option: wire existing latent detector, default on)
- **Reason:** The LLM resolver (`yadgar/conflict_resolver.py`, v5.3.4) already ships as opt-in. The lightweight detector matching the audit's exact wording (`yadgar/curation/contradiction.py` + `MemoryCurator.detect_contradictions()`) was built in v5.3.x but never wired into `curate_on_remember()`. Wiring it default-on closes the gap without requiring Ollama. LLM resolver stays as opt-in higher-fidelity upgrade. The original scan agent missed that the lightweight wrapper exists but is unwired — this plan addresses that specific gap.
- **Evidence:**
  - `yadgar/curation/contradiction.py` (built, 82 lines, tested via `test_curation.py::test_contradiction_detection`)
  - `yadgar/curation/__init__.py:183` — `MemoryCurator.detect_contradictions()` defined; `grep -rn detect_contradictions yadgar/` shows zero callers outside the file itself
  - `yadgar/conflict_resolver.py` (LLM resolver, env-gated, default off; docs at `docs/CONFLICT_RESOLVER.md`)
  - `yadgar/server/tools/memorize.py:302-405` — LLM resolver and curator paths confirmed orthogonal (LLM runs first; curator runs only when LLM falls through)
- **Revisit triggers:**
  - Silent confidence decay complaints → split detection from mutation (B2)
  - High false-positive rate observable via new `yadgar_write_time_contradiction_total{reason}` metric → tighten heuristics
  - Adopt-1 benchmarks show LLM resolver materially better → promote LLM to default-on when Ollama is reachable
- **Version slot:** v5.10.10 — see `docs/PLAN_V5_10_10_WRITE_TIME_CONTRADICTION.md`.
```
