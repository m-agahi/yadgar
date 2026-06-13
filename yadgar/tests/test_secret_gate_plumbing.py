"""TDD tests for v5.15.0 secret-gate caller tag plumbing.

Red-first. Tests must fail before plumbing implementation.

Coverage:
  Part B — gate_or_reject(tags=) forwarding per call site:
    1. test_memorize_forwards_tags_to_gate        — memorize() passes tags= to gate
    2. test_wiki_add_forwards_tags_to_gate        — wiki_add() passes tags= to gate
    3. test_anchor_forwards_tags_to_gate          — anchor() passes tags= to gate
    4. test_checkpoint_forwards_no_tags_to_gate   — checkpoint() has no user tags; None acceptable
    5. test_update_active_work_no_tags            — update_active_work: no user tags (None ok)

  End-to-end allowlist acceptance test:
    6. test_memorize_allowlisted_tag_succeeds     — write with test-fixture tag + ghp_ content
                                                    + allowlist entry → stored, not blocked

Tags: v5.15.0, secret-gate, allowlist, plumbing
"""

from __future__ import annotations

from textwrap import dedent
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Helper: patch gate_or_reject at the secrets module level
# ---------------------------------------------------------------------------


def _patch_gate(monkeypatch, target_module_path: str, return_value=None):
    """Monkeypatch gate_or_reject in the target module and return the mock."""
    mock = MagicMock(return_value=return_value)
    monkeypatch.setattr(target_module_path, mock)
    return mock


# ---------------------------------------------------------------------------
# 1. memorize() forwards tags= to gate
# ---------------------------------------------------------------------------


class TestMemorizeForwardsTagsToGate:
    """memorize() must call gate_or_reject(content, tags=tags) not check_secrets()."""

    def test_memorize_forwards_tags_to_gate(self, monkeypatch):
        """gate_or_reject receives the tags= kwarg when memorize() is called."""
        captured_calls = []

        def fake_gate(*args, tags=None, source=None):
            captured_calls.append({"args": args, "tags": tags, "source": source})
            return None  # allow through

        # Patch gate_or_reject in the memorize module
        # Import the module directly via sys.modules to avoid __init__ re-export collision
        # Patch gate_or_reject at the phase_validate module where it is actually called.
        import yadgar.server.tools._memorize_phases._phase_validate as _pv

        monkeypatch.setattr(_pv, "gate_or_reject", fake_gate)

        # Patch storage + embeddings singletons to avoid DB
        storage_mock = MagicMock()
        storage_mock.insert_memory.return_value = 999
        storage_mock.get_memory.return_value = None

        embeddings_mock = MagicMock()
        embeddings_mock.encode.return_value = [0.1] * 384
        embeddings_mock.model_name = "test-model"

        import yadgar.server.lifecycle as _lc

        monkeypatch.setattr(_lc, "_get_storage", lambda: storage_mock)
        monkeypatch.setattr(_lc, "_get_embeddings", lambda: embeddings_mock)

        # Stub file_queue to avoid queue init
        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)

        # Stub buffer state — use MagicMock so _get_buffer() guard passes
        import yadgar.server._state as _st

        buffer_mock = MagicMock()
        monkeypatch.setattr(_st, "_buffer", buffer_mock)
        monkeypatch.setattr(_st, "_rules_engine", None)

        from yadgar.server.tools.memorize import memorize

        tags = ["test-fixture", "yadgar"]
        memorize(
            content="some content",
            context="/home/user/test",
            tags=tags,
        )

        # gate_or_reject must have been called with tags= kwarg
        assert captured_calls, "gate_or_reject was never called (memorize still uses check_secrets)"
        last_call = captured_calls[-1]
        assert last_call["tags"] is not None, (
            "gate_or_reject called without tags= kwarg — plumbing missing in memorize()"
        )
        assert set(last_call["tags"]) >= {"test-fixture"}, (
            f"Expected 'test-fixture' in tags, got: {last_call['tags']}"
        )


# ---------------------------------------------------------------------------
# 2. wiki_add() forwards tags= to gate
# ---------------------------------------------------------------------------


class TestWikiAddForwardsTagsToGate:
    """wiki_add() must call gate_or_reject(content, tags=tags) not check_secrets()."""

    def test_wiki_add_forwards_tags_to_gate(self, monkeypatch):
        """gate_or_reject receives tags= kwarg from wiki_add()."""
        captured_calls = []

        def fake_gate(*args, tags=None, source=None):
            captured_calls.append({"args": args, "tags": tags})
            return None

        import yadgar.server.tools.wiki as _wiki_mod

        monkeypatch.setattr(_wiki_mod, "gate_or_reject", fake_gate, raising=False)

        # Stub wiki state
        import yadgar.server._state as _st

        wiki_mock = MagicMock()
        monkeypatch.setattr(_st, "_wiki", wiki_mock)
        monkeypatch.setattr(_st, "_rules_engine", None)

        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)

        storage_mock = MagicMock()
        storage_mock.insert_wiki_page.return_value = 1
        storage_mock.get_wiki_page_by_slug.return_value = None

        import yadgar.server.lifecycle as _lc

        monkeypatch.setattr(_lc, "_get_storage", lambda: storage_mock)
        monkeypatch.setattr(_lc, "_get_file_queue", lambda: MagicMock())

        from yadgar.server.tools.wiki import wiki_add

        tags = ["test-fixture", "wiki"]
        wiki_add(
            title="Test Page",
            content="wiki content",
            tags=tags,
        )

        assert captured_calls, "gate_or_reject never called (wiki_add still uses check_secrets)"
        last_call = captured_calls[-1]
        assert last_call["tags"] is not None, (
            "gate_or_reject called without tags= kwarg — plumbing missing in wiki_add()"
        )
        assert "test-fixture" in last_call["tags"], (
            f"Expected 'test-fixture' in gate tags, got: {last_call['tags']}"
        )


