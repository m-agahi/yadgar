# ADR-0009 — Project Registry Seeding Path (Car A, 2026-08-14 train)

> **STUB.** ADR capture is blocked until Car A lands (the ``project``
> table has zero rows, so ``adr_add`` cannot write). This file is the
> decision record parked under ``docs/adr/`` until the seed path
> works, after which the same content is promoted to the engine-#2
> ledger via ``adr_add`` and this stub retired. Tagged ``adr-pending``
> per plan §9.

## Status

Proposed — 2026-08-14 (Car A, identity train).

## Context

The engine-#2 ``project`` registry table is the FK target for every
``task`` / ``adr`` / ``agent_prompt`` ledger row. On a fresh install
the table ships with **zero rows**, and the runtime guard at
``backend/admin_exec/project_registry.py:92-128``
(``_ensure_project_exists_sync``) rejects every WRITE that names an
unknown ``project_id`` with a structured ``UnknownProjectError``
(ADR-0078, ADR-0202, ADR-0223).

The bootstrap deadlock: the guard refuses, but the operator had no
path to seed the registry. ``create_project_row``
(``backend/admin_exec/ledger.py:549``) was implemented and registered
in ``backend/admin_exec/__init__.py:152`` but **no CLI or MCP tool
exposed it**. ADR capture during the v5.182 handoff session failed for
this exact reason — decisions had to be parked as wiki pages tagged
``adr-pending`` instead.

## Decision

Add the seed path. **Do not relax ``_ensure_project_exists_sync``**
(ADR-0078 stays in force — auto-creating on collision is how a typo
mints a phantom namespace, per the comment at ledger.py:557-559).

1. **CLI subcommand.** ``yadgar project seed [--map <path>]`` reads
   the TSV at ``.yadgar/project-id-map.tsv`` (gitignored; default
   path; column 2 is the authoritative ``project_id`` per plan
   §5.3) and calls ``create_project_row`` per row. The
   ``forward_admin`` shape mirrors ``yadgar.core.cli.seed`` —
   host-side CLI, backend call over ``/admin`` (no DB on the
   client).
2. **MCP tool.** ``project_seed(directory, *, map_path)`` clones the
   same machinery as the CLI. The MCP boundary wraps ``SystemExit``
   in an error envelope so the client gets the same shape every
   other failure here returns.
3. **Idempotency.** Second call is a no-op for already-present rows
   — the backend raises ``DuplicateProjectError`` which the wrapper
   at ``ledger.py:571-573`` converts to ``{"ok": False, "error":
   "..."}`` carrying the key. ``seed_row`` classifies that as
   ``"skipped"`` rather than ``"failed"``.
4. **Best-effort per row.** A single malformed project_id must not
   abort the rest of the migration. ``FAIL: <key>: <error>`` is
   logged to stderr; the loop continues.

   **Correction (ledger task 13 defect 1, 2026-08-20):** the final
   exit code / MCP ``ok`` field is NOT unconditionally 0/True as
   originally decided above. ``cmd_project_seed`` now exits 1, and
   the MCP tool's ``ok`` reflects ``counts["failed"] == 0``, whenever
   at least one row genuinely failed (duplicates still classify as
   ``skipped``, not ``failed``, so an idempotent re-run stays
   success-shaped). The original decision left both surfaces
   success-shaped even with real per-row failures, which reached
   production: an operator ran the seed, got ``ok: True``, and
   reported three deterministic ``DataError 1406`` failures
   (``display_name`` overflow — see ``project.py``'s
   ``row["note"][:255]`` slice against the registry's
   ``display_name VARCHAR(64)`` column) as "transient". The
   best-effort **behavior** (loop continues past a bad row) is
   unchanged; only the **final signal** changed.
5. **DROP / REVIEW rows skipped.** Map rows whose column 2 is
   ``DROP`` or ``REVIEW`` are operator decisions (delete the rows;
   hand the entry back to a human), not registry rows. The seed
   command logs them as ``SKIP (drop|review)`` and moves on.

## Consequences

- Every car in the 2026-08-14 train that touches the engine-#2
  ledger (Car D's apply, the wiki/wiki-seed tools, the task tools)
  can now ``insert`` rather than crashing with
  ``UnknownProjectError``.
- The existing tests asserting the refusal behaviour
  (``tests/core/test_car_m_project_param.py:20`` and friends) stay
  green — the guard is unchanged. The new car only adds the
  bootstrap path; it does not weaken the read-side check.
- The map file format is the source of truth for ``project_id``
  values; the seed command never re-derives. Plan §5.3: column 2
  wins over any classifier. The kind column (``git`` / ``local``)
  is best-effort inferred from the key shape, since the map
  carries memory/wiki counts but no kind annotation.

## References

- ``backend/admin_exec/ledger.py:549`` — ``create_project_row``
- ``backend/admin_exec/project_registry.py:92-128`` — the guard
  (``_ensure_project_exists_sync``)
- ``backend/admin_exec/__init__.py:152`` — ``create_project_row``
  registration
- ``docs/plans/next-train-2026-08-14.md`` §2 — plan section
- ``docs/plans/next-train-2026-08-14.md`` §5.3 — map format
- ``.yadgar/project-id-map.tsv`` — the map (gitignored)
- ADR-0078, ADR-0202, ADR-0223 — guard stays in force
