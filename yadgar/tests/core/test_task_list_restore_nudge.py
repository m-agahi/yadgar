"""TDD — Car E, step 3+4: `_task_list_restore_nudge` rewire.

After Car E this function must:

1. Read from the `task` ledger (via ``list_task_rows`` / Car D's tool), NOT
   parse the `{project}-task-list` wiki page.
2. Emit `[{_tid}]` (the D11 prefix) via the existing ``_format_task_id``
   / task-number format.
3. Include the D11 prefix-preserve instruction in the nudge template:
   "Preserve the `[N]` prefix at the start of each `TaskCreate` subject so
   task ids reconcile across sessions."
4. Cap at `_CAP = 12` open tasks.
5. Fail-open: any error returns `""`.

Without Car D merged the `task` ledger symbols don't exist yet. The rewire
must use a graceful fallback or conditional import so the function still
runs (returning the old behavior) until Car D ships.

These tests pin the BEHAVIOR (not the implementation). They use scaffolding
in /tmp where the function runs the OLD wiki-page path, and pin the
POST-CAR-D behavior at a function-name level so when Car D ships the tests
will assert the new path.
"""

from __future__ import annotations

import asyncio
import os
import re


def _read_source() -> str:
    import yadgar.core.server as srv

    pkg_dir = os.path.dirname(srv.__file__)
    with open(os.path.join(pkg_dir, "http.py")) as fh:
        return fh.read()


def _extract_nudge_body(src_text: str) -> str:
    """Return the body of `_task_list_restore_nudge` AND its helpers from http.py.

    Car E extracted the legacy wiki-page parse into `_task_list_legacy_wiki_nudge`
    and the ledger-row formatter into `_format_task_list_nudge_rows` to keep the
    primary handler under the C901 complexity cap. The grep below captures all
    three so assertions on D11 / cap / wiki-page fall back stay meaningful.
    """
    # Pull all three top-level definitions in source order.
    pieces = []
    for name in (
        "_task_list_restore_nudge",
        "_format_task_list_nudge_rows",
        "_task_list_legacy_wiki_nudge",
    ):
        m = re.search(
            rf"(?:async )?def {name}\(.*?(?=\n@observe|\n@trace_span|\ndef [a-zA-Z_])",
            src_text,
            re.DOTALL,
        )
        if m is not None:
            pieces.append(m.group(0))
    assert pieces, "could not locate _task_list_restore_nudge + helpers"
    return "\n".join(pieces)


# Car C deleted two tests that lived here:
#
#   test_nudge_includes_d11_prefix_preserve_instruction  (asserted "Preserve the `[")
#   test_nudge_includes_taskcreate_action                (asserted "TaskCreate")
#
# Both regex-scraped the SOURCE TEXT of http.py and substring-matched a wording
# choice. Neither was rewritten to the new wording, because the mechanic itself
# is the problem: they were passing on the strength of `_task_list_legacy_wiki_nudge`,
# an UNREACHABLE rollback arm that still contains both phrases — so they would have
# stayed green through any rewrite of the live path, including one that deleted the
# instruction entirely. The D11 `[N]` prefix is retired regardless: the seeder makes
# the ledger id the harness id, so there is no prefix left to preserve. The
# behavioural replacements render through HTTP and assert on the response, in
# yadgar/tests/core/test_session_context_endpoint.py
# (test_render_never_orders_a_full_hand_mirror, test_nudge_leads_with_in_progress_rows).


def test_nudge_emits_open_status_set():
    """The nudge must filter for `pending` and `in_progress` only (open tasks)."""
    body = _extract_nudge_body(_read_source())
    assert "pending" in body
    assert "in_progress" in body


def test_nudge_cap_is_12():
    """The cap must remain `_CAP = 12` (the existing forcing-nudge cap)."""
    body = _extract_nudge_body(_read_source())
    assert "_CAP = 12" in body, "the open-task cap must stay at 12"


def test_nudge_fail_open_returns_empty_string():
    """The function must remain fail-open — any error returns ``""``."""
    body = _extract_nudge_body(_read_source())
    # The outermost `except Exception as _te:` returns "".
    assert 'return ""' in body, 'nudge must fail-open with return ""'


def test_nudge_no_longer_parses_wiki_page_in_primary_path():
    """After Car E the PRIMARY nudge path reads from the task ledger.

    The legacy ``storage.get_wiki_page_by_slug_directory`` call is retained
    ONLY as a graceful fallback for the without-Car-D branch — the PRIMARY
    path must use the ledger. We assert the body contains BOTH: the legacy
    call in a fallback branch (referenced) AND a ledger-reading path
    (referenced). The PRIMARY path is the one that runs when Car D has
    shipped.
    """
    body = _extract_nudge_body(_read_source())
    has_legacy = "get_wiki_page_by_slug_directory" in body
    has_ledger = "task_list" in body or "list_task_rows" in body
    assert has_legacy, (
        "primary path must include the legacy fallback so the function works without Car D merged"
    )
    assert has_ledger, "primary path must read from the task ledger (task_list / list_task_rows)"


def test_nudge_source_targets_ledger_path():
    """The rewire must reach the ledger via the forward path or a Car D
    storage handler. We do not assert the exact import (Car D may pick the
    forward-vs-direct shape) but the body must contain a forward or
    list_task_rows reference."""
    body = _extract_nudge_body(_read_source())
    has_ledger_ref = "list_task_rows" in body or "_forward_admin" in body or "task_list" in body
    assert has_ledger_ref, (
        "nudge must read from the task ledger (list_task_rows / forward / task_list)"
    )


def test_nudge_works_without_storage(monkeypatch):
    """Even when storage is unavailable the nudge fails open — no text, no rows.

    Car C: the function returns ``(nudge, rows)``; the rows feed the on-disk
    seeder. The empty ROW list matters as much as the empty string — a caller
    that seeded from a failed read would write a partial task list.
    """
    from yadgar.core.server import http

    nudge, rows = asyncio.run(http._task_list_restore_nudge(""))
    assert nudge == ""
    assert rows == []
