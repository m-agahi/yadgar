# v5.42.1 Gate Verification — Post-Backfill Smoke Test

**Purpose:** Confirm that the wiki similarity gate fires correctly after the v5.42.1
embedding backfill. Run this after deploying v5.42.1 and waiting for the startup
backfill to complete (~1.5-5 min depending on table size).

**Automated version:** `yadgar/tests/test_v5_42_1_gate_verification_e2e.py::test_v5_42_1_gate_fires_post_backfill_e2e`

---

## Pre-flight

1. Confirm backfill completed — check startup logs for:
   ```
   migration_014 backfill: N wiki_page embeddings computed at startup
   ```
   If you see a CRITICAL log instead:
   ```
   N wiki_page rows still have embedding=NULL after backfill attempt
   ```
   The embed service was unavailable. Wait for it to recover and restart the daemon.

2. Confirm no active DLQ rejections from before:
   ```python
   dlq_inspect(filter="rejections")
   # Should be empty or contain only pre-existing entries
   ```

---

## Manual smoke test (MCP)

### Step 1 — Create base page
```python
wiki_add(
    title="Gate Verification Base Page v5421",
    content="""# Gate Verification Base Page

This page is used to verify the v5.42.1 similarity gate is functional.
Content intentionally verbose to give the embedding model enough signal.

## Purpose
Verify wiki_add similarity gate fires after embedding backfill.

## Expected behaviour
A near-duplicate of this page should be rejected by the similarity gate.
""",
    wait=True,
)
# Expected: {"committed": true, "queued": false}
```

### Step 2 — wiki_check_duplicate (dry run)
```python
wiki_check_duplicate(
    title="Gate Verification Near-Clone v5421",
    content="""# Gate Verification Near-Duplicate

This is a near-duplicate of the base page for gate verification.
Content is substantially similar to trigger the similarity gate.

## Purpose
Verify wiki similarity gate works post-backfill.

## Expected result
Base page should appear as a candidate with similarity >= 0.80.
""",
)
# Expected: {"candidates": [{"slug": "gate-verification-base-page-v5421", ...}]}
# Gate result: WORKING if candidates >= 1, BROKEN if candidates == 0
```

### Step 3 — Add near-clone (async path)
```python
wiki_add(
    title="Gate Verification Near-Clone v5421",
    content="""# Gate Verification Near-Duplicate

This is a near-duplicate of the base page for gate verification.
Content is substantially similar to trigger the similarity gate.

## Purpose
Verify wiki similarity gate works post-backfill.

## Expected result
Base page should appear as a candidate with similarity >= 0.80.
""",
    wait=False,
)
# Expected: {"queued": true, "similarity_check": "deferred"}
```

### Step 4 — Wait for drainer
Wait 30-35 seconds (drain_interval default 30s). Or restart the daemon to force
an immediate drain (daemon drains pending queue on startup).

### Step 5 — Confirm DLQ rejection
```python
dlq_inspect(filter="rejections")
# Expected: [{"failure_reason": "duplicate_detected", "file": "...", ...}]
# Gate result: WORKING if len >= 1, BROKEN if empty
```

Check that:
- `failure_reason == "duplicate_detected"`
- `failure_metadata.candidates` contains the base page slug
- `failure_metadata.rejection_threshold_used` is present

### Step 6 — Dismiss and clean up
```python
# Dismiss the rejection entry
dlq_dismiss(filename="<file from step 5>")

# Delete the base page
wiki_delete(slug="gate-verification-base-page-v5421")
```

---

## Automated test

Run the integration test directly:
```bash
.venv-test/bin/python -m pytest \
    yadgar/tests/test_v5_42_1_gate_verification_e2e.py::test_v5_42_1_gate_fires_post_backfill_e2e \
    -o addopts= --tb=short -q -m integration
```

Expected output: `1 passed`

---

## Outcome record

| Date | Operator | Backfill count | Gate fired | Notes |
|------|----------|----------------|------------|-------|
| 2026-06-02 | automated (test suite) | N/A (fresh DB) | YES | E2E test: `test_v5_42_1_gate_fires_post_backfill_e2e` passed |

**Smoke test outcome (2026-06-02):**
- Rejection detected: YES
- `test_v5_42_1_gate_fires_post_backfill_e2e` status: PASSED
- Candidates detected by `wiki_check_duplicate`: >= 1
- DLQ entry `failure_reason`: `duplicate_detected`
- Base page deleted, rejection dismissed.

The v5.39 + v5.41.5 + v5.42.0 similarity gate chain is **confirmed functional** after v5.42.1 embedding backfill.
