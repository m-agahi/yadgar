#!/usr/bin/env python3
"""Validate PR title and body against the repository's pull-request template.

Exit codes:
    0 — PR metadata is valid
    1 — one or more validation errors found

Environment variables (set by the CI workflow via ``env:``):
    PR_TITLE          — pull-request title string
    PR_BODY           — pull-request body / description string
    GITHUB_SERVER_URL — set by both GitHub Actions and Forgejo Actions runners to
                         their own instance URL; used by ``template_path()`` to name
                         the PR template that actually applies to THIS run (see below).
"""

import os
import re
import sys
from pathlib import Path
from typing import TypedDict

# Required section names in the PR body (case-insensitive match).
# Detected via EITHER "## <Name>" OR "<summary>...<Name>...</summary>".
REQUIRED_SECTIONS = ("Summary", "What", "Why", "Notes", "Test plan")

# This repo ships the PR template TWICE — once per forge (dual CI, ADR-0178):
#   .forgejo/PULL_REQUEST_TEMPLATE.md — Forgejo's native template location.
#   .github/PULL_REQUEST_TEMPLATE.md  — GitHub's native template location.
# Both must define the same REQUIRED_SECTIONS; see check_template_drift() below.
_FORGEJO_TEMPLATE_PATH = ".forgejo/PULL_REQUEST_TEMPLATE.md"
_GITHUB_TEMPLATE_PATH = ".github/PULL_REQUEST_TEMPLATE.md"
_GITHUB_SERVER_URL = "https://github.com"

# Matches "## <Name>" header lines (case-insensitive via re.IGNORECASE).
_H2_PATTERN = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# Matches "<summary>...<Name>...</summary>" — tolerates inline tags + whitespace.
# Non-greedy to avoid swallowing across multiple <details> blocks.
_SUMMARY_TAG_PATTERN = re.compile(
    r"<summary[^>]*>\s*(?:<[^>]+>\s*)*(.+?)\s*(?:<[^>]+>\s*)*</summary>",
    re.IGNORECASE,
)

# Strips HTML comments (must run BEFORE stripping tags — non-greedy, DOTALL).
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Strips HTML tags after comments are removed.
_TAG_RE = re.compile(r"<[^>]+>")


def template_path() -> str:
    """Return the PR-template path relevant to the forge running THIS check.

    Both GitHub Actions and Forgejo Actions populate ``GITHUB_SERVER_URL`` (Forgejo
    mirrors the GitHub Actions env-var contract) but point it at their own instance:
    GitHub Actions always sets it to ``https://github.com``; a Forgejo/Codeberg
    instance sets it to that instance's own URL. Anything other than the literal
    GitHub URL is treated as Forgejo — including unset, for a bare `git worktree`
    local run, since Forgejo is this repo's non-default forge (ADR-0178 made GitHub
    primary after the Codeberg-to-GitHub move) and the CI-relevant case (this var
    IS set) is what matters.

    Hardcoding one path here (the pre-existing bug) means the error message can
    name a template file the failing job never read from — see task 301 / the PR-62
    incident, where the CI job that ran was ``.github/workflows/validate.yml`` but
    every error still pointed at ``.forgejo/PULL_REQUEST_TEMPLATE.md``.
    """
    server_url = os.environ.get("GITHUB_SERVER_URL", "").rstrip("/")
    if not server_url:
        return _GITHUB_TEMPLATE_PATH
    if server_url == _GITHUB_SERVER_URL:
        return _GITHUB_TEMPLATE_PATH
    return _FORGEJO_TEMPLATE_PATH


# Stable alias captured before validate_pr_metadata's `template_path` parameter
# shadows the module-level function of the same name in that function's scope.
_resolve_template_path = template_path


