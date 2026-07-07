# ruff: noqa: PLR0913  — adr_add / _build_adr_body intentionally have 11 params (ADR schema
#   has 10 mandatory content fields; FastMCP derives JSON Schema from flat keyword args).
#   Collapsing into **kwargs loses schema enforcement. PERMANENT — see .complexity-allowlist.json.
"""ADR (Architecture Decision Record) MCP tool registrations (car #12 — improvement-train).

One tool:
  adr_add — create a new ADR entry in the project's ADR log wiki page.

The ADR log is a wiki page named `<project>-adr-log`, scoped to the project's
default branch (branch_hint=default_branch). IDs are assigned sequentially by
scanning `## ADR-NNNN:` headers in the log.

If the log does not exist, `adr_add` creates it via `wiki_add(wait=True)`.
If the log exists, `adr_add` appends a new `## ADR-NNNN:` section via
`wiki_append_section(..., position="new_section_bottom")`.

Decisions:
- @_tool(power=True) — write tool convention (matches wiki_append_section).
- wait=True on wiki_add create-path — ensures read-your-writes consistency for
  sequential ID assignment (avoids race when caller immediately adds ADR-0002).
- ID scan anchored to ``^## ADR-(\\d{4})`` (re.MULTILINE) — body text refs ignored.
- branch_hint=default_branch on both read and write — ADR log is canonical/global.
"""

from __future__ import annotations

import os
import re

from yadgar._shared.models import ADR
from yadgar._shared.observability.observe import observe
from yadgar.core.server._app import _tool
from yadgar.core.server.tools.project import _get_default_branch, _resolve_project_root

# Imported lazily at call-time to match the pattern in other tool modules,
# but we list them here for clarity and to enable patching in tests.
from yadgar.core.server.tools.wiki import wiki_add, wiki_append_section, wiki_read

# Valid ADR status values.
_VALID_STATUSES: frozenset[str] = frozenset(
    {"open", "accepted", "superseded", "rejected", "deprecated"}
)

# Regex that matches ## ADR-NNNN at column 0 (header-only scan).
_ADR_HEADER_RE = re.compile(r"^## ADR-(\d{4})", re.MULTILINE)

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


def adr_log_slug(resolved: str) -> str:
    """Return the ADR log wiki slug for an already-resolved project root.

    Exported helper (car #13) — shared between adr_add and _build_adr_log in project.py.

    Args:
        resolved: Absolute path to the project root (already resolved by
                  _resolve_project_root in project.py).

    Returns:
        Slug string, e.g. "yadgar-adr-log" for /home/user/projects/yadgar.
    """
    return f"{os.path.basename(resolved)}-adr-log"


def parse_adr_ids(content: str) -> list[str]:
    """Extract ADR IDs from wiki page content, sorted descending (most recent first).

    Exported helper (car #13) — shared between adr_add and _build_adr_log in project.py.

    Args:
        content: Full text of an ADR log wiki page.

    Returns:
        List of "ADR-NNNN" strings in descending order, e.g. ["ADR-0003", "ADR-0002", "ADR-0001"].
        Empty list if no ADR headings found.
    """
    matches = _ADR_HEADER_RE.findall(content)
    return [f"ADR-{int(n):04d}" for n in sorted(matches, key=int, reverse=True)]


@observe(tier="stage", name="tools.adr._next_adr_id")
def _next_adr_id(content: str) -> str:
    """Return the next sequential ADR id (e.g. 'ADR-0004') from log content.

    Scans only ## ADR-NNNN headers at column 0 (re.MULTILINE); ignores any
    ADR references in body text.  Returns 'ADR-0001' when log is empty/absent.
    """
    matches = _ADR_HEADER_RE.findall(content)
    if not matches:
        return "ADR-0001"
    max_n = max(int(m) for m in matches)
    return f"ADR-{max_n + 1:04d}"


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
) -> tuple[str, str]:
    """Return (section_heading, section_body) for wiki_append_section.

    section_heading is the bare heading text (without ##) — the store adds ## automatically.
    section_body is the markdown content under the heading.
    """
    section_heading = f"{adr_id}: {title}"
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
    return section_heading, record.to_markdown_body()


