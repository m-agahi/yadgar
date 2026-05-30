# PLAN — v5.13.1: Integration test backend version pin fix (drift since v5.0.3)

**Status:** drafted 2026-05-30. Plan-first per I27. Patch on v5.13 minor train per `docs/VERSIONING.md`.

**Master at draft time:** v5.13.0 LIVE.

**Sequencing:** v5.13.1 hotfix. Bug pre-dates v5.13.0 (stale since v5.0.3 era) but discovered post-v5.13.0 ship.

---

## Why

User asked during v5.13.0 ship: *"why the tests are running against old backend 5.0.3?"*

Investigation: `yadgar/tests/integration/conftest.py:98` hardcodes `openfantasy/yadgar-backend:5.0.3` as the container image spun up for integration tests. Production runs `5.4.0` (backend-v5.4.0 shipped 2026-05-29 with CE + embedding caches). **Integration tests have been running against a 1-year-stale backend image** — CE cache + embedding cache code paths NEVER exercised in integration.

Other `5.0.3` references in `yadgar/tests/test_check_image_size.py` are FIXTURE/MOCK strings used to test version-string parser logic — those are correct as-is.

## Goals

1. Integration tests use the CURRENT backend version (read from `server.json` `backend_version` field).
2. Drift-proof: future backend version bumps automatically apply without code change.
3. Fallback if `server.json` unreadable: skip integration tests with clear message.

## Non-goals

- Changing the test fixtures in `test_check_image_size.py` (those reference 5.0.3 as test data; correct).
- Rewriting integration test suite. Pure config-source fix.

## Approach

Replace hardcoded literal with `server.json` lookup at fixture import time:

```python
# yadgar/tests/integration/conftest.py — near top
import json
from pathlib import Path

_SERVER_JSON = Path(__file__).resolve().parents[3] / "server.json"

def _backend_image() -> str:
    """Return current backend image tag from server.json (single source of truth)."""
    try:
        data = json.loads(_SERVER_JSON.read_text())
        version = data["backend_version"]
        return f"openfantasy/yadgar-backend:{version}"
    except (OSError, KeyError, json.JSONDecodeError) as e:
        import pytest
        pytest.skip(f"Cannot read backend_version from {_SERVER_JSON}: {e}")
```

Replace `"openfantasy/yadgar-backend:5.0.3"` at line 98 with `_backend_image()`.

## Tests

`yadgar/tests/integration/test_conftest_backend_pin.py` (new):
1. `test_conftest_uses_server_json_backend_version` — assert `_backend_image()` returns matching string from server.json
2. `test_conftest_skips_when_server_json_missing` — patch path to non-existent file → assert pytest.skip raised
3. `test_no_hardcoded_5_0_3_in_conftest` — regression gate: assert string `"5.0.3"` not in conftest.py source (excluding comments)

## Acceptance

- All 3 new tests green
- Integration tests now spin up `openfantasy/yadgar-backend:5.4.0` (current production)
- Pre-commit hooks pass
- CHANGELOG v5.13.1 entry + MIGRATION_NOTES note

## Risks + rollback

| Risk | Mitigation |
|---|---|
| Integration tests fail because they relied on v5.0.3 behavior | Skip + report; if real, escalate as v5.13.2 |
| `server.json` schema changes break parsing | Tests assert key + handle KeyError → skip cleanly |

Rollback: revert v5.13.1 commits. Integration tests use v5.0.3 again (silent drift returns).

## Files to modify

- `yadgar/tests/integration/conftest.py` — replace hardcoded image string with `_backend_image()` lookup
- `yadgar/tests/integration/test_conftest_backend_pin.py` — new test file
- `CHANGELOG.md` — v5.13.1 entry
- `MIGRATION_NOTES.md` — v5.13.1 section
- `pyproject.toml`, `server.json`, `docker-compose.yml`, `uv.lock` — bump 5.13.0 → 5.13.1

## Effort

~15 min. Trivial scope. Same pattern as prior conftest-style patches.

## Cross-references

- `docs/PLAN_V5_15_0_CPU_BURSTS_RESIDUAL.md` — next minor (incl. v5.13.0 secret-gate caller plumbing folded in)
- Original drift: backend-v5.0.3 era (~Q1 2026); not tracked until discovered 2026-05-30
- `docs/VERSIONING.md` — patch numbering convention
