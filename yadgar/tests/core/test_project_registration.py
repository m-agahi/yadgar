"""Project auto-registration: shape guard, ``register_project``, ``project_register``.

ADR-1131: the registry accepts a new row from exactly two sources — the
SessionStart hook for the id it minted, and an explicit ``project_register``
call. A ``project=`` on any other tool call never creates a row.

SAFETY: no test here reaches the live registry. ``_forward_admin`` and the
registry key-set fetch are replaced with fakes on every path.
"""

from __future__ import annotations

import pytest

from yadgar._shared.storage.sql.errors import DuplicateProjectError, UnknownProjectError
from yadgar.core import project_registration as pr
from yadgar.core.server.tools import _project_registry as reg

# ── shape guard ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "m-agahi/yadgar",
        "quinyx/daneel",
        "local/scratch",
        "group/sub/repo",
        "owner.name/repo_x",
        "a/b",
    ],
)
def test_guard_accepts_registrable_ids(value):
    assert pr.registrable_project_id_error(value) is None


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        (None, "non-empty string"),
        (42, "non-empty string"),
        ("", "non-empty string"),
        ("global", "names no project"),
        ("unresolved", "names no project"),
        ("system", "names no project"),
        ("yadgar", "owner/repo"),
        ("local/", "owner/repo"),
        ("/repo", "owner/repo"),
        ("owner/", "owner/repo"),
        ("owner//repo", "owner/repo"),
        ("Quinyx/Daneel", "lowercase"),
        ("owner/repo name", "whitespace"),
        ("owner/repo\nother/repo", "whitespace"),
        ("owner/re\tpo", "whitespace"),
        ("debugging opsecrets nixos-quinyx", "whitespace"),
        ("owner/" + "r" * 256, "256"),
        ("owner/.hidden", "owner/repo"),
        ("memory:486752", "owner/repo"),
    ],
)
def test_guard_rejects_unregistrable_ids(value, fragment):
    err = pr.registrable_project_id_error(value)
    assert err is not None
    assert fragment in err


# ── register_project ────────────────────────────────────────────────────────


