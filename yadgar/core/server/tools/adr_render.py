# ruff: noqa: PLR0913  — _build_adr_body intentionally has 11 params (ADR schema
#   has 10 mandatory content fields + adr_id). PERMANENT — see .complexity-allowlist.json.
"""ADR rendering, tag helpers, supersede handling, and write-path assembly.

Extracted from adr.py (car/adr-split). The public surface of this module
is re-exported from yadgar.core.server.tools.adr for backward compatibility.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from yadgar._shared.contracts.models import ADR
from yadgar._shared.observability.observe import observe

# Imported lazily at call-time to match the pattern in other tool modules.
from yadgar.core.server.tools.admin_other import wiki_update
from yadgar.core.server.tools.adr_index import adr_page_slug
from yadgar.core.server.tools.wiki import _resolve_page_id_by_slug

# ── Validation constants ───────────────────────────────────────────────────────

# Valid ADR status values.
_VALID_STATUSES: frozenset[str] = frozenset(
    {"open", "accepted", "superseded", "rejected", "deprecated"}
)

# Required caller-supplied content fields (directory is handled separately).
_REQUIRED_FIELDS: tuple[str, ...] = (
    "title",
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


# ── Decision-form gate (Car B) ─────────────────────────────────────────────────
#
# An ADR's Decision states the RULE, not the work that produced it.  Context may
# cite the incident; the Decision may not name a car, a train, or a ledger row as
# its SUBJECT.  Measured on the 272-ADR corpus (2026-08-28): the drift is FORM,
# not substance — every ADR since 2026-08-19 carries real alternatives, but some
# read as worklogs because the Decision's subject is the car that wrote them.
#
# PRECISION IS THE DESIGN CONSTRAINT.  A false positive blocks a legitimate ADR
# write, which is worse than missing one, so these patterns match SUBJECT
# POSITION rather than mere mention.  The two corpus fixtures that kill the naive
# forms (see yadgar/tests/core/test_adr_decision_form_gate.py):
#   * ADR-0444 mentions ``origin/train/<name>`` and states a general rule → a
#     substring match on "train/" is WRONG.
#   * ADR-0460 uses generic car-as-subject ("A deletion car must still move
#     ratchets DOWN") → grammatical-subject matching on the bare noun is WRONG.
# Hence: a SPECIFIC designator in LEADING position, plus one ledger phrase that
# is specific enough to match anywhere.

# "Car-F ...", "Car B2 ...", "Car 5 ..." as the decision's opening subject.
# CASE-SENSITIVE ON PURPOSE: the uppercase designator is what separates a car
# label from ordinary prose.  Adding re.IGNORECASE silently widens this to match
# any sentence opening with the word "car".
_LEADING_CAR_SUBJECT = re.compile(r"^[\s\"'`(]*Car[-\s](?:[A-Z]\d*[a-z]?|\d+)\b")

# A train REF as the decision's opening subject ("train/bug-bag-2 ships ...",
# "Train `train/x` lands ...").  The literal ``train/`` is required — a decision
# that merely talks about trains is not caught, by design.
_LEADING_TRAIN_SUBJECT = re.compile(
    r"^[\s\"'`(]*(?:The\s+)?(?:[Tt]rain\s+)?[`'\"]?(?:origin/)?train/[\w./-]+"
)

# A ledger row closed as the decision's own consequence ("Closes #94 in the
# ledger", "closes ledger row #94").  Specific enough to match anywhere; plain
# ledger prose ("a completed ledger row stays readable") does not reach it.
_LEDGER_ROW_CLOSE = re.compile(
    r"\bclos(?:e|es|ed|ing)\s+"
    r"(?:#\d+\s+in\s+the\s+ledger|(?:the\s+)?ledger\s+(?:row|task|entry|item)\s*#?\d+)",
    re.IGNORECASE,
)

_DECISION_FORM_PATTERNS: tuple[re.Pattern[str], ...] = (
    _LEADING_CAR_SUBJECT,
    _LEADING_TRAIN_SUBJECT,
    _LEDGER_ROW_CLOSE,
)


@observe(exempt="pure regex predicate on one string; no I/O, no error branch worth spanning")
def _car_shaped_decision_error(decision: str) -> dict | None:
    """Return an error dict when ``decision`` reads as a worklog, else ``None``.

    Blank input returns ``None`` — an absent decision is a missing-required-field
    error, and the caller's field loop must own that diagnosis.
    """
    text = (decision or "").strip()
    if not text:
        return None
    for pattern in _DECISION_FORM_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        return {
            "ok": False,
            "error": (
                f"decision names a car, a train or a ledger row as its subject "
                f"({match.group(0).strip()!r}), so it records the work rather than "
                f"the decision. An ADR's decision states the RULE the work "
                f"established; restate it as that rule and move the car/train/"
                f"incident narrative into 'context'."
            ),
        }
    return None


# ── Body builder ───────────────────────────────────────────────────────────────


def _build_adr_body(
    adr_id: str,
    title: str,
    status: str,
    date: str,
    context: str,
    decision: str,
    rationale: str,
    alternatives: str,
    consequences: str,
    revisit_trigger: str,
    supersedes: str,
) -> str:
    """Return the per-ADR page CONTENT (H1 + Context/Decision/Consequences + bullets).

    The page_type `adr` requires sections Context / Decision / Consequences (for
    wiki_lint). The 9 flat bullets carry the full structured record; the three
    required ## sections satisfy the page_type template.
    """
    record = ADR(
        adr_id=adr_id,
        title=title,
        status=status,
        date=date,
        context=context,
        decision=decision,
        rationale=rationale,
        alternatives=alternatives,
        consequences=consequences,
        revisit_trigger=revisit_trigger,
        supersedes=supersedes,
    )
    bullets = record.to_markdown_body()
    return (
        f"# {adr_id}: {title}\n\n"
        f"{bullets}\n"
        f"## Context\n\n{context}\n\n"
        f"## Decision\n\n{decision}\n\n"
        f"## Consequences\n\n{consequences}\n"
    )


def _adr_tags(adr_id: str, status: str) -> list[str]:
    """Tags for a per-ADR page. adr_id is 'ADR-NNNN'."""
    nnnn = adr_id.split("-")[1]
    return ["adr", "decisions", f"adr-status:{status}", f"adr-{nnnn}"]


# ── Supersede handling ─────────────────────────────────────────────────────────


@observe(tier="stage", metric="tools.adr._parse_supersedes")
def _parse_supersedes(supersedes: str) -> list[str]:
    """Parse a 'supersedes' field into a list of 'ADR-NNNN' ids ([] for none)."""
    if not supersedes or supersedes.strip().lower() == "none":
        return []
    ids = re.findall(r"ADR-(\d{4})", supersedes)
    return [f"ADR-{n}" for n in ids]


@observe(exempt="pure list→string formatting; no I/O, no error branch worth spanning")
def _fmt_supersedes(value: Iterable[int] | str | None) -> str:
    """Render attached supersede-target ids as ``adr_add``'s own input format.

    Ledger task 195: the backend read ops now attach ``supersedes`` to every
    ``adr`` row as a LIST OF IDS (the relation lives in the ``adr_supersedes``
    join table — ``adr`` has no such column). The 7-key consumer shape is
    strings, and ``row.get("supersedes") or "none"`` would hand a raw list
    straight through.

    ``"ADR-0023, ADR-0024"`` is chosen because it ROUND-TRIPS through
    ``_parse_supersedes`` above, which is what ``adr_add(supersedes=...)``
    accepts — so a value read out of ``adr_list`` can be fed straight back in.
    A non-list value (a string already on the row, from a caller or a backend
    that formats its own) is passed through rather than iterated character by
    character.
    """
    if isinstance(value, str):
        return value or "none"
    if not value:
        return "none"
    return ", ".join(f"ADR-{int(v):04d}" for v in value)


@observe(exempt="pure list→string formatting; no I/O, no error branch worth spanning")
def _fmt_superseded_by(value: Iterable[int] | str | None) -> str:
    """Render attached superseder ids the way the field's only writer does.

    Ledger task 195, reverse direction. Bare zero-padded numbers joined by a
    comma with NO space — that is verbatim what ``_flip_superseded_target``'s
    caller builds (``f"{prev},{nnnn}"`` in ``_assemble_index_rows``), and it is
    the only code that has ever written this field. Deliberately NOT symmetric
    with ``_fmt_supersedes``: each half matches its own established producer,
    which is the conservative choice when both formats already exist in-tree.
    """
    if isinstance(value, str):
        return value or "-"
    if not value:
        return "-"
    return ",".join(f"{int(v):04d}" for v in value)


@observe(tier="stage", metric="tools.adr._flip_superseded_target")
def _flip_superseded_target(
    resolved: str, target_id: str, new_adr_id: str, *, project_id: str | None = None
) -> None:
    """Flip a superseded ADR page's status tag to 'superseded' (best-effort).

    Reads the target page canonically, replaces its adr-status:* tag with
    adr-status:superseded, and records the superseding id in an adr-superseded-by
    tag. Never raises — a missing target must not abort the new ADR write.

    Car W4 (ledger task 226): ``resolved`` is a DIRECTORY (``_resolve_project_root``
    output — a git worktree root), and it was being fed to
    ``_resolve_page_id_by_slug`` as the sole scoping key. ``project_id`` is the
    ADR-0233 key and is threaded separately: the two are NOT interchangeable
    even though ``adr_page_slug`` basenames whichever it is given — the live
    corpus has ``local/scratch`` stamped on rows at
    ``/home/max/quinyx/reusable-workflows-apim``, where the basenames differ.

    NOTE: this function has NO live caller. ``adr.py`` imports it only to
    re-export it in ``__all__``; ``supersede`` handling runs through the ledger
    (``adr_supersedes`` + the D23 status flip) instead. The parameter is added
    so the seam is correct if it is ever wired, not because a caller exists.
    """
    try:
        slug = adr_page_slug(resolved, target_id)
        page_id, page = _resolve_page_id_by_slug(slug, directory=resolved, project_id=project_id)
        if page_id is None or page is None:
            return
        tags = list(page.get("tags") or [])
        tags = [t for t in tags if not t.startswith("adr-status:")]
        tags.append("adr-status:superseded")
        nnnn = new_adr_id.split("-")[1]
        sb_tag = f"adr-superseded-by:{nnnn}"
        if sb_tag not in tags:
            tags.append(sb_tag)
        wiki_update(page_id, {"tags": tags})
    except Exception:  # noqa: BLE001 — supersede patch is best-effort
        return


# ── adr_add helpers (write-path assembly) ─────────────────────────────────────


def _canonical_adr_payload(
    slug: str,
    content: str,
    category: str,
    tags: list[str],
    directory: str,
    *,
    replace_slug: str | None = None,
) -> dict:
    """Build a canonical wiki_add payload for the ADR write path.

    ``title`` == ``slug`` so ``_slugify(title)`` yields the deterministic slug.
    ``page_type="adr"`` satisfies the ``_wiki_write_canonical``
    CANONICAL_PAGE_TYPES allowlist assertion. No ``force=True`` flag is set:
    Car C3 (0047 §7 D21) flipped the ``adr`` page_type to
    ``gate_mode="identity"`` so the drainer sim gate is a pass-through for
    canonical ADR pages (the slug IS the identity — a re-write of the same
    slug is an update, not a duplicate).
    """
    return {
        "wiki_schema_version": 2,
        "slug": slug,
        "title": slug,
        "content": content,
        "category": category,
        "tags": tags,
        "source_memory_ids": None,
        "confidence": "high",
        "append": False,
        "replace_slug": replace_slug,
        "directory_context": directory,
        "page_type": "adr",
    }


@observe(tier="stage", metric="tools.adr._assemble_index_rows")
def _assemble_index_rows(
    existing_index: str,
    new_row: dict,
    new_adr_id: str,
    target_ids: list[str],
    project_id: str | None = None,
) -> list[dict]:
    """Return the full ADR index row-set after adding ``new_row`` + supersede back-links.

    Car G (0047 §7): re-pointed off ``parse_index_rows`` (which read the
    legacy ``<project>-adr-index`` wiki page) onto the SQL ledger. The
    ledger is the authoritative index source post-seed (D35a); the legacy
    page is now ``superseded-by-ledger``-tagged (D35d). The function shape
    is preserved so callers (tests, Car F's re-point tests) that pin the
    return value keep working unchanged.

    Args:
        existing_index: legacy index-page content (unused post-G; kept for
            signature stability with the pre-G callers).
        new_row: the new row dict being inserted.
        new_adr_id: the new ADR id (e.g. ``"ADR-0042"``).
        target_ids: list of superseded ADR ids (``["ADR-0007"]``); their
            row-side status was flipped to ``superseded`` in Car F, so the
            back-link is computed in-memory from the ledger fetch.
        project_id: when supplied, the ledger fetch is scoped to this
            project; when ``None``, the function degrades to the new_row
            + target back-links only (test seam).

    Returns:
        The projected row dicts in the 7-key consumer shape (matches the
        pre-G ``parse_index_rows`` contract — see ``_row_to_adr_list_entry``
        for the mapping).
    """
    rows: list[dict] = []
    try:
        if project_id is not None:
            from yadgar.core.forward import _forward_admin  # noqa: PLC0415

            result = _forward_admin("list_adr_rows", {"project_id": project_id})
            ledger_rows = result.get("rows") if isinstance(result, dict) else []
            for r in ledger_rows or []:
                if not isinstance(r, dict):
                    continue
                rows.append(
                    {
                        "adr_id": f"ADR-{int(r['id']):04d}",
                        "status": r.get("status") or "open",
                        "date": r.get("decided_on") or "",
                        "title": r.get("title") or "",
                        "supersedes": r.get("supersedes") or "none",
                        "superseded_by": r.get("superseded_by") or "-",
                        "slug": r.get("body_slug") or "",
                    }
                )
    except Exception:  # noqa: BLE001 — degraded: project-scoped ledger not reachable
        rows = []

    # Append the new row last (Car F's contract — caller expects the new
    # row to be present in the returned list so a downstream render can
    # re-emit the index page if it wants to).
    rows.append(new_row)
    if not target_ids:
        return rows
    by_id = {r["adr_id"]: r for r in rows}
    nnnn = new_adr_id.split("-")[1]
    for tid in target_ids:
        row = by_id.get(tid)
        if row is None:
            continue
        row["status"] = "superseded"
        prev = row.get("superseded_by", "-")
        row["superseded_by"] = nnnn if prev in ("-", "") else f"{prev},{nnnn}"
    return rows
