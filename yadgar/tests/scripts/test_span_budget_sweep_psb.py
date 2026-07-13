"""P-SB §3.4 Commit B (scope-bounded sweep) — _cosine_similarity span=False flip.

ADR-0085 Commit B seeds ``_span_budget`` with EXACTLY ONE fn —
``server_helpers:_cosine_similarity`` — and flips its ``@observe`` decorator to
``span=False`` in the SAME change so the I33 v2 lint (which hard-fails a
_span_budget fn that opens a per-call span) stays green on the tree.

SCOPE-BOUND (memory 531809 / v5.105): the advisory loop report catalogues other
offenders but this car flips NONE of them — a codebase-wide @observe sweep has
broad blast radius (~11 decorator-contract bugs last time, wedged CI). Broader
sweep is a dedicated follow-up car.

These are source-level assertions: the flip is a one-line decorator edit and the
seed is one allowlist entry; assert both are present + consistent.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COSINE_FQ = "server_helpers:_cosine_similarity"


def _cosine_observe_kwargs() -> dict[str, object]:
    path = _REPO_ROOT / "yadgar" / "_shared" / "server_helpers" / "server_helpers.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_cosine_similarity":
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = dec.func
            dec_name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if dec_name != "observe":
                continue
            return {
                kw.arg: kw.value.value for kw in dec.keywords if isinstance(kw.value, ast.Constant)
            }
    raise AssertionError("no @observe decorator on _cosine_similarity")


def test_cosine_similarity_carries_span_false():
    kwargs = _cosine_observe_kwargs()
    assert kwargs.get("span") is False, (
        f"_cosine_similarity must carry @observe(..., span=False), got span={kwargs.get('span')!r}"
    )
    # Metric + hot tier are unchanged — span=False keeps metric/attributes on the
    # enclosing span, only suppresses the per-call span.
    assert kwargs.get("tier") == "hot", f"tier must stay 'hot', got {kwargs.get('tier')!r}"
    assert kwargs.get("metric") == "tools.project._cosine_similarity"


def test_cosine_similarity_in_span_budget():
    allowlist = json.loads((_REPO_ROOT / ".observe-allowlist.json").read_text(encoding="utf-8"))
    span_budget = allowlist.get("_span_budget", {})
    assert _COSINE_FQ in span_budget, (
        f"{_COSINE_FQ} must be seeded in _span_budget; keys: {list(span_budget)}"
    )
    entry = span_budget[_COSINE_FQ]
    assert isinstance(entry, dict) and len(entry.get("rationale", "").strip()) >= 40, (
        "span_budget entry must carry a >=40-char rationale"
    )