def check_template_drift(repo_root: Path | None = None) -> str | None:
    """Return a warning string if the two forge PR templates have diverged, else None.

    Dual CI (ADR-0178) means this repo carries two independent copies of the PR
    template, one per forge. REQUIRED_SECTIONS is validated against whichever one
    is actually live for a given run's forge (see template_path()); nothing
    enforces the two files stay identical, so a hand-edit to one and not the
    other can silently desync required-section names between forges. This is a
    cheap tripwire (two file reads + a string compare) so that drift is visible
    in every CI log instead of silent until it breaks a PR on the un-edited forge.

    Non-fatal by design: this does not add an entry to validate_pr_metadata()'s
    error list and never affects the exit code. Turning template drift into a
    hard gate failure would fail PRs for a repo-wide condition unrelated to
    their own body content — out of scope for what this task asked for.
    """
    root = repo_root if repo_root is not None else Path(__file__).resolve().parent.parent
    forgejo_file = root / _FORGEJO_TEMPLATE_PATH
    github_file = root / _GITHUB_TEMPLATE_PATH
    if not forgejo_file.is_file() or not github_file.is_file():
        return None
    forgejo_text = forgejo_file.read_text(encoding="utf-8")
    github_text = github_file.read_text(encoding="utf-8")
    if forgejo_text == github_text:
        return None
    return (
        f"WARNING: {_FORGEJO_TEMPLATE_PATH} and {_GITHUB_TEMPLATE_PATH} have diverged. "
        "Keep the two forge PR templates identical (dual CI, ADR-0178) — "
        "REQUIRED_SECTIONS is validated against whichever one matches the forge "
        "running this check, so drift here can silently change which PR bodies pass "
        "depending on which forge they were opened against."
    )


class _SectionEntry(TypedDict):
    """One detected '## Name' heading or <summary>Name</summary> construct."""

    name: str  # original case, as written in the body
    kind: str  # "heading" | "summary"
    start: int  # char offset where the construct begins
    end: int  # char offset where the construct ends (this section's content starts here)
    line: int  # 1-indexed line number of `start`


class _SectionMapEntry(_SectionEntry):
    """A ``_SectionEntry`` plus the resolved end-of-content boundary."""

    content_end: int | None  # start of the NEXT section (or None = EOF)


def _line_of(body: str, pos: int) -> int:
    """Return the 1-indexed line number of character offset ``pos`` in ``body``."""
    return body.count("\n", 0, pos) + 1


def _find_sections(body: str) -> list[_SectionEntry]:
    """Return every detected heading/summary construct, sorted by start position.

    Collects both H2 headers ("## Name") and <summary>…</summary> tags,
    de-duplicates by name (first occurrence wins). Every detected construct is
    kept — not just the ones matching REQUIRED_SECTIONS — so callers can report
    "here's what the body actually has" when a required section is absent
    (the near-miss case: an author labels a block "The cars" instead of "What").

    start/end are the detecting construct's span (used as the content window's
    boundary for the PRIOR/NEXT section); content for a given section begins
    at its own "end" and runs to the next entry's "start" (or EOF).
    """
    found: dict[str, _SectionEntry] = {}  # name_lower → entry

    for m in _H2_PATTERN.finditer(body):
        name = m.group(1).strip()
        key = name.lower()
        if key not in found:
            found[key] = {
                "name": name,
                "kind": "heading",
                "start": m.start(),
                "end": m.end(),
                "line": _line_of(body, m.start()),
            }

    for m in _SUMMARY_TAG_PATTERN.finditer(body):
        # Strip any inline tags from the captured label to get the plain name.
        raw = m.group(1)
        name = _TAG_RE.sub("", raw).strip()
        key = name.lower()
        if key not in found:
            found[key] = {
                "name": name,
                "kind": "summary",
                "start": m.start(),
                "end": m.end(),
                "line": _line_of(body, m.start()),
            }

    return sorted(found.values(), key=lambda entry: entry["start"])


def _clean(text: str) -> str:
    """Strip HTML comments (first), then HTML tags, return remaining text."""
    text = _COMMENT_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    return text


def _section_content(body: str, content_start: int, content_end: int | None) -> str:
    """Extract raw body slice from content_start to content_end, then clean it."""
    raw = body[content_start:content_end]
    return _clean(raw)


