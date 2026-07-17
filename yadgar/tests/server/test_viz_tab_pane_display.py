"""Regression guard for the v5.50.3 tab-pane visibility bug.

Tab visibility is owned by `.tab-pane { display: none }` + `.tab-pane.active
{ display: flex }`. A bare `#tab-<name> { display: ... }` rule has higher
specificity (id beats class) and forces that pane ALWAYS-visible, stacking
every tab down the page (shipped broken in v5.50.1/.2). jsdom unit tests do
not apply the CSS cascade, so this guard scans the CSS text directly: any
`display` on a `#tab-*` selector MUST be qualified with `.active`.
"""

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent.parent / "core" / "static"
_CSS_SOURCES = [
    _STATIC / "index.html",
    _STATIC / "bookmarks-tab.css",
    _STATIC / "traces-tab.css",
]
# Matches a CSS rule whose selector targets a tab-PANE id (not #tab-bar / links).
# Captures the selector and the rule body.
# `(?![\w-])` so the pane name must END here — a hyphenated child like
# `#tab-stats-body` (a legit content container) is NOT treated as the pane.
# `config-ref` is matched in full (the trailing `-ref` is part of the pane id,
# not a hyphenated child); the lookahead still fires after it.
_RULE = re.compile(
    r"(#tab-(?:home|stats|health|bookmarks|info|control|debug"
    r"|traces|config-ref|help|search)(?![\w-])[^\{]*?)\{([^}]*)\}",
    re.DOTALL,
)


_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _iter_tab_rules():
    for src in _CSS_SOURCES:
        if not src.exists():
            continue
        text = _COMMENT.sub("", src.read_text())  # drop CSS comments first
        for m in _RULE.finditer(text):
            yield src.name, m.group(1).strip(), m.group(2)


def test_no_unconditional_display_on_tab_pane_ids():
    """A `#tab-*` selector may only set `display` when scoped to `.active`."""
    offenders = []
    for fname, selector, body in _iter_tab_rules():
        if re.search(r"(^|[;{\s])display\s*:", body) is None:
            continue
        # display present — only allowed if the selector is .active-qualified
        if ".active" not in selector:
            offenders.append(f"{fname}: `{selector} {{ … display … }}`")
    assert not offenders, (
        "Unconditional `display` on #tab-* ids forces the pane always-visible "
        "(outranks `.tab-pane`/`.tab-pane.active`). Scope to `.active`:\n  "
        + "\n  ".join(offenders)
    )


def test_active_pane_rule_exists():
    """The `.tab-pane.active` show rule must exist (the toggle's positive half)."""
    index = (_STATIC / "index.html").read_text()
    assert re.search(r"\.tab-pane\.active\s*\{[^}]*display\s*:", index), (
        ".tab-pane.active display rule missing — tabs cannot be shown"
    )
    assert re.search(r"\.tab-pane\s*\{[^}]*display\s*:\s*none", index), (
        ".tab-pane { display: none } base rule missing — inactive tabs cannot hide"
    )