class _FakeAdmin:
    """Records ``_forward_admin`` calls; answers with a fixed envelope."""

    def __init__(self, answer=None, raises: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.answer = answer
        self.raises = raises

    def __call__(self, op, payload, timeout_s=30.0):
        self.calls.append((op, dict(payload)))
        if self.raises is not None:
            raise self.raises
        return self.answer


@pytest.fixture
def registry(monkeypatch):
    """A fake registry key set.

    ``state['keys']`` is what the cache answers; ``state['fresh_keys']``, when
    set, is what a ``refresh=True`` fetch answers instead (a row another
    process registered after the cache was filled). ``state['refreshes']``
    counts the forced fetches.
    """
    state: dict = {"keys": frozenset(), "invalidated": 0, "refreshes": 0}

    def _known(*, refresh: bool = False):
        if refresh:
            state["refreshes"] += 1
            return state.get("fresh_keys", state["keys"])
        return state["keys"]

    def _invalidate():
        state["invalidated"] += 1

    monkeypatch.setattr(reg, "known_project_ids", _known)
    monkeypatch.setattr(reg, "invalidate_project_registry_cache", _invalidate)
    return state


def _patch_admin(monkeypatch, fake: _FakeAdmin) -> _FakeAdmin:
    import yadgar.core.forward as fwd

    monkeypatch.setattr(fwd, "_forward_admin", fake)
    return fake


def test_register_creates_a_new_row_and_refreshes_the_cache(monkeypatch, registry):
    fake = _patch_admin(monkeypatch, _FakeAdmin({"ok": True, "row": {"key": "quinyx/daneel"}}))

    out = pr.register_project("quinyx/daneel")

    assert out == {"ok": True, "project_id": "quinyx/daneel", "created": True}
    assert fake.calls == [("create_project_row", {"key": "quinyx/daneel", "kind": "git"})]
    assert registry["invalidated"] == 1


def test_register_local_id_uses_local_kind(monkeypatch, registry):
    fake = _patch_admin(monkeypatch, _FakeAdmin({"ok": True, "row": {}}))

    pr.register_project("local/scratch", display_name="Scratch")

    assert fake.calls == [
        ("create_project_row", {"key": "local/scratch", "kind": "local", "display_name": "Scratch"})
    ]


def test_register_known_id_is_success_without_an_insert(monkeypatch, registry):
    registry["keys"] = frozenset({"m-agahi/yadgar"})
    fake = _patch_admin(monkeypatch, _FakeAdmin({"ok": True}))

    out = pr.register_project("m-agahi/yadgar")

    assert out == {"ok": True, "project_id": "m-agahi/yadgar", "created": False}
    assert fake.calls == []


def test_register_known_id_skips_the_guard(monkeypatch, registry):
    """A row seeded before the guard existed is success, not a guard warning.

    The guard decides what may ENTER the registry. Run before the membership
    check, it would fail an existing key on every SessionStart.
    """
    registry["keys"] = frozenset({"Legacy/Key"})
    fake = _patch_admin(monkeypatch, _FakeAdmin({"ok": True}))

    out = pr.register_project("Legacy/Key")

    assert out == {"ok": True, "project_id": "Legacy/Key", "created": False}
    assert fake.calls == []


def test_register_stale_cache_miss_refreshes_before_creating(monkeypatch, registry):
    """A cache filled before another process registered the row is re-fetched once."""
    registry["keys"] = frozenset()
    registry["fresh_keys"] = frozenset({"quinyx/daneel"})
    fake = _patch_admin(monkeypatch, _FakeAdmin({"ok": True}))

    out = pr.register_project("quinyx/daneel")

    assert out == {"ok": True, "project_id": "quinyx/daneel", "created": False}
    assert registry["refreshes"] == 1
    assert fake.calls == []


def test_register_unconsultable_registry_reports_unavailable(monkeypatch, registry):
    """No engine #2 (or no backend): a reason code, and no INSERT attempt."""
    registry["keys"] = None
    fake = _patch_admin(monkeypatch, _FakeAdmin({"ok": True}))

    out = pr.register_project("quinyx/daneel")

    assert out["ok"] is False
    assert out["created"] is False
    assert out["reason"] == pr.REASON_REGISTRY_UNAVAILABLE == "registry_unavailable"
    assert registry["refreshes"] == 1
    assert fake.calls == []


def test_register_duplicate_refusal_is_success(monkeypatch, registry):
    """The real envelope ``_forward_admin`` returns for an AdminRefusal (409)."""
    exc = DuplicateProjectError("quinyx/daneel")
    envelope = {"ok": False, "refused": True, "reason": exc.reason, "error": str(exc)}
    _patch_admin(monkeypatch, _FakeAdmin(envelope))

    out = pr.register_project("quinyx/daneel")

    assert out == {"ok": True, "project_id": "quinyx/daneel", "created": False}


def test_register_duplicate_error_text_without_reason_is_success(monkeypatch, registry):
    envelope = {"ok": False, "error": str(DuplicateProjectError("quinyx/daneel"))}
    _patch_admin(monkeypatch, _FakeAdmin(envelope))

    assert pr.register_project("quinyx/daneel")["ok"] is True


def test_register_backend_failure_is_not_ok(monkeypatch, registry):
    _patch_admin(monkeypatch, _FakeAdmin({"ok": False, "error": "engine #2 not composed"}))

    out = pr.register_project("quinyx/daneel")

    assert out["ok"] is False
    assert out["created"] is False
    assert "engine #2 not composed" in out["error"]


def test_register_backend_raise_is_not_ok(monkeypatch, registry):
    _patch_admin(monkeypatch, _FakeAdmin(raises=RuntimeError("YADGAR_EMBED_URL is not set")))

    out = pr.register_project("quinyx/daneel")

    assert out["ok"] is False
    assert "YADGAR_EMBED_URL" in out["error"]


def test_register_guard_rejects_before_any_backend_call(monkeypatch, registry):
    fake = _patch_admin(monkeypatch, _FakeAdmin({"ok": True}))

    out = pr.register_project("global")

    assert out["ok"] is False
    assert "names no project" in out["error"]
    assert fake.calls == []


def test_register_rejects_a_display_name_wider_than_the_column(monkeypatch, registry):
    fake = _patch_admin(monkeypatch, _FakeAdmin({"ok": True}))

    out = pr.register_project("quinyx/daneel", display_name="x" * 65)

    assert out["ok"] is False
    assert "64" in out["error"]
    assert fake.calls == []


# ── project_register MCP tool ───────────────────────────────────────────────


def test_project_register_tool_is_idempotent(monkeypatch, registry):
    from yadgar.core.server.tools.project_register import project_register

    fake = _patch_admin(monkeypatch, _FakeAdmin({"ok": True, "row": {}}))
    first = project_register(project_id="quinyx/daneel")
    registry["keys"] = frozenset({"quinyx/daneel"})
    second = project_register(project_id="quinyx/daneel")

    assert first == {"ok": True, "project_id": "quinyx/daneel", "created": True}
    assert second == {"ok": True, "project_id": "quinyx/daneel", "created": False}
    assert len(fake.calls) == 1


def test_project_register_tool_rejects_free_text(monkeypatch, registry):
    from yadgar.core.server.tools.project_register import project_register

    fake = _patch_admin(monkeypatch, _FakeAdmin({"ok": True}))
    out = project_register(project_id="debugging opsecrets nixos-quinyx")

    assert out["ok"] is False
    assert out["created"] is False
    assert "whitespace" in out["error"]
    assert fake.calls == []


# ── the create gate still refuses, and now names the fix ────────────────────


def test_unknown_project_error_names_the_fix():
    msg = str(UnknownProjectError("quinyx/daneal"))

    assert msg.startswith("unknown project_id: 'quinyx/daneal'")
    assert 'project_register("quinyx/daneal")' in msg
    assert "new session" in msg


def test_memorize_gate_still_refuses_an_unknown_free_text_project(monkeypatch):
    """ADR-1131: ``project=`` on a write NEVER creates a row; it fails loud."""
    import yadgar.core.forward as fwd

    calls: list[str] = []

    def _admin(op, payload, timeout_s=30.0):
        calls.append(op)
        return {"rows": [{"key": "m-agahi/yadgar"}]}

    monkeypatch.setattr(fwd, "_forward_admin", _admin)
    reg.invalidate_project_registry_cache()
    try:
        with pytest.raises(UnknownProjectError) as excinfo:
            reg.assert_project_registered_for_create("quinyx/daneal", tool="memorize")
    finally:
        reg.invalidate_project_registry_cache()

    assert "create_project_row" not in calls
    assert 'project_register("quinyx/daneal")' in str(excinfo.value)
