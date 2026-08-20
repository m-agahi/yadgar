"""Car 5 (2026-08-20 train) — the CREATE path enforces the project registry.

Three findings, one defect: every stated guarantee about ``project_id``
validation was weaker than the code claimed, and the guarantees disagreed
with each other.

1. ``_ensure_project_exists_sync`` had ZERO production call sites — only its
   definition, its ``__all__`` entry, and ~12 docstrings across ``adr.py`` /
   ``memorize.py`` / ``recall.py`` / ``task.py`` / ``wiki.py`` /
   ``_project_param.py`` claiming a registry check ran "at the backend write
   path". The only real enforcement was
   ``MariaStorageEngine.assert_project_registered``, reached from
   ``create_task_row`` / ``create_adr_row`` — the engine-#2 ledger ONLY.
   ``memory.project_id`` and ``wiki_page.project_id`` had no registry check on
   any writer.
2. CREATION was LOOSER than CORRECTION. ``resolve_effective_project``
   validated non-empty-string only, so ``memorize(project="global")`` stamped
   the ADR-0227 sentinel, while the restamp gates (ledger tasks 246 / 262)
   reject ``global`` / ``unresolved`` / ``system``. The correction path being
   stricter than the creation path is the exact asymmetry task 246 argued
   against.
3. The drainer kept a PRIVATE sentinel set (``_SENTINEL_PROJECT_IDS``) whose
   own comment claimed the sentinel set "cannot drift", while omitting
   ``"system"`` — which ``_NON_IDENTIFYING_PROJECT_IDS`` has.

RED → GREEN: this file was written before the implementation.

WHY THE CHECK IS CORE-SIDE, THROUGH ``_forward_admin``
------------------------------------------------------
``init_engines(sql_storage=False)`` is the default and only
``embed_service._ensure_recall_engines`` passes ``True`` — so
``_st._sql_storage`` is ALWAYS ``None`` in the core process. A ``_shared``
predicate reading that slot (option b) would raise
``ProjectRegistryUnavailableError`` on every core write. And the drainer
(option c) is a bare ``threading.Thread`` with no event loop, while the engine
is built with ``AsyncAdaptedQueuePool`` — driving it from there means an
``asyncio.run`` per write, the hazard this repo has written down three times
(``retrieval/superseded.py``, ``admin_exec/invariants_cross_engine.py``,
``embed_service/embed_service_lifecycle.py``). Forwarding to the backend
``/admin`` route runs the query on the backend's own loop, which is the only
placement with neither problem.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

_REGISTERED = "m-agahi/yadgar"
_UNREGISTERED = "typo/nope"
_REGISTRY_ROWS = {"rows": [{"key": _REGISTERED}, {"key": "quinyx/aws2slack"}]}


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    """Every test starts with a cold registry cache."""
    from yadgar.core.server.tools._project_registry import invalidate_project_registry_cache

    invalidate_project_registry_cache()
    yield
    invalidate_project_registry_cache()


def _queued_payloads(queue_dir: Path) -> list[dict]:
    """Re-read the DURABLE queue files — never the tool's return dict."""
    return [json.loads(p.read_text()) for p in sorted(queue_dir.glob("*.json"))]


def _fake_queue(tmp_path: Path):
    from yadgar._shared.file_queue.queue import FileQueue

    return FileQueue(tmp_path / "fq")


# ── 1. the MINIMUM BAR: sentinels are rejected at resolution ────────────────


class TestSentinelsRejectedAtResolution:
    """Creation is now AT LEAST as strict as correction (finding 2)."""

    @pytest.mark.parametrize("sentinel", ["global", "unresolved", "system"])
    def test_override_sentinel_is_rejected(self, sentinel: str) -> None:
        from yadgar.core.server.tools._project_param import (
            InvalidProjectOverrideError,
            resolve_effective_project,
        )

        with pytest.raises(InvalidProjectOverrideError) as exc:
            resolve_effective_project(
                project=sentinel,
                directory=None,
                session_project=None,
                tool="test",
            )
        assert sentinel in str(exc.value)

    @pytest.mark.parametrize("sentinel", ["global", "unresolved", "system"])
    def test_session_sentinel_is_rejected_too(self, sentinel: str) -> None:
        """A sentinel is not an identity whichever tier produced it."""
        from yadgar.core.server.tools._project_param import (
            InvalidProjectOverrideError,
            resolve_effective_project,
        )

        with pytest.raises(InvalidProjectOverrideError):
            resolve_effective_project(
                project=None,
                directory=None,
                session_project=sentinel,
                tool="test",
            )

    def test_a_real_project_id_still_resolves(self) -> None:
        from yadgar.core.server.tools._project_param import resolve_effective_project

        assert (
            resolve_effective_project(
                project=_REGISTERED,
                directory=None,
                session_project=None,
                tool="test",
            )
            == _REGISTERED
        )


# ── 2. the registry check itself ────────────────────────────────────────────


