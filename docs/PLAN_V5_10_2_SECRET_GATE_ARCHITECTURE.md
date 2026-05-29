# PLAN — v5.10.2: Secret-Gate Architecture (single chokepoint, no escape)

**Status:** drafted 2026-05-29 after discovering `anchor()` bypasses secret detection. Security fix.

**Master at draft time:** core v5.10.1 + backend v5.4.0 deployed.

**Sequencing:** part of unified v5.10.2 train (nightly-cycle hotfix + memorize/anchor parity + this).

---

## Why

### The hole

`yadgar/secrets.py::check_secrets()` runs at the API boundary of `memorize()` (memorize.py:152) and `wiki_add()` (wiki.py:53) but **NOT** `anchor()`. anchor() enqueues directly to `file_queue` with raw content + reason, bypassing the gate entirely.

```python
# anchor() body excerpt (misc.py:222):
_enqueue_payload: dict = {
    "content": content,         # ← never scanned
    "context": context,
    "reason": reason,            # ← never scanned
    ...
}
_get_file_queue().enqueue("anchor", _enqueue_payload)
```

Practical impact: a Claude session calling `anchor(content="AKIAIOSFODNN7EXAMPLE my AWS key", ...)` would store the key as `_anchor`-tagged, decay-immune, indefinite-life memory. Surfaces on every `restore()`, `project_brief(restore).top_anchors`, hot ranking. **Persistent + visible.**

### Why per-tool gating is fragile

Current pattern: each write tool calls `check_secrets()` at its own API handler:

| Tool | Gated? | Where |
|---|---|---|
| `memorize()` | ✓ | memorize.py:152 |
| `wiki_add()` | ✓ | wiki.py:53 |
| `anchor()` | ✗ | — |
| `update_active_work()` | ? | unverified |
| `bootstrap_project()` | ? | unverified |
| `checkpoint()` | ? | unverified |
| `wiki_update()` | ? | unverified |
| future tools | ✗ by default | — |

Every new write tool MUST remember to add `check_secrets()`. Failure = silent security regression. No lint, no test, no enforcement.

### Secondary weakness

GitHub token regex (`secrets.py:23`): `(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36,}`. The `{36,}` threshold lets short tokens slip. Observed in DB: memory id 519107 contains `ghp_SECRETTOKEN1234567890abcdefghijk` (33 chars after prefix) — under threshold, stored unredacted.

Real GitHub tokens are typically 36-40 chars. Test/fake tokens used in docs, error messages, or paste-from-screenshot can be shorter and slip through.

---

## Architectural fix

### Layer 1 — Storage gate (load-bearing)

Move `check_secrets()` invocation INSIDE the storage write path. Single chokepoint:

**Location:** `yadgar/storage/memory.py::insert_memory()` (or whatever the actual insert function is — locate during implementation).

**Behavior:**
1. Before SurrealDB write, call `check_secrets(content)` on the row's content field.
2. Also scan content of any field that contains free text (reason, tags joined as string).
3. If blocked: raise `SecretLeakBlocked(reason, pattern_preview)` exception. Storage NEVER commits a row containing a detected secret.
4. Caller (file_queue drainer, sync path, future paths) catches exception and either:
   - Writes to DLQ (for inspection + manual review)
   - Logs metric `yadgar_writegate_outcome{outcome="rejected_secret_at_storage"}`
   - Returns reject status to API layer if synchronous

**Single chokepoint guarantee:** EVERY write to the memory table passes through `insert_memory()`. New tools that bypass this function aren't writing memories — by definition. Cannot escape.

### Layer 2 — API boundary gate (UX fast-feedback)

Keep existing `check_secrets()` calls at `memorize()` + `wiki_add()` API handlers. Also ADD to:
- `anchor()` (the immediate hole)
- `wiki_update()`
- `update_active_work()`
- `bootstrap_project()`
- `checkpoint()` (scan `current_task`, `key_decisions`, `next_steps`, `open_questions`, `active_errors`, `custom_context`)

Refactor into shared helper:

```python
# yadgar/secrets.py — new helper:
def gate_or_reject(*content_fields: str) -> Optional[dict]:
    """Scan all provided fields. Return rejection dict if any has a secret, else None."""
    for field in content_fields:
        if not field:
            continue
        blocked, reason, pattern = check_secrets(field)
        if blocked:
            from yadgar.metrics import yadgar_writegate_outcome
            yadgar_writegate_outcome.labels(outcome="rejected_secret").inc()
            return {"stored": False, "reason": f"secret_detected: {reason}", "pattern_preview": pattern}
    return None
```

