#!/usr/bin/env python3
# ruff: noqa: PLR0913  — _migrate_project has 6 params (all necessary for
#   per-project isolation + dry-run + delete-monolith gating). PERMANENT.
"""Migrate all ``<project>-adr-log`` monolith pages to canonical per-ADR pages.

Car 2 (ADR-consultable, v5.141.0) replaced the write-only ``<project>-adr-log``
monolith with one CANONICAL wiki page per ADR (``<project>-adr-NNNN``, branch IS
NULL) and one CANONICAL index page (``<project>-adr-index``). This script performs
the one-time migration for every project that has a ``<project>-adr-log`` page in
the shared store.

Key properties:

  - Project-agnostic: enumerates every ``*-adr-log`` slug via direct DB query.
  - Idempotent: skips already-existing ``<project>-adr-NNNN`` pages (safe re-run).
  - Deprecated-audit (§C.6.1): rejects and drops ADRs with status
    ``rejected``/``deprecated`` that have NO inbound ``supersedes`` reference.
    Superseded targets and open/accepted ADRs are always retained.
  - Branch-drift guard: warns if stray ``<project>-adr-*`` pages land on a
    feature branch (should not exist post-migration).
  - Per-project failure isolation: one bad parse does NOT abort other projects.
  - Monolith DELETE is gated behind BOTH ``--delete-monolith`` AND a per-project
    verify pass. Default is ``--dry-run`` (no mutations).

Usage::

    # Preview all projects (no mutations):
    uv run scripts/migrate_adr_monolith.py

    # Live run (writes pages + index, no delete):
    uv run scripts/migrate_adr_monolith.py --execute

    # Live run with monolith deletion after successful verify:
    uv run scripts/migrate_adr_monolith.py --execute --delete-monolith

    # Override DB path:
    uv run scripts/migrate_adr_monolith.py --execute --db-path ~/.yadgar/surreal_db

The script accesses the EMBEDDED store directly via ``StorageEngine`` (same as
``migrate_v5_7_to_v5_8.py``), then calls the same ``adr_add`` helpers the live
server uses — ensuring identical canonical page format. Write commits are
synchronous (``_wiki_write_canonical(payload, wait=True)``) so verify reads land
after writes without a poll loop.

NEVER run against the shared production DB without operator sign-off.
See ``MIGRATION_NOTES.md`` for the exact commands to hand to the user.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path

_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

logging.basicConfig(
    level=logging.INFO,
    format='{"ts": "%(asctime)s", "level": "%(levelname)s", "event": "%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("migrate_adr_monolith")

# ── Monolith section parser ────────────────────────────────────────────────────

_ADR_HEADER_FULL_RE = re.compile(r"^## ADR-(\d{4})(?::\s*(.*))?$", re.MULTILINE)
_BULLET_RE = re.compile(r"^- (\w+):\s*(.*)", re.MULTILINE)

_FIELD_NAMES = (
    "status",
    "date",
    "context",
    "decision",
    "rationale",
    "alternatives",
    "consequences",
    "revisit_trigger",
    "supersedes",
)


def _parse_section_body(body: str) -> dict[str, str]:
    """Extract ``- key: value`` bullets from a monolith section body.

    Multi-line continuation lines (indented) are folded back into the value.
    Missing fields default to the empty string — the migration emits them as-is
    (blank values are better than silently losing data).
    """
    result: dict[str, str] = {f: "" for f in _FIELD_NAMES}
    current_key: str | None = None
    current_val_lines: list[str] = []

    def _flush() -> None:
        if current_key and current_key in result:
            result[current_key] = "\n".join(current_val_lines).strip()

    for line in body.splitlines():
        m = _BULLET_RE.match(line)
        if m:
            _flush()
            current_key = m.group(1)
            current_val_lines = [m.group(2)]
        elif current_key is not None and (line.startswith("  ") or line.startswith("\t")):
            # Indented continuation — part of the current bullet value.
            current_val_lines.append(line.strip())
        else:
            _flush()
            current_key = None
            current_val_lines = []

    _flush()
    return result


def parse_monolith_sections(content: str) -> list[dict]:
    """Parse a ``<project>-adr-log`` monolith into a list of ADR dicts.

    Returns list ordered by ADR-NNNN ascending (smallest NNNN first). Each
    dict has keys: ``adr_id``, ``title``, and the 9 field names from
    ``_FIELD_NAMES``. The title is the text after ``ADR-NNNN: `` on the header
    line; defaults to the adr_id when the header carries no title.
    """
    matches = list(_ADR_HEADER_FULL_RE.finditer(content))
    if not matches:
        return []

    sections: list[dict] = []
    for i, m in enumerate(matches):
        adr_id = f"ADR-{int(m.group(1)):04d}"
        title = (m.group(2) or "").strip() or adr_id
        # Body: from end of this header to start of next header (or EOF).
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[body_start:body_end]
        fields = _parse_section_body(body)
        sections.append({"adr_id": adr_id, "title": title, **fields})

    return sorted(sections, key=lambda s: int(s["adr_id"].split("-")[1]))


# ── Deprecated-audit logic (§C.6.1) ───────────────────────────────────────────

_DROPPABLE_STATUSES = frozenset({"rejected", "deprecated"})


def compute_inbound_refs(sections: list[dict]) -> dict[str, list[str]]:
    """Build a map ``target_id → [list of ADR-IDs that supersede it]``.

    Parses the ``supersedes`` field of each section; handles comma-separated
    lists such as ``ADR-0001, ADR-0002`` and bare ``ADR-0001`` forms.
    Returns empty lists for IDs with no inbound references.
    """
    inbound: dict[str, list[str]] = {}
    for s in sections:
        raw = (s.get("supersedes") or "").strip().lower()
        if raw in ("", "none"):
            continue
        targets = re.findall(r"ADR-(\d{4})", s.get("supersedes", ""), re.IGNORECASE)
        for t in targets:
            target_id = f"ADR-{int(t):04d}"
            inbound.setdefault(target_id, []).append(s["adr_id"])
    return inbound


def decide_retention(sections: list[dict]) -> dict[str, bool]:
    """Apply §C.6.1 deprecated-audit rules to each section.

    Returns a mapping ``adr_id → retain (True/False)``.

    Rules (in priority order):
      1. ``superseded`` → RETAIN (is a supersede target; dropping dangles chain).
      2. ``rejected`` or ``deprecated``, no inbound ``supersedes`` ref → DROP.
      3. ``rejected`` or ``deprecated``, WITH an inbound ref → RETAIN.
      4. ``open`` or ``accepted`` → RETAIN.
    """
    inbound = compute_inbound_refs(sections)
    decisions: dict[str, bool] = {}
    for s in sections:
        adr_id = s["adr_id"]
        status = (s.get("status") or "").strip().lower()
        if status == "superseded":
            decisions[adr_id] = True
        elif status in _DROPPABLE_STATUSES:
            decisions[adr_id] = bool(inbound.get(adr_id))
        else:
            decisions[adr_id] = True
    return decisions


# ── Per-project migration ──────────────────────────────────────────────────────


def _migrate_project(
    storage,
    project_name: str,
    monolith_page_id: int,
    content: str,
    directory_context: str,
    dry_run: bool,
    delete_monolith: bool,
) -> dict:
    """Migrate one project's monolith to canonical pages.

    Returns a result dict with keys:
      project, total, retained, dropped_deprecated, created, skipped_existing,
      index_written, verify_ok, monolith_deleted, errors (list).

    Isolated: exceptions are caught and appended to ``errors``; caller decides
    whether to abort the run.
    """
    from yadgar.core.server.tools.adr import (
        _adr_tags,
        _build_index_content,
        _canonical_adr_payload,
        adr_page_slug,
    )
    from yadgar.core.server.tools.wiki import _wiki_write_canonical, wiki_read

    result: dict = {
        "project": project_name,
        "total": 0,
        "retained": 0,
        "dropped_deprecated": 0,
        "created": 0,
        "skipped_existing": 0,
        "index_written": False,
        "verify_ok": False,
        "monolith_deleted": False,
        "errors": [],
    }

    try:
        sections = parse_monolith_sections(content)
        result["total"] = len(sections)

        retention = decide_retention(sections)
        surviving = [s for s in sections if retention.get(s["adr_id"], True)]
        dropped = [s for s in sections if not retention.get(s["adr_id"], True)]
        result["retained"] = len(surviving)
        result["dropped_deprecated"] = len(dropped)

        if dry_run:
            # Count supersede back-links for dry-run report.
            inbound = compute_inbound_refs(surviving)
            k_links = sum(1 for v in inbound.values() if v)
            logger.info(
                "[DRY-RUN] %s: would migrate %d (drop %d deprecated), "
                "create index, resolve %d supersede links, DELETE %s-adr-log",
                project_name,
                len(surviving),
                len(dropped),
                k_links,
                project_name,
            )
            return result

        # ── Two-pass: emit per-ADR pages, build index rows with supersede back-links ──
        inbound = compute_inbound_refs(surviving)
        index_rows: list[dict] = []

        for s in surviving:
            adr_id = s["adr_id"]
            slug = adr_page_slug(directory_context, adr_id)

            # Idempotency: skip already-existing pages.
            existing = wiki_read(slug, directory=directory_context)
            if "error" not in existing:
                result["skipped_existing"] += 1
                # Still need to add an index row for idempotent index rebuild.
                status = s.get("status") or "open"
                supersedes_val = (s.get("supersedes") or "none").strip()
                superseded_by_str = ",".join(inbound.get(adr_id, [])) or "-"
                nnnn_list = re.findall(r"ADR-(\d+)", superseded_by_str)
                superseded_by_display = ",".join(nnnn_list) if nnnn_list else "-"
                index_rows.append(
                    {
                        "adr_id": adr_id,
                        "status": status,
                        "date": s.get("date") or "",
                        "title": s.get("title") or adr_id,
                        "supersedes": supersedes_val,
                        "superseded_by": superseded_by_display,
                        "slug": slug,
                    }
                )
                continue

            # Build body using the same helper as adr_add.
            from yadgar.core.server.tools.adr import _build_adr_body  # noqa: PLC0415

            status = s.get("status") or "open"
            supersedes_val = (s.get("supersedes") or "none").strip()
            body_content = _build_adr_body(
                adr_id=adr_id,
                title=s.get("title") or adr_id,
                status=status,
                date=s.get("date") or "",
                context=s.get("context") or "",
                decision=s.get("decision") or "",
                rationale=s.get("rationale") or "",
                alternatives=s.get("alternatives") or "",
                consequences=s.get("consequences") or "",
                revisit_trigger=s.get("revisit_trigger") or "",
                supersedes=supersedes_val,
            )
            tags = _adr_tags(adr_id, status)
            payload = _canonical_adr_payload(
                slug=slug,
                content=body_content,
                category="decision",
                tags=tags,
                directory=directory_context,
            )
            write_result = _wiki_write_canonical(payload, wait=True)
            if write_result.get("stored") is False:
                result["errors"].append(
                    f"{adr_id}: page write failed: {write_result.get('reason', 'unknown')}"
                )
                continue

            result["created"] += 1

            # Compute superseded_by back-links for index row.
            superseded_by_adrs = inbound.get(adr_id, [])
            superseded_by_display = (
                ",".join(n.split("-")[1] for n in superseded_by_adrs) if superseded_by_adrs else "-"
            )
            index_rows.append(
                {
                    "adr_id": adr_id,
                    "status": status,
                    "date": s.get("date") or "",
                    "title": s.get("title") or adr_id,
                    "supersedes": supersedes_val,
                    "superseded_by": superseded_by_display,
                    "slug": slug,
                }
            )

        # ── Write the canonical index ──────────────────────────────────────────
        if index_rows:
            index_slug = f"{project_name}-adr-index"
            index_content = _build_index_content(project_name, index_rows)
            index_payload = _canonical_adr_payload(
                slug=index_slug,
                content=index_content,
                category="reference",
                tags=["adr", "adr-index"],
                directory=directory_context,
            )
            # Check if index already exists to set replace_slug.
            existing_index = wiki_read(index_slug, directory=directory_context)
            if "error" not in existing_index:
                index_payload["replace_slug"] = index_slug
            idx_result = _wiki_write_canonical(index_payload, wait=True)
            if idx_result.get("stored") is False:
                result["errors"].append(
                    f"index write failed: {idx_result.get('reason', 'unknown')}"
                )
            else:
                result["index_written"] = True

        # ── Verify ─────────────────────────────────────────────────────────────
        verify_ok = _verify_project(
            project_name,
            surviving,
            inbound,
            directory_context,
            storage,
        )
        result["verify_ok"] = verify_ok

        # ── Delete monolith (gated) ────────────────────────────────────────────
        if delete_monolith and verify_ok and not result["errors"]:
            ok = storage.delete_wiki_page(monolith_page_id)
            result["monolith_deleted"] = ok
            logger.info(
                "%s: deleted monolith %s-adr-log (page_id=%d) ok=%s",
                project_name,
                project_name,
                monolith_page_id,
                ok,
            )

    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"unexpected error: {exc}")
        logger.exception("Error migrating project %s", project_name)

    return result


def _verify_project(
    project_name: str,
    surviving: list[dict],
    inbound: dict[str, list[str]],
    directory_context: str,
    storage,
) -> bool:
    """Verify per-project migration: page count, index rows, supersede links, branch drift.

    Returns True iff all checks pass. Logs individual failures.
    """
    from yadgar.core.server.tools.adr import adr_page_slug, parse_index_rows
    from yadgar.core.server.tools.wiki import wiki_read

    ok = True

    # 1. Every surviving ADR page must exist canonically.
    for s in surviving:
        adr_id = s["adr_id"]
        slug = adr_page_slug(directory_context, adr_id)
        page = wiki_read(slug, directory=directory_context)
        if "error" in page:
            logger.error("VERIFY FAIL %s: %s not found canonically", project_name, adr_id)
            ok = False
        elif page.get("branch") is not None:
            logger.error(
                "VERIFY FAIL %s: %s has branch=%r (should be canonical NULL)",
                project_name,
                adr_id,
                page.get("branch"),
            )
            ok = False

    # 2. Index must exist and have the right row count.
    index_slug = f"{project_name}-adr-index"
    index_page = wiki_read(index_slug, directory=directory_context)
    if "error" in index_page:
        logger.error("VERIFY FAIL %s: index %s not found", project_name, index_slug)
        ok = False
    else:
        rows = parse_index_rows(index_page.get("content") or "")
        if len(rows) != len(surviving):
            logger.error(
                "VERIFY FAIL %s: index has %d rows, expected %d",
                project_name,
                len(rows),
                len(surviving),
            )
            ok = False

        # 3. Supersede links resolve — each referenced slug exists.
        for row in rows:
            sup_by = row.get("superseded_by") or "-"
            if sup_by in ("-", ""):
                continue
            for nnnn in re.findall(r"\d{4}", sup_by):
                target_slug = f"{project_name}-adr-{nnnn}"
                t_page = wiki_read(target_slug, directory=directory_context)
                if "error" in t_page:
                    logger.warning(
                        "VERIFY WARN %s: supersede target %s not found (dangling link)",
                        project_name,
                        target_slug,
                    )

    # 4. Branch-drift scan: no stray <project>-adr-* pages on a feature branch.
    adr_prefix = f"{project_name}-adr-"
    drift_rows = storage._q(
        "SELECT slug, branch FROM wiki_page "
        "WHERE string::starts_with(slug, $pfx) AND branch IS NOT NONE",
        {"pfx": adr_prefix},
    )
    if drift_rows:
        for dr in drift_rows:
            row_dict = dr if isinstance(dr, dict) else {}
            logger.warning(
                "VERIFY WARN %s: branch-drift page slug=%r branch=%r",
                project_name,
                row_dict.get("slug"),
                row_dict.get("branch"),
            )

    return ok


# ── Main enumerate + dispatch ──────────────────────────────────────────────────


def _run_migration(
    db_path: str | None,
    dry_run: bool,
    delete_monolith: bool,
) -> dict[str, dict]:
    """Enumerate all ``*-adr-log`` pages, migrate each project.

    Returns a mapping ``project_name → result_dict``.
    """
    os.environ.setdefault("YADGAR_ALLOW_ROOT", "1")
    os.environ.setdefault("YADGAR_DB_PASS", "root")
    os.environ.setdefault("YADGAR_DB_USER", "root")

    resolved_db_path = db_path or os.environ.get("YADGAR_DB_PATH", "~/.yadgar/surreal_db")

    # Bootstrap the full server-tool pipeline (needed for _wiki_write_canonical).
    from yadgar._shared.storage.migrations import (  # noqa: PLC0415
        _migration_013_wiki_page_version,
    )
    from yadgar.core import server as _server  # noqa: PLC0415
    from yadgar.core.bootstrap import core_init_engines as init_engines  # noqa: PLC0415

    init_engines(
        db_path=resolved_db_path,
        embedding_model="all-MiniLM-L6-v2",
    )
    storage = _server._get_storage()
    _migration_013_wiki_page_version(storage)

    # Wire a synchronous in-process drainer so wait=True writes commit immediately.
    _wire_sync_drainer(_server)

    # Enumerate monolith pages.
    monolith_rows = storage._q(
        "SELECT id, slug, content, directory_context, branch "
        "FROM wiki_page WHERE string::ends_with(slug, '-adr-log')"
    )

    if not monolith_rows:
        logger.info("No *-adr-log pages found — nothing to migrate")
        _server.shutdown()
        return {}

    logger.info("Found %d project(s) to migrate", len(monolith_rows))

    results: dict[str, dict] = {}
    for row in monolith_rows:
        row_dict = row if isinstance(row, dict) else {}
        slug = row_dict.get("slug") or ""
        if not slug.endswith("-adr-log"):
            continue
        project_name = slug[: -len("-adr-log")]
        directory_context = (row_dict.get("directory_context") or "").rstrip("/")
        page_id_raw = storage._extract_id(row_dict.get("id"))
        if page_id_raw is None:
            logger.warning("Skipping %s: could not extract page_id", slug)
            continue
        page_id = int(page_id_raw)
        content = row_dict.get("content") or ""

        logger.info(
            "Migrating project=%r directory=%r page_id=%d branch=%r",
            project_name,
            directory_context,
            page_id,
            row_dict.get("branch"),
        )

        res = _migrate_project(
            storage=storage,
            project_name=project_name,
            monolith_page_id=page_id,
            content=content,
            directory_context=directory_context,
            dry_run=dry_run,
            delete_monolith=delete_monolith,
        )
        results[project_name] = res

        if res["errors"]:
            logger.error("Project %s finished with errors: %s", project_name, res["errors"])
        else:
            logger.info(
                "Project %s: total=%d retained=%d dropped=%d "
                "created=%d skipped=%d index=%s verify=%s deleted=%s",
                project_name,
                res["total"],
                res["retained"],
                res["dropped_deprecated"],
                res["created"],
                res["skipped_existing"],
                res["index_written"],
                res["verify_ok"],
                res["monolith_deleted"],
            )

    _server.shutdown()
    return results


def _wire_sync_drainer(server_module) -> None:
    """Wire an in-process QueueDrainer so wait=True writes commit synchronously.

    Mirrors the test harness in ``yadgar/tests/_backend_harness.py``.
    """
    import yadgar._shared.runtime.state as _st  # noqa: PLC0415
    from yadgar.backend.queue_drainer import QueueDrainer  # noqa: PLC0415

    fq = server_module._get_file_queue()
    drainer = QueueDrainer(
        queue=fq,
        storage_factory=lambda: _st._storage,
        drain_interval=9999,  # background loop stays inert; drain_now() used by wait path
    )
    drainer.start()
    server_module._queue_drainer = drainer
    _st._queue_drainer = drainer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the migration (default is dry-run — preview only).",
    )
    parser.add_argument(
        "--delete-monolith",
        action="store_true",
        help=(
            "Delete the <project>-adr-log monolith after successful per-project verify. "
            "Only active when --execute is also set."
        ),
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to SurrealDB embedded store (overrides YADGAR_DB_PATH).",
    )
    args = parser.parse_args()

    dry_run = not args.execute
    delete_monolith = args.delete_monolith and args.execute

    if dry_run:
        logger.info("DRY-RUN mode (pass --execute to apply)")
    elif delete_monolith:
        logger.info("EXECUTE mode with --delete-monolith")
    else:
        logger.info("EXECUTE mode (monolith retained; pass --delete-monolith to remove)")

    results = _run_migration(
        db_path=args.db_path,
        dry_run=dry_run,
        delete_monolith=delete_monolith,
    )

    failed = [p for p, r in results.items() if r.get("errors")]
    if failed:
        logger.error("Migration completed with errors in %d project(s): %s", len(failed), failed)
        sys.exit(1)

    logger.info(
        "Migration %s complete: %d project(s) processed",
        "DRY-RUN" if dry_run else "EXECUTE",
        len(results),
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