class TestRegistryCheck:
    def test_unregistered_project_id_is_rejected(self) -> None:
        from yadgar._shared.storage.sql.errors import UnknownProjectError
        from yadgar.core.server.tools._project_registry import (
            assert_project_registered_for_create,
        )

        with patch("yadgar.core.forward._forward_admin", return_value=dict(_REGISTRY_ROWS)) as fwd:
            with pytest.raises(UnknownProjectError):
                assert_project_registered_for_create(_UNREGISTERED, tool="memorize")
        assert fwd.called, "the check must actually consult the registry"

    def test_registered_project_id_passes(self) -> None:
        from yadgar.core.server.tools._project_registry import (
            assert_project_registered_for_create,
        )

        with patch("yadgar.core.forward._forward_admin", return_value=dict(_REGISTRY_ROWS)):
            assert_project_registered_for_create(_REGISTERED, tool="memorize")

    def test_registry_is_cached_between_calls(self) -> None:
        from yadgar.core.server.tools._project_registry import (
            assert_project_registered_for_create,
        )

        with patch("yadgar.core.forward._forward_admin", return_value=dict(_REGISTRY_ROWS)) as fwd:
            for _ in range(5):
                assert_project_registered_for_create(_REGISTERED, tool="memorize")
        assert fwd.call_count == 1

    def test_a_miss_forces_one_refresh_before_rejecting(self) -> None:
        """A project registered SINCE the cache was filled must not be rejected."""
        from yadgar.core.server.tools._project_registry import (
            assert_project_registered_for_create,
        )

        calls = {"n": 0}

        def _rows(op: str, payload: dict, *a, **kw):  # noqa: ARG001
            calls["n"] += 1
            if calls["n"] == 1:
                return {"rows": [{"key": _REGISTERED}]}
            return {"rows": [{"key": _REGISTERED}, {"key": "brand/new"}]}

        with patch("yadgar.core.forward._forward_admin", side_effect=_rows):
            assert_project_registered_for_create(_REGISTERED, tool="memorize")
            assert_project_registered_for_create("brand/new", tool="memorize")
        assert calls["n"] == 2

    @pytest.mark.parametrize("sentinel", ["", "global", "unresolved", "system"])
    def test_sentinels_rejected_without_consulting_the_registry(self, sentinel: str) -> None:
        from yadgar.core.server.tools._project_param import InvalidProjectOverrideError
        from yadgar.core.server.tools._project_registry import (
            assert_project_registered_for_create,
        )

        with patch("yadgar.core.forward._forward_admin") as fwd:
            with pytest.raises(InvalidProjectOverrideError):
                assert_project_registered_for_create(sentinel, tool="memorize")
        assert not fwd.called


# ── 3. engine #2 ABSENT must still permit legitimate writes ─────────────────


class TestEngineTwoAbsent:
    """``ProjectRegistryUnavailableError`` must never become "no writes"."""

    def test_engine_absent_envelope_permits_the_write(self) -> None:
        from yadgar.core.server.tools._project_registry import (
            assert_project_registered_for_create,
        )

        with patch(
            "yadgar.core.forward._forward_admin",
            return_value={"ok": False, "error": "engine #2 not composed"},
        ):
            assert_project_registered_for_create(_UNREGISTERED, tool="memorize")

    def test_backend_unreachable_permits_the_write(self) -> None:
        from yadgar.core.server.tools._project_registry import (
            assert_project_registered_for_create,
        )

        with patch(
            "yadgar.core.forward._forward_admin",
            side_effect=RuntimeError("YADGAR_EMBED_URL is not set"),
        ):
            assert_project_registered_for_create(_UNREGISTERED, tool="memorize")

    def test_sentinels_are_STILL_rejected_with_no_registry(self) -> None:
        """The minimum bar does not depend on engine #2 being composed."""
        from yadgar.core.server.tools._project_param import InvalidProjectOverrideError
        from yadgar.core.server.tools._project_registry import (
            assert_project_registered_for_create,
        )

        with patch(
            "yadgar.core.forward._forward_admin",
            side_effect=RuntimeError("YADGAR_EMBED_URL is not set"),
        ):
            with pytest.raises(InvalidProjectOverrideError):
                assert_project_registered_for_create("global", tool="memorize")


# ── 4. memorize — assert on the DURABLE queue file, not the return dict ─────


class TestMemorizeCreatePath:
    def _memorize(self, tmp_path: Path, project, registry):
        from yadgar.core.server.tools.memorize import memorize

        fq = _fake_queue(tmp_path)
        with (
            patch("yadgar.core.server.tools.memorize._get_file_queue", return_value=fq),
            patch("yadgar.core.forward._forward_admin", **registry),
        ):
            result = memorize(
                content="car 5 enforcement probe",
                context="/home/max/git/yadgar",
                tags=[],
                project=project,
            )
        return result, _queued_payloads(fq.queue_dir)

    def test_unregistered_project_writes_nothing(self, tmp_path: Path) -> None:
        result, queued = self._memorize(
            tmp_path, _UNREGISTERED, {"return_value": dict(_REGISTRY_ROWS)}
        )
        assert queued == [], "an unregistered project_id must not reach the queue"
        assert result.get("stored") is False

    @pytest.mark.parametrize("sentinel", ["global", "unresolved", "system"])
    def test_sentinel_project_writes_nothing(self, tmp_path: Path, sentinel: str) -> None:
        result, queued = self._memorize(tmp_path, sentinel, {"return_value": dict(_REGISTRY_ROWS)})
        assert queued == []
        assert result.get("stored") is False

    def test_registered_project_reaches_the_queue(self, tmp_path: Path) -> None:
        _, queued = self._memorize(tmp_path, _REGISTERED, {"return_value": dict(_REGISTRY_ROWS)})
        assert len(queued) == 1
        assert queued[0]["payload"]["project_id"] == _REGISTERED

    def test_engine_two_absent_still_writes(self, tmp_path: Path) -> None:
        _, queued = self._memorize(
            tmp_path,
            "quinyx/aws2slack",
            {"side_effect": RuntimeError("YADGAR_EMBED_URL is not set")},
        )
        assert len(queued) == 1
        assert queued[0]["payload"]["project_id"] == "quinyx/aws2slack"