Each write tool calls it:
```python
result = gate_or_reject(content, reason, context)
if result is not None:
    return result
```

This is the FAST path — rejected before file_queue enqueue, before storage transaction, before any state change. Caller gets immediate feedback via MCP response.

### Layer 3 — I26 invariant lint (enforcement)

New script `scripts/check_secret_gate.py`. AST-based check:

1. Enumerate all `@_tool()`-decorated functions in `yadgar/server/tools/*.py`.
2. For each, determine if it's a WRITE tool (parameter names include `content`, `tags`, `current_task`, etc.).
3. Assert one of:
   - Function body contains a call to `gate_or_reject(` OR `check_secrets(`.
   - OR function delegates to a function that does (e.g. calls `memorize()` internally).
4. Or simpler: assert that no `_tool()`-decorated function in tools/ writes via storage.* without first calling `gate_or_reject()`.

Adds to pre-commit hooks alongside I13/I23/I24/I25. CI gate on every PR.

### Pattern strictness fix

`yadgar/secrets.py` updates:

| Pattern | Old threshold | New threshold | Why |
|---|---|---|---|
| GitHub token (`ghp_` etc.) | `{36,}` | `{20,}` | Test/fake tokens slip through. 20+ catches both fake-looking AND real. |
| GitLab (`glpat-`) | `{20,}` | unchanged |
| Stripe (`sk_live_`) | `{24,}` | unchanged |
| OpenAI (`sk-`) | `{30,}` | `{20,}` | Same fake-token issue. |
| Anthropic (`sk-ant-`) | `{32,}` | `{20,}` | Same. |

Lowering threshold trades off false-positive risk (legitimate `ghp_VALID` strings ≥20 chars elsewhere) — acceptable because:
- False-positive on store = caller sees rejection, knows to redact.
- False-negative on store = silent persistent secret leak. Much worse.

### Backfill scan (one-shot)

After v5.10.2 deploys, run `scripts/scan_db_for_secrets.py` once:
- Scans all existing memory + wiki content via the new pattern set.
- Reports IDs of matched rows for manual review.
- Does NOT auto-delete (data loss risk; user verifies).
- Output: report file at `~/.yadgar/secret-leak-scan-${TS}.txt`.

Adds one-shot cleanup affordance. Not part of regular runtime.

---

## What ships

| Item | Where | Risk |
|---|---|---|
| Layer 1 storage gate | `yadgar/storage/memory.py` | medium — exception path testing critical |
| Layer 2 `gate_or_reject` helper | `yadgar/secrets.py` | low |
| Layer 2 API-boundary calls | 6+ tool handlers (anchor, wiki_update, update_active_work, bootstrap_project, checkpoint, etc.) | low |
| Layer 3 I26 invariant lint | `scripts/check_secret_gate.py` + pre-commit + CI | low |
| Pattern strictness updates | `yadgar/secrets.py` regex literals | low |
| Backfill scan script | `scripts/scan_db_for_secrets.py` | low (read-only) |
| Tests | `yadgar/tests/test_secret_gate_architecture.py` | medium |

## What does NOT ship

| Item | Why deferred |
|---|---|
| Auto-redact + store partial content | Risk of confusing caller about what was stored. Reject is the right default. |
| Encrypted-at-rest for any secret that slips through | Defense-in-depth too deep; out of scope. |
| Per-tag policy (e.g. allow `aws-key` tag to bypass) | Anti-pattern; would create explicit policy holes. |
| External tools integration (gitleaks-as-subprocess) | Adds runtime dep + latency. yadgar/secrets.py is fast + sufficient. |

---

## Implementation order

1. **TDD scaffolding** `yadgar/tests/test_secret_gate_architecture.py`:
   - `anchor(content="AKIAIOSFODNN7EXAMPLE")` → rejected with secret_detected.
   - `update_active_work(directory, content="AWS key inside")` → rejected.
   - `bootstrap_project(directory, content="...")` → rejected.
   - `checkpoint(directory, current_task="ghp_...")` → rejected on current_task field.
   - `wiki_update(page_id, fields={"content": "ghp_..."})` → rejected.
   - Storage-level: directly call `insert_memory(content="AKIAIOSFODNN7EXAMPLE", ...)` → raises `SecretLeakBlocked`.
   - Drainer DLQ: simulated bypass of API gate (test-only path) ends in DLQ with reject reason.
   - I26 lint: a test that itself adds a fake write tool without gate; assert `check_secret_gate.py` flags it.
   - Pattern strictness: `ghp_SECRETTOKEN1234567890abcdefghijk` (33 chars) now blocked (was passing).
