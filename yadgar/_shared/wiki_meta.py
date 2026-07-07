"""Wiki page-type registry and templates (v5.53.2 — Phase B-schema).

PAGE_TYPES maps page_type string → required section headings (case-insensitive
for lint matching). Templates are kept small (2-4 sections each) to avoid
over-constraining the wiki corpus.

Design:
- page_type is OPTIONAL on all pages. Pages without page_type are never
  format-checked by wiki_lint.
- Required sections must appear as ## headings (case-insensitive) in the page
  content for wiki_lint format checks to pass.
- 6 types cover ~90% of the corpus (fn-, mod-, services-, arch-,
  decision-, analysis-* slug prefixes identified in 5.53.2 audit).

WIKI_SCHEMA_VERSION is stamped on new pages at write time. Existing pages
that predate this field are treated as schema_version=0 / untyped.
"""

from yadgar._shared.observability.observe import observe

WIKI_SCHEMA_VERSION: int = 1

#: Registry of page types → required markdown section headings.
#: Keys are canonical page_type values (lowercase, hyphen-separated).
#: Values are lists of heading texts (case-insensitive match at lint time).
PAGE_TYPES: dict[str, list[str]] = {
    "function": [
        "Purpose",
        "Signature",
        "Behaviour",
    ],
    "module": [
        "Purpose",
        "Exports",
        "Design",
    ],
    "service": [
        "Purpose",
        "Interface",
        "Dependencies",
    ],
    "architecture": [
        "Overview",
        "Components",
    ],
    "decision": [
        "Context",
        "Decision",
        "Consequences",
    ],
    "analysis": [
        "Summary",
        "Findings",
    ],
    "agent_prompt": [
        "Purpose",
        "Prompt",
    ],
}


@observe(tier="stage")
def check_page_type_format(slug: str, page_type: str, content: str) -> list[dict]:
    """Return missing-section issues for a typed wiki page (v5.53.2).

    Called by wiki_lint. Returns list of issue dicts (empty = no violations).
    Pages with unknown page_type return []. Case-insensitive heading match.
    Heading extraction is delegated to the caller via the content string —
    the caller (wiki.py lint()) uses _find_section_headings which skips fenced blocks.
    """
    if page_type not in PAGE_TYPES:
        return []
    required = PAGE_TYPES[page_type]
    # Simple regex-based heading extraction (headings only, no fenced-block skip needed
    # for lint — complex code blocks are unusual in wiki summaries; the caller's
    # _find_section_headings handles fenced blocks when content warrants it).
    import re as _re  # noqa: PLC0415

    heading_re = _re.compile(r"^#{2,3} (.+)", _re.MULTILINE)
    present_lower = {m.group(1).strip().lower() for m in heading_re.finditer(content)}
    issues = []
    for req in required:
        if req.lower() not in present_lower:
            issues.append(
                {
                    "page": slug,
                    "severity": "warning",
                    "type": "missing_section",
                    "message": f"page_type='{page_type}' requires section '## {req}' — not found",
                }
            )
    return issues
