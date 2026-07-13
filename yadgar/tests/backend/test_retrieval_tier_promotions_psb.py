"""P-SB tier promotions (plan §3.3): encode_query + score_ce_cached hot -> stage.

Two live-recall-path stages were ``@observe(tier="hot")`` — hot emits NOTHING
(span-open/close only), so query-embed and total-CE were invisible on the stage
histogram. Promoting them to ``tier="stage"`` makes each emit exactly one
``yadgar_observe_stage_duration_seconds{stage=...}`` sample per call — the #50 CE
source and criterion-1 stages.

These are decorator-contract tests: assert the ``@observe`` decorator on each
target function carries ``tier="stage"`` (and the unchanged ``metric=`` label) by
parsing the module source with ``ast``. A source-level assertion is the right
granularity — it pins the one-line promotion without constructing a heavy
Retriever / CrossEncoder object, and it fails loudly if a future edit reverts the
tier.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _observe_kwargs_for_function(module_path: Path, func_name: str) -> dict[str, object]:
    """Return the resolved literal kwargs of the @observe decorator on `func_name`.

    Searches every function (including methods nested in classes) whose name
    matches. Raises AssertionError if the function or its @observe decorator is
    absent.
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func_name:
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = dec.func
            dec_name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if dec_name != "observe":
                continue
            kwargs: dict[str, object] = {}
            for kw in dec.keywords:
                if kw.arg is None:
                    continue
                if isinstance(kw.value, ast.Constant):
                    kwargs[kw.arg] = kw.value.value
            return kwargs
    raise AssertionError(f"no @observe decorator found on {func_name} in {module_path}")


def test_encode_query_promoted_to_stage():
    path = _REPO_ROOT / "yadgar" / "backend" / "retrieval" / "scoring.py"
    kwargs = _observe_kwargs_for_function(path, "_encode_vector_query")
    assert kwargs.get("tier") == "stage", (
        f"retrieval.vector.encode_query must be tier='stage', got {kwargs.get('tier')!r}"
    )
    assert kwargs.get("metric") == "retrieval.vector.encode_query", (
        f"metric label must be unchanged, got {kwargs.get('metric')!r}"
    )


def test_score_ce_cached_promoted_to_stage():
    path = _REPO_ROOT / "yadgar" / "backend" / "retrieval" / "_reranking_cross_encoder.py"
    kwargs = _observe_kwargs_for_function(path, "score_ce_cached")
    assert kwargs.get("tier") == "stage", (
        f"retrieval.ce.score_ce_cached must be tier='stage', got {kwargs.get('tier')!r}"
    )
    assert kwargs.get("metric") == "retrieval.ce.score_ce_cached", (
        f"metric label must be unchanged, got {kwargs.get('metric')!r}"
    )