@_tool(power=True)
def adr_add(
    directory: str,
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
) -> dict:
    """Create a new Architecture Decision Record (ADR) in the project's ADR log.

    The ADR log is a wiki page `<project>-adr-log` scoped to the project's default
    branch.  If it does not exist, this tool creates it.  IDs are assigned
    sequentially by scanning existing ## ADR-NNNN headers.

    Args:
        directory: Absolute path to the project root.
        title: Short human-readable title (e.g. "Use SurrealDB for storage").
        status: One of: open, accepted, superseded, rejected, deprecated.
        date: ISO date string (e.g. "2026-06-25").
        context: Background / problem statement.
        decision: The decision that was made.
        rationale: Why this decision was made.
        alternatives: Alternatives that were considered.
        consequences: Known / expected consequences.
        revisit_trigger: Condition that would trigger revisiting this decision.
        supersedes: "none" or a comma-separated list of superseded ADR IDs (e.g. "ADR-0002").

    Returns:
        {"adr_id": "ADR-NNNN", "version": M} on success.
        {"error": "...", "ok": False} on validation failure or storage error.
    """
    # ── Validation ─────────────────────────────────────────────────────────────
    # Validate all required content fields first (before any storage access).
    provided: dict[str, str] = {
        "title": title,
        "status": status,
        "date": date,
        "context": context,
        "decision": decision,
        "rationale": rationale,
        "alternatives": alternatives,
        "consequences": consequences,
        "revisit_trigger": revisit_trigger,
        "supersedes": supersedes,
    }
    for field in _REQUIRED_FIELDS:
        val = provided.get(field)
        if not val or not str(val).strip():
            return {
                "ok": False,
                "error": f"missing required field: {field!r}",
            }
    if status not in _VALID_STATUSES:
        return {
            "ok": False,
            "error": (f"invalid status {status!r}; must be one of {sorted(_VALID_STATUSES)}"),
        }

    # ── Resolve project root and ADR log slug ───────────────────────────────────
    try:
        resolved = _resolve_project_root(directory)
    except Exception as exc:
        return {"ok": False, "error": f"cannot resolve project root: {exc}"}

    project_name = os.path.basename(resolved)
    slug = adr_log_slug(resolved)
    default_branch = _get_default_branch(resolved)

    # ── Read existing log (branch-pinned to default branch) ─────────────────────
    log_page = wiki_read(slug, directory=resolved, branch_hint=default_branch)
    log_exists = "error" not in log_page
    existing_content = log_page.get("content", "") if log_exists else ""

    # ── Assign next ID ──────────────────────────────────────────────────────────
    adr_id = _next_adr_id(existing_content)

    # ── Build section content ───────────────────────────────────────────────────
    section_heading, section_body = _build_adr_body(
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

    # ── Append or create ────────────────────────────────────────────────────────
    if log_exists:
        # Append new section to existing log.
        append_result = wiki_append_section(
            slug,
            section_heading=section_heading,
            content=section_body,
            position="new_section_bottom",
            directory=resolved,
            branch_hint=default_branch,
        )
        if "error" in append_result:
            return {
                "ok": False,
                "error": f"wiki_append_section failed: {append_result['error']}",
                "adr_id": adr_id,
            }
        new_version = append_result.get("new_version")
        result: dict = {"adr_id": adr_id}
        if new_version is not None:
            result["version"] = new_version
        return result
    else:
        # Create the log page with the first ADR entry.
        log_title = f"{project_name} ADR Log"
        full_content = (
            f"# {log_title}\n\n"
            f"Architecture Decision Records for `{project_name}`.\n\n"
            f"---\n\n"
            f"## {section_heading}\n\n"
            f"{section_body}"
        )
        create_result = wiki_add(
            title=log_title,
            content=full_content,
            category="reference",
            tags=["adr", "decisions"],
            directory=resolved,
            branch_hint=default_branch,
            wait=True,  # synchronous — ensures read-your-writes for sequential IDs
        )
        if create_result.get("stored") is False:
            return {
                "ok": False,
                "error": f"wiki_add failed: {create_result.get('reason', 'unknown')}",
                "adr_id": adr_id,
            }
        return {"adr_id": adr_id}
