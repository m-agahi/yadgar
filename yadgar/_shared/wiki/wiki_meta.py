"""Wiki page-type registry and templates (v5.53.2 — Phase B-schema).

Stage 3 externalization (2026-07-10): the schema itself lives in the packaged
resource yadgar/_shared/schemas/wiki_page_types.yaml — loaded here at import
via importlib.resources. This module keeps ZERO schema literals; edit the yaml,
not this file.

PAGE_TYPE_SCHEMAS maps page_type string → full schema dict (required /
optional / metadata). PAGE_TYPES keeps the historical dict[str, list[str]]
required-sections shape for existing consumers (wiki lint, tests).

Design:
- page_type is OPTIONAL on all pages. Pages without page_type are never
  format-checked by wiki_lint.
- Required sections must appear as ## headings (case-insensitive) in the page
  content for wiki_lint format checks to pass. Lint is ADVISORY — wiki_add
  never rejects a write on page_type/template mismatch.
- Optional sections are documented shape only — never linted against.

WIKI_SCHEMA_VERSION is stamped on new pages at write time. Existing pages
that predate this field are treated as schema_version=0 / untyped.
"""

from yadgar._shared.observability.observe import observe

# ── Agent-library page types (ADR-0209) ──────────────────────────────────────
# The prompt library used ONE page_type (``agent_prompt``) for two families
# discriminated only by slug prefix and tags, while ADR-0198 splits them at the
# row level and ADR-0208 gives them different governance. page_type is the
# policy lever (``get_policy(page_type).recall_disposition``), so the split has
# to be a type, not a string prefix.
#
# These constants live here — NOT in policy.py and NOT duplicated per side —
# because both core (``tools/agent_prompts.py``) and backend
# (``admin_exec/wiki.py``, ``retrieval/providers/wiki.py``) need them and the
# import-linter contract forbids a backend→core edge. ``_shared`` is the one
# place both may import (contrast the _TOC_SLUG / _TOC_ROW_RE pair, which is
# hand-mirrored core↔backend).

#: Dispatch-pattern pages: ``agent-prompt-<pattern>``.
PAGE_TYPE_AGENT_PATTERN = "agent_pattern"

#: Cross-cutting rule pages: ``agent-discipline-<name>``. Per ADR-0209 the
#: prelude CONTRACT lives inside this type too (distinguished by ADR-0198's
#: ``always_applied`` flag), rather than being promoted to a third type.
PAGE_TYPE_AGENT_DISCIPLINE = "agent_discipline"

#: The library index (``agent-prompt-toc``). Deliberately absent from
#: wiki_page_types.yaml: it is a link list with no Purpose/Prompt sections, and
#: ``check_page_type_format`` returns [] for unregistered types — so registering
#: it in POLICY_BY_TYPE alone buys the recall exclusion (task 0134) with no
#: permanent lint warning.
PAGE_TYPE_AGENT_INDEX = "agent_index"

#: Pre-ADR-0209 value. Retained for rows on installs that have not yet run
#: migration 028 — they must keep resolving to the same routing policy.
PAGE_TYPE_AGENT_PROMPT_LEGACY = "agent_prompt"

#: Task list pages. Car C7 (0047, absorbing C8 item 4) flipped this type from
#: C2's ``downweight`` to ``recall_disposition="exclude"`` with
#: ``opt_in_tag=None``. The downweight it used to carry was applied as a
#: MULTIPLY on a score containing a raw cross-encoder logit, which is commonly
#: negative — so the penalty inverted into a promotion. Excluding is also the
#: shape C7's stage-1 WHERE can act on: an excluded type is never fetched, so
#: it cannot consume a pool slot. The page stays reachable by exact key
#: (``wiki_read`` / ``wiki_get`` / ``wiki_list``, and the session-start restore
#: nudge), which is how every real consumer reads it — exclusion is a SEARCH
#: rule only. Car D (task tools) + Car E (task seed) move tasks to SQL and
#: delete the task-list markdown page; surviving ``task_list`` pages
#: (e.g. re-created post-spine) inherit the exclusion for free.
PAGE_TYPE_TASK_LIST = "task_list"


@observe(tier="stage")
def _load_page_type_schemas() -> dict:
    """Load + parse schemas/wiki_page_types.yaml (packaged resource).

    Read via importlib.resources so it works both from source and from an
    installed wheel. Uses ruamel.yaml — yadgar's only declared YAML
    dependency (see pyproject). PyYAML is NOT used here: it is not a
    declared dependency (present only transitively via the optional `ml`
    extra), so preferring it would make this loader's behavior depend on
    which packages happen to be installed (v5.169.1 fix).
    """
    from importlib.resources import files  # noqa: PLC0415

    from ruamel.yaml import YAML  # noqa: PLC0415

    text = files("yadgar._shared").joinpath("schemas").joinpath("wiki_page_types.yaml").read_text()
    return YAML(typ="safe").load(text)


_SCHEMA_DATA: dict = _load_page_type_schemas()

WIKI_SCHEMA_VERSION: int = int(_SCHEMA_DATA["schema_version"])

#: Full per-type schema dicts: {page_type: {required: [...], optional: [...], metadata: {...}}}.
PAGE_TYPE_SCHEMAS: dict[str, dict] = _SCHEMA_DATA["page_types"]

#: Registry of page types → required markdown section headings (historical shape).
#: Keys are canonical page_type values (lowercase, hyphen-separated).
#: Values are lists of heading texts (case-insensitive match at lint time).
PAGE_TYPES: dict[str, list[str]] = {
    page_type: list(schema.get("required", [])) for page_type, schema in PAGE_TYPE_SCHEMAS.items()
}


@observe(tier="stage")
def check_page_type_format(slug: str, page_type: str, content: str) -> list[dict]:
    """Return missing-section issues for a typed wiki page (v5.53.2).

    Called by wiki_lint. Returns list of issue dicts (empty = no violations).
    Pages with unknown page_type return []. Case-insensitive heading match.
    Optional sections (PAGE_TYPE_SCHEMAS[type]["optional"]) are never checked —
    lint stays ADVISORY on the richer agent_prompt schema (Stage 3).
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