# ── 5. wiki_add — same contract, same durable-state assertion ───────────────


class TestWikiAddCreatePath:
    def _wiki_add(self, tmp_path: Path, project, registry):
        from yadgar.core.server.tools.wiki import wiki_add

        fq = _fake_queue(tmp_path)
        with (
            patch("yadgar.core.server.tools.wiki._st._wiki", object()),
            patch("yadgar.core.server.tools.wiki._check_wiki_add_context", return_value={}),
            patch("yadgar.core.server.tools.wiki._get_file_queue", return_value=fq),
            patch("yadgar.core.forward._forward_admin", **registry),
        ):
            result = wiki_add(
                title="Car 5 enforcement probe",
                content="body text for the car 5 probe page",
                directory="/home/max/git/yadgar",
                project=project,
            )
        return result, _queued_payloads(fq.queue_dir)

    def test_unregistered_project_writes_nothing(self, tmp_path: Path) -> None:
        result, queued = self._wiki_add(
            tmp_path, _UNREGISTERED, {"return_value": dict(_REGISTRY_ROWS)}
        )
        assert queued == []
        assert result.get("stored") is False

    @pytest.mark.parametrize("sentinel", ["global", "unresolved", "system"])
    def test_sentinel_project_writes_nothing(self, tmp_path: Path, sentinel: str) -> None:
        result, queued = self._wiki_add(tmp_path, sentinel, {"return_value": dict(_REGISTRY_ROWS)})
        assert queued == []
        assert result.get("stored") is False

    def test_registered_project_reaches_the_queue(self, tmp_path: Path) -> None:
        _, queued = self._wiki_add(tmp_path, _REGISTERED, {"return_value": dict(_REGISTRY_ROWS)})
        assert len(queued) == 1
        assert queued[0]["payload"]["project_id"] == _REGISTERED

    def test_engine_two_absent_still_writes(self, tmp_path: Path) -> None:
        _, queued = self._wiki_add(
            tmp_path,
            "quinyx/aws2slack",
            {"side_effect": RuntimeError("YADGAR_EMBED_URL is not set")},
        )
        assert len(queued) == 1
        assert queued[0]["payload"]["project_id"] == "quinyx/aws2slack"


# ── 6. the drainer's private sentinel set must not drift ────────────────────


class TestDrainerSentinelSetIsTheSharedOne:
    """Finding 3: the un-bypassable backend half kept a private copy."""

    def test_drainer_uses_the_shared_frozenset(self) -> None:
        from yadgar._shared.storage._project_id_writer import _NON_IDENTIFYING_PROJECT_IDS
        from yadgar.backend.queue_drainer.dlq import _DLQMixin

        assert _DLQMixin._SENTINEL_PROJECT_IDS is _NON_IDENTIFYING_PROJECT_IDS

    def test_system_is_rejected_by_the_drainer(self) -> None:
        from yadgar.backend.queue_drainer.dlq import _DLQMixin

        reason = _DLQMixin()._validate_project_id({"project_id": "system"}, "memorize")
        assert reason is not None
        assert "missing_project_id" in reason


# ── 7. the docstrings must stop claiming an enforcement that never ran ──────


class TestNoFalseEnforcementClaims:
    """A docstring claiming enforcement that does not exist IS the defect class.

    ``_ensure_project_exists_sync`` is unreachable from every process that
    would need it (see the module docstring), so no core-side surface may go
    on naming it as the thing that validates a caller's ``project=``.
    """

    def test_core_never_names_the_dead_guard(self) -> None:
        from yadgar.tests._paths import REPO_ROOT

        core = REPO_ROOT / "yadgar" / "core"
        # The ONE sanctioned mention: the module that replaced the dead guard
        # has to name it to explain why it is dead and unusable.
        allowed = {core / "server" / "tools" / "_project_registry.py"}
        offenders = [
            str(p.relative_to(REPO_ROOT))
            for p in core.rglob("*.py")
            if p not in allowed and "_ensure_project_exists" in p.read_text(encoding="utf-8")
        ]
        assert offenders == [], (
            "these core files still claim `_ensure_project_exists_sync` enforces "
            f"something: {offenders}"
        )
