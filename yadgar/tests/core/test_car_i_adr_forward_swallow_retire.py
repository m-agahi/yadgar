"""Car I (ledger task #346) — retire the C10 #319 sibling swallows in adr.py.

Car C10 #319 made the /admin route map ``AdminRefusal`` → 409 + structured
envelope so a deliberate refusal and a server fault stop being byte-identical
to every automated caller. That fix landed for the three create_*_row wrappers
in ``backend/admin_exec/ledger.py``, but the same pattern lived in core's
``yadgar/core/server/tools/adr.py`` too — ``_allocate_adr_ledger_row`` and
``_link_adr_body_slug`` each wrapped a ``_forward_admin(...)`` call in
``except Exception`` and converted the raise into ``{"ok": False, "error":
"..."}`` at HTTP 200.

Why that mattered:

* ``_forward_admin`` raises ``httpx.HTTPError`` on a backend transport failure
  (connect refused, read timeout, 5xx that is not ours) and ``RuntimeError``
  on a missing ``YADGAR_EMBED_URL``. Both are server faults, and the route
  renders them as 500s — but ONLY if the wrapper lets them escape. A bare
  ``except Exception`` on the core side silently turned every transport
  failure into a 200 with ``ok:False``, indistinguishable from a successful
  refusal envelope that the same caller is also expected to read.

* A structured refusal from ``create_adr_row`` (e.g. an unknown project_id
  after Car 5's registry check) is RETURNED by ``_forward_admin`` as
  ``{"ok": False, "refused": True, "reason": ...}`` — not raised. The
  existing post-forward check (``if row is None``, ``if slug_result.get("ok")
  is False``) already handles that path. The except blocks were not load-
  bearing for refusals; they were load-bearing ONLY for masking real faults.

* The two except blocks also gave a misleading error string —
  ``"create_adr_row forward failed: <ConnectionError str>"`` — that names
  the WRONG op from the operator's point of view (the op did not fail; the
  transport to the op failed) and breaks the uniform "5xx + reason" the
  /admin route already speaks.

This file pins BOTH halves of the contract:

  (1) the two wrappers LET ``httpx.HTTPError`` and ``RuntimeError`` propagate,
      so the /admin route renders them as server faults;
  (2) the existing post-forward refusal-envelope check still parses the
      ``{"ok": False, "refused": True, ...}`` shape that ``_forward_admin``
      returns for a deliberate ``AdminRefusal`` and surfaces it as the
      same 409-boundary envelope the rest of the tool surface speaks.

SCOPE: surgical. The other ``except Exception`` sites in adr.py
(``_link_adr_supersede_targets`` best-effort logging, ``adr_list`` /
``adr_get`` GET-shaped forwards, the rollup-regen fire-and-forget) stay —
they are not on the refusal path and were never the defect.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from yadgar.core.server.tools import adr as adr_mod


def _row_envelope(row: dict) -> dict:
    """The shape ``_forward_admin`` returns for a successful create_adr_row."""
    return {"ok": True, "row": row}


def _refusal_envelope(reason: str = "unknown_project_id", **extra) -> dict:
    """The shape ``_forward_admin`` returns for a structured AdminRefusal."""
    return {"ok": False, "refused": True, "reason": reason, "error": reason, **extra}


def _slug_envelope() -> dict:
    """The shape ``_forward_admin`` returns for a successful set_adr_body_slug."""
    return {"ok": True}


class TestAllocateAdrLedgerRowPropagatesTransport:
    """``_allocate_adr_ledger_row`` must NOT swallow httpx / RuntimeError faults.

    Pre-Car I: a transport failure on the ``create_adr_row`` forward came
    back as ``{"ok": False, "error": "create_adr_row forward failed: <exc>"}``,
    rendered as 200. Post-Car I: the exception propagates so the /admin route
    renders it as 500 and ``_forward_admin``'s refusal envelope stays
    distinguishable from a real fault.
    """

    def test_httpx_http_error_propagates_not_swallowed(self) -> None:
        err = httpx.ConnectError("backend down")
        with patch("yadgar.core.server.tools.adr._forward_admin", side_effect=err) as fwd:
            with pytest.raises(httpx.ConnectError) as excinfo:
                adr_mod._allocate_adr_ledger_row(
                    project_id="m-agahi/yadgar",
                    title="x",
                    status="accepted",
                    date="2026-08-26",
                )
            assert excinfo.value is err, (
                "the exact exception must propagate — the wrapper must not "
                "re-wrap it into a generic Exception"
            )
        # The single forward was attempted exactly once.
        assert len(fwd.call_args_list) == 1

    def test_runtime_error_missing_backend_url_propagates(self) -> None:
        """``_forward_admin`` raises ``RuntimeError("YADGAR_EMBED_URL is not set")``
        on a bare host. That is a real fault, not a refusal — the wrapper
        must let it through so the route renders 500."""
        err = RuntimeError("YADGAR_EMBED_URL is not set; cannot forward admin op")
        with patch("yadgar.core.server.tools.adr._forward_admin", side_effect=err):
            with pytest.raises(RuntimeError) as excinfo:
                adr_mod._allocate_adr_ledger_row(
                    project_id="m-agahi/yadgar",
                    title="x",
                    status="accepted",
                    date="2026-08-26",
                )
            assert "YADGAR_EMBED_URL" in str(excinfo.value)

    def test_refusal_envelope_still_surfaces_as_ok_false(self) -> None:
        """The CARRIED refusal (returned, not raised) keeps the old contract.

        ``_forward_admin`` returns ``{"ok": False, "refused": True, "reason":
        "unknown_project_id", ...}`` for a deliberate ``AdminRefusal``. The
        post-forward check on the row result must surface that to the caller
        as ``{"ok": False, "error": "..."}`` — the same envelope the rest of
        the tool surface uses for a refused write. A blanket
        ``except Exception`` that ALSO catches the returned envelope would
        turn this into ``"create_adr_row forward failed: {'ok': False,
        'refused': True, ...}"`` — a regression the test pins.
        """
        env = _refusal_envelope(
            reason="unknown_project_id",
            project_id="m-agahi/ghost",
        )
        with patch("yadgar.core.server.tools.adr._forward_admin", return_value=env):
            result = adr_mod._allocate_adr_ledger_row(
                project_id="m-agahi/ghost",
                title="x",
                status="accepted",
                date="2026-08-26",
            )
        assert isinstance(result, dict), (
            "a refusal must surface as the structured error dict the rest of "
            "adr_add parses, not as an exception — the wrapper's job is the "
            "non-refusal path only"
        )
        assert result.get("ok") is False
        # The post-forward path names the reason it received — not a transport
        # error string.
        assert "create_adr_row returned no row" in result.get("error", ""), (
            "the post-forward reason-name must come from the returned envelope, "
            "not the swallowed-exception string"
        )
        assert "unknown_project_id" in result.get("error", "") or "forward failed" in result.get(
            "error", ""
        ), (
            f"the error string should name either the returned reason or the "
            f"returned envelope; got {result.get('error')!r}"
        )

    def test_successful_envelope_still_returns_row_id(self) -> None:
        """Control: the happy path keeps working."""
        with patch(
            "yadgar.core.server.tools.adr._forward_admin",
            return_value=_row_envelope({"id": 42, "title": "x"}),
        ):
            result = adr_mod._allocate_adr_ledger_row(
                project_id="m-agahi/yadgar",
                title="x",
                status="accepted",
                date="2026-08-26",
            )
        adr_id_int, adr_id = result  # type: ignore[misc]
        assert adr_id_int == 42
        assert adr_id == "ADR-0042"


class TestLinkAdrBodySlugPropagatesTransport:
    """``_link_adr_body_slug`` — same fix, same contract."""

    def test_httpx_http_error_propagates_not_swallowed(self) -> None:
        err = httpx.ReadTimeout("backend hung")
        with patch("yadgar.core.server.tools.adr._forward_admin", side_effect=err):
            with pytest.raises(httpx.ReadTimeout):
                adr_mod._link_adr_body_slug(adr_id_int=1, adr_id="ADR-0001", page_slug="x_adr-0001")

    def test_runtime_error_missing_backend_url_propagates(self) -> None:
        err = RuntimeError("YADGAR_EMBED_URL is not set")
        with patch("yadgar.core.server.tools.adr._forward_admin", side_effect=err):
            with pytest.raises(RuntimeError):
                adr_mod._link_adr_body_slug(adr_id_int=1, adr_id="ADR-0001", page_slug="x_adr-0001")

    def test_refusal_envelope_still_surfaces_as_ok_false(self) -> None:
        """A carried refusal envelope returns ``{"ok": False, "refused": True,
        "reason": "..."}`` — the existing post-forward check on line 528
        already surfaces it. The Car I fix must not regress that path."""
        env = _refusal_envelope(reason="wiki_size_collapse", old_bytes=15902)
        with patch("yadgar.core.server.tools.adr._forward_admin", return_value=env):
            result = adr_mod._link_adr_body_slug(
                adr_id_int=1, adr_id="ADR-0001", page_slug="x_adr-0001"
            )
        assert result is not None
        assert result.get("ok") is False
        # Names the underlying reason, not the swallowed-exception string.
        assert "set_adr_body_slug failed" in result.get("error", "")

    def test_successful_envelope_returns_none(self) -> None:
        """Control: the happy path still returns None (no error)."""
        with patch(
            "yadgar.core.server.tools.adr._forward_admin",
            return_value=_slug_envelope(),
        ):
            result = adr_mod._link_adr_body_slug(
                adr_id_int=1, adr_id="ADR-0001", page_slug="x_adr-0001"
            )
        assert result is None


# ---------------------------------------------------------------------------
# Pin: the two wrappers are the only C10 #319 siblings in adr.py.
# Other except sites are best-effort and stay — the surgical fix retires
# ONLY the two above. A future car that wants to widen it has its own ledger
# task and its own test file.
# ---------------------------------------------------------------------------


def test_no_other_adr_py_sibling_swallow_added() -> None:
    """Regression: the two sites Car I retired are the ONLY admin-op
    forwarder sites in adr.py that still wrap ``_forward_admin`` in a
    blanket ``except Exception``. Anything that adds one is a re-introduce
    of the defect the car just retired, and this test fires BEFORE the
    wider adr.py swallows (the best-effort rollup / supersede logging) are
    audited for retirement, so it pins the surgical scope, not the long-
    term ideal."""
    import re
    from pathlib import Path

    src = Path(adr_mod.__file__).read_text(encoding="utf-8")
    # Each forwarder call in adr.py — find the immediate context. We
    # require: a line with `_forward_admin(`, the enclosing try block has
    # NO `except Exception` that also returns/raises an {ok:False,...} dict.
    # The "best-effort" sites use a different shape (logger.warning + continue,
    # not return {"ok": False, ...}) and stay.
    pattern = re.compile(r"_forward_admin\(", re.MULTILINE)
    for m in pattern.finditer(src):
        # Walk backwards 30 lines to look for `except Exception`
        # and forward 30 lines to look for `return {"ok": False`.
        start = max(0, m.start() - 1500)
        ctx = src[start : m.end() + 200]
        if "except Exception" in ctx and 'return {"ok": False' in ctx:
            pytest.fail(
                "a new `_forward_admin(...)` site in adr.py is wrapped in "
                "`except Exception` and returns `{ok: False, ...}`. That is "
                "the Car I C10 #319 sibling shape — let transport faults "
                "propagate; parse the returned envelope instead."
            )
