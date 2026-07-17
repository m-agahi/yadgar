"""Car D — #tab-search registration guard (audit landmine).

A new viz tab must be registered in ALL of:
  1. the index.html ``_getActiveTab`` fallback ``_VALID`` set
  2. the index.html ``_switchTab`` fallback ``_VALID`` set
  3. the tabs.js ``VALID_TABS`` module set
  4. the nav tree HTML (a data-tab="search" leaf/link)
  5. the ``#tab-search`` pane element
  6. the test_viz_tab_pane_display display-guard regex

Miss any and the tab silently falls back to 'home' or the CSS guard goes blind.
This test asserts every registration point contains ``search``.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parent.parent.parent / "core" / "static"
_INDEX = _STATIC / "index.html"
_TABS = _STATIC / "tabs.js"


def test_valid_sets_contain_search():
    """Both inline _VALID Sets in index.html must include 'search'."""
    text = _INDEX.read_text()
    valid_sets = re.findall(r"const _VALID = new Set\(\[([^\]]*)\]\)", text)
    assert len(valid_sets) >= 2, f"expected >=2 inline _VALID sets, found {len(valid_sets)}"
    for i, body in enumerate(valid_sets):
        assert "'search'" in body, f"inline _VALID set #{i} missing 'search': {body}"


def test_tabs_js_valid_tabs_contains_search():
    """tabs.js VALID_TABS module Set must include 'search'."""
    text = _TABS.read_text()
    m = re.search(r"export const VALID_TABS = new Set\(\[([^\]]*)\]\)", text)
    assert m is not None, "VALID_TABS declaration not found in tabs.js"
    assert "'search'" in m.group(1), f"tabs.js VALID_TABS missing 'search': {m.group(1)}"


def test_nav_tree_has_search_link():
    """The nav HTML must carry a data-tab=\"search\" anchor (routable link)."""
    text = _INDEX.read_text()
    assert 'data-tab="search"' in text, 'no data-tab="search" nav link in index.html'


def test_search_pane_exists():
    """The #tab-search pane element must exist."""
    text = _INDEX.read_text()
    assert 'id="tab-search"' in text, "#tab-search pane element missing"


def test_display_guard_regex_covers_search():
    """The test_viz_tab_pane_display _RULE regex must include 'search'."""
    guard = (Path(__file__).resolve().parent / "test_viz_tab_pane_display.py").read_text()
    m = re.search(r"#tab-\(\?:([^)]*)\)", guard)
    assert m is not None, "could not locate the _RULE tab-id alternation"
    assert "search" in m.group(1), f"display-guard regex missing 'search': {m.group(1)}"
