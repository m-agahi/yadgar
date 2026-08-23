# C4 — task 234: Core /health self_version is the BACKEND's version, not the core's

## State going in

Task 234 in the ledger: `Core /health reports the BACKEND's version as its own
self_version: version=5.185.0 but versions_compatible.self_version=5.78.0,
peer=5.78.0. Compatibility verdict is computed backend-vs-backend.`

The reported outcome — `self_version=5.78.0` (backend) on a core /health where
top-level `version=5.185.0` (core) — is the literal symptom of the original
Car F (task #61) wire-up bug, but the version-compat wiring was already
corrected on a later car (recorded as "signals-that-lie" train, PR #62 / commit
range ending at `c7f794e8`). The remaining deliverable for C4 car 234 is to
verify the fix is in place, add pinning tests that lock the correct
behaviour, and close the ledger row with rationale.

## What the bug was (Car F original)

Car F (commit `e3d6aabc`) introduced `_handshake_block` in
`yadgar/core/server/http.py`. The first version read:

```python
from yadgar import BACKEND_VERSION  # noqa: PLC0415
...
return handshake_status(BACKEND_VERSION, _peer_version, side="core")
```

Wrong on two axes:

1. **`self_version` was `BACKEND_VERSION`, a hardcoded constant tracking
   the backend image**, not the core process' actual `__version__`. The
   block the /health payload carried said "my version is the backend's
   version" — a single curl read it as "the core has a different version
   than the top-level `version` field right above it".

2. **`peer_version` was the backend /health response's `version` field**
   (the correct reading), so `self_version` and `peer_version` both held
   the same number once the backend reported it, and `handshake_status`
   compared two copies of the backend's version against the core bounds.
   The function was always answering "is the backend version in the
   core-compatibility window?" — never "is this core paired with this
   backend?"

In production, the bug was masked because both `BACKEND_VERSION` and the
probed peer version happened to read `5.78.0` at the time — a deploy window
where the core hadn't been bumped and the backend image hadn't been
bumped — so the wrong-but-self-consistent answer happened to be the right
answer too. The `5.78.0` numbers in the task title are not "the bug at this
deployment"; they're the signature of the comparison shape, which is
"the core is asked whether the backend version is in the core window".

## What the fix is

A later car on the same train (recorded as a "signals-that-lie" car) changed
the import and the call so `__version__` — the core's own version — is
self, and `peer_version` is the backend's probe:

```python
from yadgar import __version__  # noqa: PLC0415
...
return handshake_status(__version__, _peer_version, side="core")
```

Three lines. `side="core"` then takes the `core_compatible(self)` /
`backend_compatible(peer)` branch in `handshake_status`, which is the
right comparison: "is THIS core still in the supported core window? AND
is the backend at the other end of this wire in the supported backend
window?" The peer and self stay distinct because they come from distinct
sources. The pins section below lists the existing test that holds the
fix in place.

## Plan deliverable for C4 car 234

1. **Confirm the fix is in place** — read the current source of
   `_handshake_block` and confirm `from yadgar import __version__`,
   `handshake_status(__version__, _peer_version, side="core")`.

2. **Confirm the pinning test exists** — `test_core_handshake_block_reports_own_version_not_backend_constant`
   in `yadgar/tests/core/test_version_compat.py` lines 90-107 explicitly
   catches the regression. The docstring names the bug history.

3. **Add one complementary pinning test** for the case that has NO
   existing coverage — `_handshake_block` with a CONFIGURED peer URL (a
   peer we probe) must read the peer's reported version as `peer_version`
   while keeping `self_version = __version__`. Today only the
   `peer_url=None` branch is pinned; the network branch is uncovered at
   the seam level. A mock-flavoured unit test covers the read-through
   path that motivated the original bug while staying deterministic.

4. **Close the task in BOTH the harness TaskList and the yadgar
   ledger row**, with a `C4 car 234` summary in the closing rationale.

## What this car is NOT

- Not a behavioural fix (the fix shipped on the earlier train).
- Not a perf change (no code path changes; only an additional test).
- Not a plan-doc rewrite of the version_compat module — `handshake_status`
  is already correct, the comparison branches are correct, the unverifiable
  pass is correct, and the sidecar-fallback is pinned by
  `test_sidecar_missing_falls_back_permissively`.

## Files touched

| File | Edit |
|---|---|
| `docs/plans/c4-task-234.md` | NEW — this file |
| `yadgar/tests/core/test_version_compat.py` | +1 test (`test_handshake_block_with_peer_url_keeps_self_and_peer_distinct`) |
| (no source change) | `_handshake_block` was already corrected on an earlier car |

## Acceptance

- All 7 `test_version_compat.py` tests pass.
- The new test red-fails if `_handshake_block`'s body regresses to either
  `BACKEND_VERSION` or any single-value replacement.
- The complexity / I33 / I30 / observe-coverage gates are clean — only a
  test was added, source unchanged.
- Task #234 closed in harness + yadgar ledger, completed_at stamped on
  both.
