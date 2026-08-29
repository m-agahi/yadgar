# Moved to `yadgarhq/docs`

The successor architecture record no longer lives in this repository. It is at
**https://github.com/yadgarhq/docs**.

| Was | Now |
|---|---|
| `docs/plans/new-arch/architecture-decisions-2026-08-29.md` | `yadgarhq/docs` → `architecture-decisions-2026-08-29.md` |
| `docs/plans/new-arch/ask-tool-design.md` | `yadgarhq/docs` → `ask-tool-design.md` |
| `docs/plans/new-arch/proto-contract-design.md` | `yadgarhq/docs` → `proto-contract-design.md` |
| `docs/plans/new-arch/proto/` | `yadgarhq/docs` → `proto/` (relocating again to `yadgarhq/proto`) |

ADR-0468, ADR-0470, ADR-0471 and ADR-0472 cite the old paths. They were written
before the move and are not rewritten; this file is the redirect.

**Why it moved:** the successor is a different system in a different language
across many repositories. Its design record was accumulating here, inheriting this
project's ADR ledger, release machinery and Python-specific pre-commit gates, none
of which apply to it. Nothing in that record describes code in this repository —
the successor starts with empty databases and migrates nothing.

This repository remains the current implementation and is unaffected.
