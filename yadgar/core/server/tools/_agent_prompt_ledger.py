"""Ledger heal for the packaged agent-library slugs (ADR-0217 / ADR-0218).

``seed_agent_prompts`` seeds pages create-if-absent per SLUG, and the ledger row
used to be written only as a side effect of creating the page. A page that
predated the ledger table therefore never got a row, and no seed run could
repair it: ``agent_prompt_get("pr-review")`` returned ``not_found`` while the
page was right there. The same path never recorded ``baseline_hash``, so the
ADR-0217 three-way upgrade merge had no ancestor.

This module closes that for the slugs the package owns, and ONLY those. The
open-ended corpus stays with the human-run procedure
``docs/prompts/backfill-agent-pattern-ledger.md`` (ADR-0005).

Split out of ``agent_prompts.py`` for the I13 file-LOC cap. ``_forward_admin``
is resolved through the ``agent_prompts`` module at call time, so every test
seam that patches that module's binding covers this code too.
"""

from __future__ import annotations

import logging

from yadgar._shared.observability.observe import observe
from yadgar.core.server.tools import agent_prompts as _ap

logger = logging.getLogger(__name__)

_LEDGER_LIST_OPS = {
    "agent_pattern": "list_agent_prompt_rows",
    "agent_discipline": "list_agent_discipline_rows",
}


@observe(tier="stage", metric="tools.agent_prompt_ledger._packaged_ledger_entries")
def _packaged_ledger_entries() -> list[tuple[str, str, str, str, str]]:
    """``(table, name, slug, purpose, packaged_hash)`` for every packaged slug.

    The contract's row lives in ``agent_pattern`` under ``name="contract"``:
    that is where ``agent_prompt_save`` (which ``_seed_contract_page`` calls)
    has always written it, so the heal must agree or it would mint a second
    row in the other table.
    """
    out = [
        (
            "agent_pattern",
            pattern,
            f"agent-prompt-{pattern}",
            purpose,
            _ap._content_hash(_ap._wrap_prompt_page(purpose, content)),
        )
        for pattern, purpose, content in [*_ap.STARTER_PROMPTS, _ap.CONTRACT_GENESIS]
    ]
    out += [
        (
            "agent_discipline",
            name,
            f"{_ap.DISCIPLINE_SLUG_PREFIX}{name}",
            purpose,
            _ap._content_hash(_ap._wrap_prompt_page(purpose, content)),
        )
        for name, purpose, content in _ap.DISCIPLINES
    ]
    return out


@observe(tier="stage", metric="tools.agent_prompt_ledger._read_ledger_rows")
def _read_ledger_rows() -> dict[str, dict[str, dict]]:
    """Both ledger tables keyed by ``name``. Raises ``RuntimeError`` on a failed read.

    NEVER degrade a failed read to an empty set: "no row exists" would make
    the heal rewrite every packaged row with seed-time values.
    """
    out: dict[str, dict[str, dict]] = {}
    for table, op in _LEDGER_LIST_OPS.items():
        result = _ap._forward_admin(op, {}) or {}
        rows = result.get("rows")
        if result.get("ok") is False or rows is None:
            raise RuntimeError(f"{op} failed: {result.get('error')!r}")
        out[table] = {str(r["name"]): r for r in rows if r.get("name") is not None}
    return out


@observe(tier="stage", metric="tools.agent_prompt_ledger._write_ledger_row")
def _write_ledger_row(table: str, row: dict) -> None:
    """Forward one row upsert in the payload shape each table's op takes."""
    if table == "agent_pattern":
        _ap._forward_admin(
            "save_agent_pattern_row",
            {
                k: row.get(k)
                for k in ("name", "body_slug", "content_hash", "purpose", "status", "baseline_hash")
            },
        )
        return
    meta = {k: row[k] for k in ("purpose", "status", "always_applied", "position") if k in row}
    _ap._forward_admin(
        "save_agent_discipline_row",
        {
            "name": row["name"],
            "body_slug": row["body_slug"],
            "content_hash": row["content_hash"],
            "baseline_hash": row.get("baseline_hash"),
            "meta": meta,
        },
    )


@observe(tier="stage", metric="tools.agent_prompt_ledger.heal_seeded_ledger_rows")
def heal_seeded_ledger_rows(storage) -> dict:
    """Give every packaged page a ledger row, and record its baseline when provable.

    * Page present, row absent: write a row pinned to the LIVE page bytes.
    * ``baseline_hash`` = hash of the packaged content, but ONLY when the live
      page still is that content. A page that differs may have been seeded from
      an older package, and naming the current one as its ancestor would make
      ADR-0217's merge read a stale page as a local edit. Such names are
      reported, never guessed.
    * Row present: untouched, except a NULL ``baseline_hash`` is filled under
      the same proof, with every other column written back as stored.
    """
    report: dict = {
        "ledger_rows_created": 0,
        "ledger_baselines_set": 0,
        "ledger_baseline_unknown": [],
        "ledger_error": None,
    }
    try:
        existing = _read_ledger_rows()
    except RuntimeError as exc:
        logger.warning("seed ledger heal skipped: %s", exc)
        report["ledger_error"] = str(exc)
        return report

    for table, name, slug, seed_purpose, packaged in _packaged_ledger_entries():
        page = _ap._read_agent_prompt(slug, storage=storage)
        if page is None:
            continue
        live_hash = _ap._content_hash(page["content"])
        baseline = packaged if live_hash == packaged else None
        row = existing[table].get(name)
        if row is None:
            if baseline is None:
                report["ledger_baseline_unknown"].append(name)
            _write_ledger_row(
                table,
                {
                    "name": name,
                    "body_slug": slug,
                    "content_hash": live_hash,
                    "purpose": _ap._extract_purpose(page["content"]) or seed_purpose,
                    "status": "active",
                    "baseline_hash": baseline,
                },
            )
            report["ledger_rows_created"] += 1
        elif row.get("baseline_hash") is None and baseline is not None:
            _write_ledger_row(table, {**row, "baseline_hash": baseline})
            report["ledger_baselines_set"] += 1
    return report
