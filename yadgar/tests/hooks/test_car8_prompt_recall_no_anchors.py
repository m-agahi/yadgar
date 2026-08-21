"""Car 8 (task 283) — prompt-recall must not re-inject anchors.

Anchored memories are ALREADY in the context window: ``project_brief(mode=
"restore")`` returns ``top_anchors`` and the SessionStart hook injects it.
Measured 2026-08-20: all five rows prompt-recall surfaced during a live
investigation were ``_anchor``-tagged and ``is_protected`` — the hook spent its
entire 3000-char budget restating context the model already had.

Scope: the PROMPT-RECALL hook only. An explicit ``recall("what did we decide
about X")`` must still be able to return an anchor — there the model asked for
it and it is not duplicated.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch


def _make_mock_request(params: dict) -> MagicMock:
    req = MagicMock()
    req.query_params = MagicMock()
    req.query_params.get = MagicMock(side_effect=lambda k, d="": params.get(k, d))
    return req


def _run_prompt_recall(rows: list[dict], captured: dict | None = None):
    """Drive hook_prompt_recall with `rows` as the forwarded backend result."""
    import yadgar._shared.runtime.state as _st
    import yadgar.core.server.http as _http

    async def _fake_forward(forwarder, handler_name, *args, **kwargs):
        if captured is not None:
            captured["args"] = args
            captured["kwargs"] = kwargs
        return rows

    req = _make_mock_request({"query": "how does the drainer work", "directory": "/home/user/proj"})

    async def _run():
        with (
            patch.object(_st, "_retriever", MagicMock()),
            patch("yadgar.core.server.http._recall_with_timeout", side_effect=_fake_forward),
            patch.object(_st, "_last_session_context", {}),
            patch.object(_st, "_last_prompt_recall", {}),
        ):
            return await _http.hook_prompt_recall(req)

    return json.loads(asyncio.run(_run()).body)


def _row(content: str, **extra) -> dict:
    row = {"content": content, "directory_context": "/home/user/proj"}
    row.update(extra)
    return row


class TestPromptRecallDropsAnchors:
    def test_anchor_tagged_rows_are_not_injected(self):
        """A row tagged `_anchor` is already in context — it must not be re-injected."""
        body = _run_prompt_recall(
            [
                _row("ANCHORED-FACT", tags=["yadgar", "_anchor"]),
                _row("FRESH-DRAINER-DETAIL", tags=["drainer"]),
            ]
        )
        assert "FRESH-DRAINER-DETAIL" in body["text"], body
        assert "ANCHORED-FACT" not in body["text"], body

    def test_is_protected_rows_are_not_injected(self):
        """Legacy anchors carry `is_protected` without the tag — drop those too."""
        body = _run_prompt_recall(
            [
                _row("LEGACY-ANCHOR", is_protected=True),
                _row("FRESH-DRAINER-DETAIL", is_protected=False),
            ]
        )
        assert "FRESH-DRAINER-DETAIL" in body["text"], body
        assert "LEGACY-ANCHOR" not in body["text"], body

    def test_all_anchors_yields_empty_injection(self):
        """No injection beats an injection that only restates existing context."""
        body = _run_prompt_recall(
            [
                _row("A1", tags=["_anchor"]),
                _row("A2", is_protected=True),
            ]
        )
        assert body["text"] == "", body

    def test_non_anchor_rows_are_untouched(self):
        """Rows with no anchor marking pass through exactly as before."""
        body = _run_prompt_recall([_row("PLAIN-1", tags=["x"]), _row("PLAIN-2")])
        assert "PLAIN-1" in body["text"] and "PLAIN-2" in body["text"], body

    def test_forward_over_fetches_so_the_filter_has_headroom(self):
        """The hook asks the backend for MORE than it injects.

        Dropping anchors from a 5-row page would leave a near-empty injection —
        exactly the budget waste this car exists to remove. The hook over-fetches
        and truncates AFTER filtering. (`rerank_pool` is already >= RERANKER_TOP_K
        server-side, so the extra rows cost a longer trim, not more hydration.)
        """
        import yadgar.core.server.http as _http

        captured: dict = {}
        _run_prompt_recall([_row(f"m{i}") for i in range(20)], captured)

        asked = captured["kwargs"].get("max_results")
        assert asked == _http._PROMPT_RECALL_CANDIDATES, captured
        assert asked > _http._PROMPT_RECALL_INJECTED, (
            f"the hook must over-fetch: asked={asked}, injects={_http._PROMPT_RECALL_INJECTED}"
        )

    def test_injection_is_capped_after_filtering(self):
        """Over-fetching must not widen the injection: still at most 5 rows."""
        import yadgar.core.server.http as _http

        body = _run_prompt_recall([_row(f"unique-row-{i}") for i in range(20)])
        injected = [ln for ln in body["text"].splitlines() if ln.startswith("- unique-row-")]
        assert len(injected) == _http._PROMPT_RECALL_INJECTED, body


class TestAnchorExclusionIsScopedToTheHook:
    def test_recall_tool_does_not_drop_anchors(self):
        """The MCP recall tool must NOT inherit the exclusion (source-level).

        An explicit recall asks for whatever matches, anchors included.
        """
        import importlib.util
        from pathlib import Path

        # `yadgar.core.server.tools.recall` resolves to the re-exported FUNCTION,
        # so reach the module file through the spec instead.
        spec = importlib.util.find_spec("yadgar.core.server.tools.recall")
        assert spec is not None and spec.origin
        src = Path(spec.origin).read_text()
        assert "_drop_anchor_rows" not in src, (
            "anchor exclusion leaked into the recall tool — it is prompt-recall-scoped"
        )

    def test_only_prompt_recall_calls_the_filter(self):
        """Exactly one call site, and it is inside hook_prompt_recall's body."""
        import re
        from pathlib import Path

        import yadgar.core.server.http as _http

        src = Path(_http.__file__).read_text()
        # Call sites only — exclude the `def _drop_anchor_rows(` definition.
        calls = [
            m.start()
            for m in re.finditer(r"(?<!def )_drop_anchor_rows\(", src)
            if not src[: m.start()].rstrip().endswith("def")
        ]
        assert len(calls) == 1, f"expected one call site, found {len(calls)}"

        start = src.index("async def hook_prompt_recall(")
        rest = src[start + 1 :]
        nxt = rest.find("\nasync def ")
        end = len(src) if nxt == -1 else start + 1 + nxt
        assert start < calls[0] < end, (
            "the only _drop_anchor_rows call must live inside hook_prompt_recall"
        )
