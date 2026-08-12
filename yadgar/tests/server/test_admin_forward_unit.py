"""Unit tests for the R3 Car 3a (R5) admin write-forward contract.

Covers:
  1. _forward_admin sends {op, payload} to ${YADGAR_EMBED_URL}/admin with Bearer
     auth, and unwraps the backend {"result": ...} envelope.
  2. _forward_admin raises RuntimeError when YADGAR_EMBED_URL is unset (forward-only).
  3. run_admin_op dispatches known ops and raises KeyError on unknown ops.
  4. The core CRUD write tools forward the correct op-name + payload (validation
     stays core-side; the DB write forwards).

These are pure-unit (no live backend): httpx.post is patched, and run_admin_op
is exercised against a stubbed storage.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1 + 2: _forward_admin HTTP contract
# ---------------------------------------------------------------------------
def test_forward_admin_payload_and_auth():
    """_forward_admin POSTs {op, payload} to /admin with Bearer auth; unwraps result."""
    from yadgar.core.forward import _forward_admin

    captured: dict = {}

    def _fake_post(url, *, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        resp = MagicMock()
        resp.json.return_value = {"result": {"added": True, "slug": "s", "position": 0}}
        resp.raise_for_status.return_value = None
        return resp

    with (
        patch("httpx.post", _fake_post),
        patch.dict(
            "os.environ",
            {"YADGAR_EMBED_URL": "http://backend:8001", "YADGAR_MCP_AUTH_TOKEN": "tok123"},
        ),
    ):
        result = _forward_admin("bookmark_add", {"slug": "s", "label_override": ""})

    assert captured["url"] == "http://backend:8001/admin"
    assert captured["headers"]["Authorization"] == "Bearer tok123"
    assert captured["json"] == {
        "op": "bookmark_add",
        "payload": {"slug": "s", "label_override": ""},
    }
    # Envelope unwrapped: caller gets the inner result dict, not {"result": ...}.
    assert result == {"added": True, "slug": "s", "position": 0}


def test_forward_admin_no_url_raises():
    """_forward_admin raises RuntimeError when YADGAR_EMBED_URL is unset (forward-only)."""
    from yadgar.core.forward import _forward_admin

    with patch.dict("os.environ", {"YADGAR_EMBED_URL": ""}, clear=False):
        with pytest.raises(RuntimeError) as exc:
            _forward_admin("bookmark_add", {"slug": "s"})
    assert "YADGAR_EMBED_URL" in str(exc.value)


# ---------------------------------------------------------------------------
# 3: run_admin_op dispatch
# ---------------------------------------------------------------------------
def test_run_admin_op_dispatches_known_op():
    """run_admin_op routes a known op to its registered impl and returns its result.

    The dispatch table (_ADMIN_OPS) binds the impl by reference at import time, so
    stub the table entry (not the module attribute) to intercept the dispatch.
    """
    from yadgar.backend import admin_exec

    sentinel = {"added": True, "slug": "z", "position": 3}
    stub = MagicMock(return_value=sentinel)
    with patch.dict(admin_exec._ADMIN_OPS, {"bookmark_add": stub}):
        out = admin_exec.run_admin_op("bookmark_add", {"slug": "z"})
    stub.assert_called_once_with({"slug": "z"})
    assert out is sentinel


def test_run_admin_op_unknown_op_raises_keyerror():
    """run_admin_op raises KeyError on an unregistered op (route maps to 400)."""
    from yadgar.backend.admin_exec import run_admin_op

    with pytest.raises(KeyError):
        run_admin_op("does_not_exist", {})


def test_admin_ops_registry_covers_bookmarks_and_blocks():
    """The dispatch table registers exactly the bookmark + block write ops."""
    from yadgar.backend.admin_exec import admin_ops

    ops = admin_ops()
    assert {"bookmark_add", "bookmark_remove", "bookmark_reorder"} <= ops
    assert {
        "block_create",
        "block_update",
        "block_delete",
        "block_replace",
        "block_append",
    } <= ops


def test_admin_ops_registry_covers_memory_rules_writes():
    """R3 Car 3b: the dispatch table registers the memory/rules write ops."""
    from yadgar.backend.admin_exec import admin_ops

    ops = admin_ops()
    assert {"forget", "memory_update", "reembed_all", "add_rule", "archive_purge"} <= ops


def test_admin_ops_registry_covers_wiki_edit_family():
    """R3 Car 3c: the dispatch table registers the wiki-edit + agent_prompt writes."""
    from yadgar.backend.admin_exec import admin_ops

    ops = admin_ops()
    assert {
        "wiki_delete",
        "wiki_autolink",
        "wiki_update",
        "wiki_restore",
        "wiki_append_section",
        "wiki_set_metadata",
        "wiki_replace_text",
        "wiki_delete_text",
        "wiki_insert_after",
        "wiki_insert_before",
        "wiki_replace_at",
        "wiki_delete_at",
        "wiki_insert_at",
        "wiki_replace_markdown_block",
        "agent_prompt_save",
    } <= ops


# ---------------------------------------------------------------------------
# 4: core CRUD tools forward the right op-name + payload
# ---------------------------------------------------------------------------
def test_bookmark_add_forwards_op_and_stripped_slug():
    """bookmark_add strips the slug core-side then forwards op='bookmark_add'."""
    import yadgar.core.server.tools.bookmarks as _bm

    calls: list = []

    def _fake_forward(op, payload):
        calls.append((op, payload))
        return {"added": True, "slug": payload["slug"], "position": 0}

    with patch.object(_bm, "_forward_admin", _fake_forward):
        _bm.bookmark_add("  spaced-slug  ", label_override="Lbl")

    assert calls == [("bookmark_add", {"slug": "spaced-slug", "label_override": "Lbl"})]


def test_bookmark_add_empty_slug_short_circuits_no_forward():
    """bookmark_add with an empty slug rejects core-side and never forwards."""
    import yadgar.core.server.tools.bookmarks as _bm

    def _boom(op, payload):  # pragma: no cover - must not be called
        raise AssertionError("empty slug must not forward")

    with patch.object(_bm, "_forward_admin", _boom):
        result = _bm.bookmark_add("   ")
    assert result == {"added": False, "reason": "slug_empty"}


def test_block_create_project_scope_missing_directory_no_forward():
    """block_create(scope='project', directory=None) rejects core-side, no forward."""
    import yadgar.core.server.tools.blocks as _bl

    def _boom(op, payload):  # pragma: no cover - must not be called
        raise AssertionError("directory-guard reject must not forward")

    with patch.object(_bl, "_forward_admin", _boom):
        result = _bl.block_create(name="n", content="c", scope="project", directory=None)
    assert result.get("error") == "missing_directory"


def test_block_create_forwards_payload_after_guards():
    """block_create forwards op='block_create' with the full payload once guards pass."""
    import yadgar.core.server.tools.blocks as _bl

    calls: list = []

    def _fake_forward(op, payload):
        calls.append((op, payload))
        return {"id": "block:1", "name": payload["name"]}

    with patch.object(_bl, "_forward_admin", _fake_forward):
        _bl.block_create(name="n", content="c", scope="global", directory=None, char_limit=1234)

    assert len(calls) == 1
    op, payload = calls[0]
    assert op == "block_create"
    # C11 (0047 PR#40 §5): block_create now KEEPS accept_project_param's
    # validated return value and puts it on the payload (migration 033 gave
    # memory_block a project_id column). No project= was passed here, and
    # accept_project_param deliberately does not resolve when project is
    # None (it would raise via resolve_effective_project otherwise) — so
    # project_id: None is the correct forwarded value for this unnamed call,
    # not a caller that should have supplied one.
    assert payload == {
        "name": "n",
        "content": "c",
        "scope": "global",
        "directory": None,
        "project_id": None,
        "char_limit": 1234,
    }


# ---------------------------------------------------------------------------
# 5: R3 Car 3b — memory/rules write tools forward the right op-name + payload
# ---------------------------------------------------------------------------
def test_forget_forwards_op_and_int_id():
    """forget forwards op='forget' with an int-coerced memory_id."""
    import yadgar.core.server.tools.admin_other as _ao

    calls: list = []

    def _fake_forward(op, payload):
        calls.append((op, payload))
        return {"memory_id": payload["memory_id"], "status": "deleted"}

    with patch.object(_ao, "_forward_admin", _fake_forward):
        _ao.forget("42")

    assert calls == [("forget", {"memory_id": 42})]


def test_memory_update_validates_core_then_forwards():
    """memory_update rejects disallowed keys core-side (no forward), else forwards."""
    import yadgar.core.server.tools.admin_other as _ao

    def _boom(op, payload):  # pragma: no cover - must not be called
        raise AssertionError("disallowed key must not forward")

    with patch.object(_ao, "_forward_admin", _boom):
        with pytest.raises(ValueError, match="Disallowed field"):
            _ao.memory_update(1, {"heat": 999.0})

    calls: list = []

    def _fake_forward(op, payload):
        calls.append((op, payload))
        return {"id": payload["memory_id"]}

    with patch.object(_ao, "_forward_admin", _fake_forward):
        _ao.memory_update(7, {"content": "x"})

    assert calls == [("memory_update", {"memory_id": 7, "fields": {"content": "x"}})]


def test_reembed_all_forwards_with_long_timeout():
    """reembed_all forwards op='reembed_all' with an empty payload + a long timeout."""
    import yadgar.core.server.tools.admin_other as _ao

    calls: list = []

    def _fake_forward(op, payload, timeout_s=30.0):
        calls.append((op, payload, timeout_s))
        return {"status": "ok", "reembedded": 0}

    with patch.object(_ao, "_forward_admin", _fake_forward):
        _ao.reembed_all()

    assert len(calls) == 1
    op, payload, timeout_s = calls[0]
    assert op == "reembed_all"
    assert payload == {}
    assert timeout_s >= 300.0  # generous timeout so a large backlog does not trip 30s


def test_add_rule_forwards_full_payload():
    """add_rule forwards op='add_rule' with all args (validation runs backend-side)."""
    import yadgar.core.server.tools.admin_other as _ao

    calls: list = []

    def _fake_forward(op, payload):
        calls.append((op, payload))
        return {"status": "created", "rule_id": 1}

    with patch.object(_ao, "_forward_admin", _fake_forward):
        _ao.add_rule(
            rule_type="soft",
            scope="global",
            condition="tag contains x",
            action="boost:0.3",
            priority=5,
        )

    assert calls == [
        (
            "add_rule",
            {
                "rule_type": "soft",
                "scope": "global",
                "condition": "tag contains x",
                "action": "boost:0.3",
                "priority": 5,
                "scope_value": "",
            },
        )
    ]


def test_archive_purge_secret_gates_core_then_forwards():
    """archive_purge runs the secret gate core-side, then forwards the DB write."""
    import yadgar.core.server.tools.admin_archive as _aa

    calls: list = []

    def _fake_forward(op, payload):
        calls.append((op, payload))
        return {"dry_run": payload["dry_run"], "purged": 0, "candidates": 0}

    with patch.object(_aa, "_forward_admin", _fake_forward):
        _aa.archive_purge(dry_run=False, retention_days=40)

    assert calls == [("archive_purge", {"dry_run": False, "retention_days": 40})]


# ---------------------------------------------------------------------------
# 5: R3 Car 3c — wiki-edit family + agent_prompt forward the right op + payload
# ---------------------------------------------------------------------------


def test_wiki_replace_text_gates_and_resolves_core_then_forwards():
    """wiki_replace_text keeps the I26 secret gate + slug→page_id resolution core,
    then forwards the write keyed by page_id (backend has no git/cwd)."""
    import yadgar.core.server.tools.wiki as _w

    calls: list = []

    def _fake_forward(op, payload):
        calls.append((op, payload))
        return {"ok": True, "page_id": 7, "replaced_count": 1, "slug": "s"}

    with (
        patch.object(_w, "_forward_admin", _fake_forward),
        patch.object(_w, "_resolve_page_id_by_slug", return_value=(7, {"id": 7})),
    ):
        _w.wiki_replace_text("s", "old", "new", occurrences=1)

    assert calls == [
        (
            "wiki_replace_text",
            {
                "page_id": 7,
                "old_text": "old",
                "new_text": "new",
                "occurrences": 1,
                "slug": "s",
            },
        )
    ]


def test_wiki_replace_text_secret_gate_blocks_before_forward():
    """A secret in new_text is rejected core-side — no forward happens."""
    import yadgar.core.server.tools.wiki as _w

    calls: list = []

    def _fake_forward(op, payload):
        calls.append((op, payload))
        return {}

    # A live AWS-key-shaped token trips the I26 gate.
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    with (
        patch.object(_w, "_forward_admin", _fake_forward),
        patch.object(_w, "_resolve_page_id_by_slug", return_value=(7, {"id": 7})),
    ):
        result = _w.wiki_replace_text("s", "old", f"key={secret}", occurrences=1)

    assert calls == []  # gate fired before any forward
    assert result.get("stored") is False or "error" in result or result.get("ok") is False


def test_wiki_set_metadata_forwards_slug_keyed_no_resolution():
    """wiki_set_metadata forwards slug-keyed (all-rows path — no page_id resolution)."""
    import yadgar.core.server.tools.wiki as _w

    calls: list = []

    def _fake_forward(op, payload):
        calls.append((op, payload))
        return {"ok": True, "slug": "s", "rows_updated": 1}

    with patch.object(_w, "_forward_admin", _fake_forward):
        # ADR-0215 removed 'branch' from the settable set — 'directory_context'
        # is now the only allowed field, so it is what exercises the forward.
        _w.wiki_set_metadata("s", "directory_context", "/home/max/work")

    assert calls == [
        (
            "wiki_set_metadata",
            {"slug": "s", "field": "directory_context", "value": "/home/max/work"},
        )
    ]


def test_wiki_update_validates_and_gates_core_then_forwards():
    """wiki_update keeps allowed-key validation + I26 gate core, then forwards page_id."""
    import yadgar.core.server.tools.admin_other as _ao

    calls: list = []

    def _fake_forward(op, payload):
        calls.append((op, payload))
        return {"id": 3, "content": "x"}

    with patch.object(_ao, "_forward_admin", _fake_forward):
        _ao.wiki_update(3, {"content": "x", "tags": ["a"]})

    assert calls == [("wiki_update", {"page_id": 3, "fields": {"content": "x", "tags": ["a"]}})]


def test_wiki_update_disallowed_key_raises_before_forward():
    """A disallowed field raises ValueError core-side — no forward."""
    import yadgar.core.server.tools.admin_other as _ao

    calls: list = []

    with patch.object(_ao, "_forward_admin", lambda op, payload: calls.append((op, payload))):
        with pytest.raises(ValueError, match="Disallowed field"):
            _ao.wiki_update(3, {"slug": "nope"})

    assert calls == []


def test_agent_prompt_save_validates_gates_wraps_core_then_forwards():
    """agent_prompt_save keeps directory-validate + I26 gate + content-wrap core,
    then forwards the composed payload (wiki.add + agent_pattern ledger row —
    TOC + library anchor retired by 0047 Car I)."""
    import yadgar.core.server.tools.agent_prompts as _ap

    calls: list = []

    def _fake_forward(op, payload):
        calls.append((op, payload))
        return {"saved": True, "version": 1, "slug": payload["slug"], "page_id": 9}

    with patch.object(_ap, "_forward_admin", _fake_forward):
        _ap.agent_prompt_save(
            "fix-bug",
            "do the thing",
            directory="/proj",
            # C5 (ADR-0227): agent_prompt_save resolves an identity before it
            # composes the payload, and returns an unresolved_project envelope
            # instead of forwarding when nothing names one — so without this the
            # two forwards asserted below never happen.
            project="owner/repo",
        )

    # 0047 Car I: page-first then ledger-row mirror (D40 content_hash). Two
    # forwards, in that order — a crash between them leaves an orphan page
    # (detected by check_page_row_desync), not an orphan row.
    assert len(calls) == 2
    page_op, page_payload = calls[0]
    row_op, row_payload = calls[1]

    # Forward #1 — the wiki body page (the canonical content).
    assert page_op == "agent_prompt_save"
    assert page_payload["slug"] == "agent-prompt-fix-bug"
    assert page_payload["pattern"] == "fix-bug"
    assert page_payload["directory"] == "/proj"
    assert "## Purpose" in page_payload["full_content"]
    assert "## Prompt" in page_payload["full_content"]
    assert "do the thing" in page_payload["full_content"]

    # Forward #2 — the agent_pattern ledger row (D40 content_hash pins the row
    # to the wiki body bytes; the row is the discovery surface for
    # agent_prompt_list / agent_prompt_get post-Car I).
    assert row_op == "save_agent_pattern_row"
    assert row_payload["name"] == "fix-bug"
    assert row_payload["body_slug"] == "agent-prompt-fix-bug"
    assert row_payload["status"] == "active"
    assert "content_hash" in row_payload


def test_agent_prompt_save_missing_directory_no_forward():
    """agent_prompt_save rejects an empty directory core-side — no forward."""
    import yadgar.core.server.tools.agent_prompts as _ap

    calls: list = []

    with patch.object(_ap, "_forward_admin", lambda op, payload: calls.append((op, payload))):
        result = _ap.agent_prompt_save("fix-bug", "content", directory="")

    assert calls == []
    assert result.get("saved") is False
    assert result.get("error") == "missing_directory"