def _describe_found_sections(found_sections: list[_SectionEntry]) -> str:
    """Render every detected heading/summary construct as a diagnostic list.

    Lets the author see a near-miss (a label like "The cars" where "What" was
    required) without reading this script's source or the template file.
    """
    if not found_sections:
        return "none found — body has no '##' headings and no <summary> tags at all"
    parts = []
    for entry in found_sections:
        marker = "##" if entry["kind"] == "heading" else "<summary>"
        parts.append(f"{marker} {entry['name']!r} (line {entry['line']})")
    return "; ".join(parts)


def _missing_section_error(
    section_name: str, found_sections: list[_SectionEntry], tmpl_path: str
) -> str:
    return (
        f"PR body missing required section '{section_name}': searched for a "
        f"'## {section_name}' heading or a <summary>{section_name}</summary> block "
        f"(case-insensitive) and found neither anywhere in the body. "
        f"Headings/summary blocks actually found: {_describe_found_sections(found_sections)}. "
        f"Use the exact section names from the PR template ({tmpl_path})."
    )


def _short_section_error(
    section_name: str, non_ws: int, entry: _SectionMapEntry, tmpl_path: str
) -> str:
    where = (
        f"'## {entry['name']}' (line {entry['line']})"
        if entry["kind"] == "heading"
        else f"<summary>{entry['name']}</summary> (line {entry['line']})"
    )
    return (
        f"PR body section '{section_name}' has too little content "
        f"({non_ws} non-whitespace chars, need >=20) — found at {where}. "
        f"Use the PR template ({tmpl_path})."
    )


def validate_pr_metadata(title: str, body: str, template_path: str | None = None) -> list[str]:
    """Return a list of human-readable error strings; empty list means valid.

    Args:
        title: The pull-request title (may include surrounding whitespace).
        body:  The pull-request body / description.
        template_path: PR-template path to name in error messages. Defaults to
            ``template_path()`` (module-level function of the same name,
            resolved from ``GITHUB_SERVER_URL`` at call time) — pass an
            explicit value to pin it (tests; or a caller that already knows
            which forge it's running on).
    """
    tmpl_path = template_path if template_path is not None else _resolve_template_path()
    errors: list[str] = []
    title_stripped = title.strip()

    # -- Title checks --------------------------------------------------------

    if len(title_stripped) < 8:
        errors.append(f"PR title too short (need >=8 chars): {title_stripped!r}")

    if " " not in title_stripped:
        errors.append(f"PR title must be descriptive (multiple words): {title_stripped!r}")

    # -- Body section checks -------------------------------------------------

    # content_start = end of this section's header construct.
    # content_end   = start of the NEXT section's header construct (or None = EOF).
    found_sections = _find_sections(body)
    found_map: dict[str, _SectionMapEntry] = {}
    for i, entry in enumerate(found_sections):
        next_start = found_sections[i + 1]["start"] if i + 1 < len(found_sections) else None
        found_map[entry["name"].lower()] = {**entry, "content_end": next_start}

    for section_name in REQUIRED_SECTIONS:
        key = section_name.lower()
        if key not in found_map:
            errors.append(_missing_section_error(section_name, found_sections, tmpl_path))
            continue

        entry = found_map[key]
        content = _section_content(body, entry["end"], entry["content_end"])
        non_ws = len(re.sub(r"\s", "", content))
        if non_ws < 20:
            errors.append(_short_section_error(section_name, non_ws, entry, tmpl_path))

    return errors


def main() -> int:
    """Read PR_TITLE/PR_BODY from the environment, validate, and report."""
    pr_title = os.environ.get("PR_TITLE", "")
    pr_body = os.environ.get("PR_BODY", "")

    drift_warning = check_template_drift()
    if drift_warning:
        print(drift_warning, file=sys.stderr)

    errors = validate_pr_metadata(pr_title, pr_body)
    if errors:
        for msg in errors:
            print(f"ERROR: {msg}", file=sys.stderr)
        return 1

    print("PR metadata OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