# ---------------------------------------------------------------------------
# 3. anchor() forwards tags= to gate
# ---------------------------------------------------------------------------


class TestAnchorForwardsTagsToGate:
    """anchor() must call gate_or_reject(content, reason, tags=[..._anchor...])."""

    def test_anchor_forwards_tags_to_gate(self, monkeypatch):
        """gate_or_reject called with non-None tags= from anchor()."""
        captured_calls = []

        def fake_gate(*args, tags=None, source=None):
            captured_calls.append({"args": args, "tags": tags})
            return None

        import yadgar.server.tools.misc as _misc_mod

        monkeypatch.setattr(_misc_mod, "gate_or_reject", fake_gate)

        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)

        import yadgar.server.lifecycle as _lc

        replay_mock = MagicMock()
        replay_mock.anchor_memory.return_value = 42
        monkeypatch.setattr(_lc, "_get_replay", lambda: replay_mock)

        from yadgar.server.tools.misc import anchor

        anchor(
            content="critical fact",
            context="/home/user/project",
            reason="test reason",
        )

        assert captured_calls, "gate_or_reject never called in anchor()"
        last_call = captured_calls[-1]
        assert last_call["tags"] is not None, (
            "anchor() calls gate_or_reject without tags= — plumbing missing"
        )
        # anchor always writes with _anchor tag — it should forward that
        assert "_anchor" in last_call["tags"], (
            f"Expected '_anchor' in gate tags for anchor(), got: {last_call['tags']}"
        )


# ---------------------------------------------------------------------------
# 4. checkpoint() — no user-supplied tags; None is acceptable
# ---------------------------------------------------------------------------


class TestCheckpointGateCallAcceptable:
    """checkpoint() has no user tags; gate_or_reject(... tags=None) is acceptable for it."""

    def test_checkpoint_gate_called(self, monkeypatch):
        """gate_or_reject is called from checkpoint() (tags=None is fine)."""
        captured_calls = []

        def fake_gate(*args, tags=None, source=None):
            captured_calls.append({"args": args, "tags": tags})
            return None

        import yadgar.server.tools.misc as _misc_mod

        monkeypatch.setattr(_misc_mod, "gate_or_reject", fake_gate)

        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)

        import yadgar.server.lifecycle as _lc

        replay_mock = MagicMock()
        replay_mock.create_checkpoint.return_value = {"stored": True}
        monkeypatch.setattr(_lc, "_get_replay", lambda: replay_mock)

        import yadgar.server._state as _st

        monkeypatch.setattr(_st, "_buffer", None)

        from yadgar.server.tools.misc import checkpoint

        checkpoint(
            directory="/home/user/project",
            current_task="doing stuff",
        )

        assert captured_calls, "gate_or_reject never called in checkpoint()"
        # checkpoint has no user tags so None is fine — just assert it was called


# ---------------------------------------------------------------------------
# 5. End-to-end allowlist acceptance test
# ---------------------------------------------------------------------------


class TestMemorizeAllowlistedTagSucceeds:
    """Write a test-fixture tagged memory containing ghp_ token + allowlist entry → succeeds."""

    def test_memorize_allowlisted_tag_succeeds(self, tmp_path, monkeypatch):
        """v5.13.0 allowlist + v5.15.0 tag plumbing → write allowed through when allowlisted."""
        # Seed allowlist YAML
        allowlist_yaml = tmp_path / "allowlist.yaml"
        allowlist_yaml.write_text(
            dedent("""\
            allowlist:
              - tags: ["test-fixture"]
                patterns: ["ghp_*"]
                reason: "test fixtures may contain fake GitHub tokens"
            """)
        )
        monkeypatch.setenv("YADGAR_SECRET_GATE_ALLOWLIST_PATH", str(allowlist_yaml))
        monkeypatch.setenv("YADGAR_SECRET_GATE_AUDIT_DIR", str(tmp_path / "audit"))

        # Force reload of allowlist module state (in case prior tests loaded it)
        import yadgar.security.allowlist as _al

        _al._allowlist_loaded = False
        _al._allowlist = []
        _al._reload_allowlist()

        # Stub storage + embeddings to avoid real DB
        storage_mock = MagicMock()
        storage_mock.insert_memory.return_value = 1001
        storage_mock.get_memory.return_value = None

        embeddings_mock = MagicMock()
        embeddings_mock.encode.return_value = [0.1] * 384
        embeddings_mock.model_name = "test-model"

        import yadgar.server.lifecycle as _lc

        monkeypatch.setattr(_lc, "_get_storage", lambda: storage_mock)
        monkeypatch.setattr(_lc, "_get_embeddings", lambda: embeddings_mock)

        import yadgar.file_queue as _fq

        monkeypatch.setattr(_fq, "is_draining", lambda: True)

        import yadgar.server._state as _st

        buffer_mock = MagicMock()
        monkeypatch.setattr(_st, "_buffer", buffer_mock)
        monkeypatch.setattr(_st, "_rules_engine", None)

        from yadgar.server.tools.memorize import memorize

        fake_token = "ghp_" + "X" * 25  # gitleaks:allow — fake, test fixture
        result = memorize(
            content=f"Test memory with fake token: {fake_token}",
            context="/home/user/test",
            tags=["test-fixture", "yadgar"],
        )

        assert result.get("stored") is not False, (
            f"Expected write to succeed (allowlisted), got rejection: {result}"
        )
        assert "reason" not in result or "secret_detected" not in result.get("reason", ""), (
            f"Allowlisted write incorrectly rejected: {result}"
        )
