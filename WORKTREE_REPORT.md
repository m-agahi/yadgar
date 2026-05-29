# Worktree Report — v5.10.2 Unified Hotfix Train

Branch: `worktree-agent-a64ed7076522efc1f`
Base: `master @ 725a65a`
Completed: 2026-05-29

---

## Phase 1 — Secret-Gate Architecture (c1–c6)

### Commits

| Commit | Message |
|---|---|
| `72e1d11` | `test(secret_gate): TDD failing tests for storage + API gates + I26 lint + pattern strictness` |
| `e7514ec` | `feat(secrets): SecretLeakBlocked + gate_or_reject + lowered pattern thresholds` |
| `3c2a783` | `feat(secret_gate): API-boundary gates on anchor + update_active_work + bootstrap_project + checkpoint + wiki_update` |
| `0cfec56` | `chore(lint): I26 invariant — scripts/check_secret_gate.py + pre-commit entry` |
| `f0d9641` | `feat(scan): v5.10.2 backfill scan script — scripts/scan_db_for_secrets.py` |

### What was implemented

**Layer 2 (API boundary)** — `gate_or_reject(*content_fields)` helper in `yadgar/secrets.py`. Added to: `anchor()`, `checkpoint()`, `update_active_work()`, `bootstrap_project()`, `wiki_update()`, `agent_prompt_save()`.

**Layer 1 (storage)** — `check_secrets()` inside `insert_memory()` before DB write. Raises `SecretLeakBlocked` on hit. `YADGAR_SECRET_GATE_DISABLED` kill switch bypasses Layer 1 with loud warning.

**DLQ handling** — `_classify_error()` treats `SecretLeakBlocked` as `permanent` (not transient), so infected queue entries move to DLQ after 3 attempts.

**Pattern thresholds**: `ghp_/gho_/ghu_/ghs_/ghr_ {36,}→{20,}`, `sk-ant- {32,}→{20,}`, `sk- {30,}→{20,}`.

**I26 invariant lint** — `scripts/check_secret_gate.py`: AST-walks all `@_tool()` write tools; exits 1 if any lack `gate_or_reject()` or `check_secrets()`. Pre-commit hook added as `check-secret-gate`.

**Bonus finding**: I26 lint caught `agent_prompt_save` — a write tool with a `content` parameter that was missed in the original 5-tool spec list. This is exactly what the invariant is for (defence against spec drift).

**Backfill scan** — `scripts/scan_db_for_secrets.py`: read-only scan, `--dry-run` (always on), `--storage-mock` for tests, `YADGAR_SCAN_REPORT_DIR` env override.

### Test results
37 tests in `test_secret_gate_architecture.py` — all pass.

---

## Phase 2 — memorize(is_protected=True) Anchor Parity (c7–c8)

### Commits

| Commit | Message |
|---|---|
| `9864485` | `feat(memorize): v5.10.x anchor parity — is_protected=True auto-tier/tags/reason` |

### What was implemented

`memorize(is_protected=True, reason="")` now has full parity with `anchor()`:

| Behaviour | Before | After |
|---|---|---|
| tier when unset | None | "conditional" (auto-set) |
| `_anchor` in tags | Only post-insert in sync path | Injected before insert + synced via `update_memory_fields` |
| `anchor:{reason}` tag | Never | Added when `reason` provided |
| `semantic_immortal` without reason | Accepted silently | Rejected when `ANCHOR_SEMANTIC_IMMORTAL_REQUIRES_REASON=True` |

New kwarg: `reason: str = ""`.

### Test infrastructure fixes

The TDD test file (`test_memorize_anchor_parity.py`) had two fixture bugs:
1. `monkeypatch.setattr("yadgar.server.tools.memorize.settings", ...)` resolved to the `memorize` function (shadowed by `from ... import memorize` in `tools/__init__.py`) — fixed by using `importlib.import_module()` directly.
2. `monkeypatch.setattr(_fq, "is_draining", ...)` patched the module attribute but `memorize.py` held a direct reference — fixed by also patching `_mem_mod.is_draining`.

### Test results
13 tests in `test_memorize_anchor_parity.py` — all pass.

---

## Phase 3 — Nightly Cycle Hotfix (c9–c11)

### Commits

| Commit | Message |
|---|---|
| `c37dafd` | `test(vacuum): v5.10.2 TDD — _log_consolidation_row must use YADGAR_DB_URL not :8080 literal` |
| `c24dc07` | `fix(deps): v5.10.2 — promote surrealdb to base deps (was dev-only)` |
| `1586dd4` | `fix(vacuum): v5.10.2 — use YADGAR_DB_URL in _log_consolidation_row not :8080 literal` |

### What was fixed

**Bug 1 — surrealdb missing from base deps**: `surrealdb>=1.0.0` was in `[project.optional-dependencies].dev`. Fresh installs would `ImportError` on `StorageEngine.__init__`. Promoted to `[project.dependencies]`.

**Bug 2 — vacuum `:8080` literal**: `_log_consolidation_row()` in `yadgar/vacuum/__init__.py:130` used `"http://127.0.0.1:8080"` as a hard-coded fallback. Fixed to `os.environ.get("YADGAR_DB_URL", "http://127.0.0.1:8080")`.

### Test results
2 new TDD tests in `TestLogConsolidationRowURL` — confirmed red before fix, green after. YADGAR_DB_URL tests in `test_nightly_cycle.py` continue to pass.

---

## Phase 4 — Release (c12)

### Commits

| Commit | Message (pending) |
|---|---|
| pending | `chore(release): bump 5.10.1→5.10.2 + CHANGELOG + MIGRATION_NOTES` |

### Version bump
- `pyproject.toml`: `5.10.1 → 5.10.2`
- `server.json`: both `version` and `packages[0].version` updated
- `docker-compose.yml`: image tag updated
- `uv.lock`: regenerated
- `scripts/check_versions.py`: exits clean

---

## I26 Lint Status

```
$ python scripts/check_secret_gate.py
I26 OK — all write tools gated (yadgar/server/tools)
```

All write tools gated. One bonus violation caught during implementation (`agent_prompt_save`).

---

## Invariant Status

All pre-commit hooks pass on all modified files:
- I13 complexity: baselines updated for `memorize.py`, `misc.py`, `project.py`, `admin_other.py`, `memory.py`, `file_queue/__init__.py`, `test_vacuum.py`
- I23 metric writers: clean
- I24 trace spans: clean
- I25 three-way sync: clean
- I26 secret gate: clean
- ruff lint + format: clean
- gitleaks: clean