2. **Refactor `yadgar/secrets.py`** — add `gate_or_reject()` helper.
3. **Update Layer 2 callers** — anchor, wiki_update, update_active_work, bootstrap_project, checkpoint.
4. **Implement Layer 1 storage gate** — in `yadgar/storage/memory.py::insert_memory()`. Raises `SecretLeakBlocked`. Catch in file_queue drainer + sync paths.
5. **Pattern strictness updates** — lower `{36,}` to `{20,}` for ghp/sk-/sk-ant-/etc.
6. **I26 lint script + pre-commit hook** — AST-walk.
7. **Backfill scan script** — `scripts/scan_db_for_secrets.py`. Read-only.
8. **MIGRATION_NOTES.md** v5.10.2 section.
9. **CHANGELOG.md** entry.
10. **Version bump** 5.10.1 → 5.10.2 (in same release commit as the rest of v5.10.2 train).

---

## Acceptance criteria

- `pytest yadgar/tests/test_secret_gate_architecture.py` green.
- All existing tests (`test_secrets.py`, `test_anchor.py`, `test_memorize.py`, `test_wiki_*.py`) green.
- Full sweep `pytest -n 2` 0 failures (solo-rerun any flake per v5.8 lesson).
- I13 + I23 + I24 + I25 + **I26 (new)** lints exit 0.
- `python scripts/check_versions.py` exit 0.
- Backfill scan script runs on live DB and produces a report (manual run, not automated).

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Layer 1 storage gate breaks existing flows that legitimately store secret-looking content (e.g. tests, fixtures) | Add `_SECRET_GATE_BYPASS` env var for test fixtures ONLY. Document it's test-use-only. CI strips it. |
| `gate_or_reject` rejection breaks an automation that relied on lax behavior | Surfaces via metric `rejected_secret_total` counter. Operator review before tightening. |
| Pattern strictness `{20,}` causes false-positives on legit short identifiers | Pattern still requires `ghp_` etc. prefix. False-positive risk is bounded. Backfill scan first to see real rate before shipping. |
| I26 lint blocks legitimate non-write tools | Heuristic: only flag tools with `content: str` or equivalent. Manual override via `# secret-gate: skip` comment. |
| Storage exception during async drainer leaves DLQ entries operator must clean | DLQ inspection tool (already exists: dlq_inspect). Document procedure. |
| Existing leaked rows in DB (already past gate) | Backfill scan script identifies for manual cleanup. Out of automatic scope. |

---

## Estimate

~200-300 LOC implementation + ~250 LOC tests + ~100 LOC I26 script + docs. Single agent dispatch, ~60-90 min.

---

## Sequencing

Part of unified v5.10.2 train:
1. Nightly-cycle hotfix (`PLAN_V5_10_2_NIGHTLY_CYCLE_HOTFIX.md`)
2. Memorize/anchor parity (`PLAN_V5_10_X_MEMORIZE_ANCHOR_PARITY.md`)
3. **Secret-gate architecture (this plan)**

All three small, surgical, related (memory tooling robustness + security). One release commit, one tag `v5.10.2`. Single image rebuild.

Order within agent dispatch: secret-gate FIRST (security urgency) → parity → nightly hotfix. Agent should ship each as separate commit per scope.

---

## Open / parked questions

- **Should checkpoint's structured fields all be scanned, or just `current_task` + `custom_context`?** Lean: all free-text fields. `next_steps`, `open_questions`, `active_errors` are all caller-supplied strings.
- **`_SECRET_GATE_BYPASS` env var name** — bikeshed. Lean: `YADGAR_SECRET_GATE_DISABLED` (matches other kill switches). Document loudly in MIGRATION_NOTES + log warning when enabled at startup.
- **Pattern set for `aws-cli`-style key environment variables (`AWS_SECRET_ACCESS_KEY=...`)** — generic credential pattern (regex line 75 in secrets.py) already catches `secret\s*[=:]\s*` form. Verify with test.
- **Should the storage gate scan tags too?** A caller could inject a tag like `aws_key:AKIA...`. Lean YES — scan tags joined as one string before write.
- **DLQ vs immediate-reject for async drainer** — DLQ keeps a record for investigation; immediate-reject loses the payload. Lean DLQ for forensics + post-incident review.

---

## v5.X+ follow-up (deferred)

- Per-tag policy overrides (explicit allowlist of `_test`-tagged content bypassing gate for fixtures).
- External secret scanner integration (gitleaks/trufflehog as subprocess) — only if observed false-negative rate justifies the runtime cost.
- Encrypted-at-rest for content fields — major rework, separate train.
- Telemetry dashboard: rejection counters by pattern type → identify which patterns fire often vs not (tune set over time).
