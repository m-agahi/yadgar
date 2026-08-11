"""v5.42.6 — the enforcement-off knob (YADGAR_DIRECTORY_ENFORCEMENT) is INERT.

ADR-0215 removed branch scoping, and with it the YADGAR_BRANCH_ENFORCEMENT half
of this file (K3-K6, K8, K10, K12 and the branch arms of K13-K14, K15). C5 of
the 0047 spine train (ADR-0227) then deleted the DIRECTORY half of the knob
itself, across config.py, config_registry.py, config_yaml.py,
queue_drainer/dlq.py, routes/control.py and tools/wiki.py: "relaxed enforcement
is the mode in which unscoped rows entered the corpus", and a knob whose OFF
position disables a scoping guarantee cannot coexist with an identity contract
that is fail-loud by construction.

**So this file is inverted, not deleted.** Every assertion that used to pin
"OFF relaxes the check" now pins "OFF changes nothing" — which is the more
valuable test of the two, because the failure mode being guarded against is an
operator (or a future car) resurrecting the escape hatch and quietly reopening
the hole. A deleted file would guard nothing; a file asserting inertness fails
loudly the moment the knob is rewired.

Coverage after the inversion:
K1.  enforcement "off" → the missing-directory record is STILL rejected.
K2.  enforcement "on"  → rejected (unchanged; the default was always this).
K7.  no relaxation WARN is logged, because no write is relaxed any more.
K9.  yadgar_writes_with_enforcement_relaxed{enforcement="directory"} never
     increments. The metric NAME is retained deliberately (C5: a metric that
     vanishes breaks dashboards and alert rules that outlive the code), so the
     assertion is that it stays pinned at its prior value.
K11. every falsy spelling (false/0/FALSE/False) is inert, not just the default.
K13. unset → rejected (unchanged).
K14. garbage/truthy → rejected (unchanged).
K16. the MCP boundary: wiki_add with no directory still returns an error with
     the knob off. It now returns unresolved_project rather than
     missing_directory — C5 replaced _missing_directory_error with the
     structured raise — so the assertion names the error it must be, not merely
     one it must not be. The old form (`!= "missing_directory"`) survived C5
     GREEN while testing nothing at all.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_drainer(tmp_path):
    """Build a minimal QueueDrainer with a real FileQueue for unit tests."""
    import yadgar._shared.runtime.state as _st
    from yadgar.backend.queue_drainer import FileQueue, QueueDrainer

    q = FileQueue(tmp_path)
    return QueueDrainer(queue=q, storage_factory=lambda: _st._storage, drain_interval=9999)


def _missing_directory_record() -> dict:
    """wiki_add record with all required fields EXCEPT directory_context."""
    return {
        "op": "wiki_add",
        "payload": {
            "wiki_schema_version": 2,
            "slug": "enforcement-test",
            "title": "Enforcement Test",
            "content": "Non-degenerate content for enforcement knob test.",
            "category": "reference",
            "tags": ["test"],
            # directory_context deliberately absent
        },
    }


# ── K1-K2: directory enforcement knob on _validate_wiki_add ──────────────────


class TestDirectoryEnforcementKnob:
    """K1-K2: YADGAR_DIRECTORY_ENFORCEMENT no longer controls anything."""

    def test_enforcement_off_still_rejects_missing_directory(self, tmp_path):
        """K1 (inverted): YADGAR_DIRECTORY_ENFORCEMENT=false relaxes nothing.

        This assertion used to be ``result is None``. C5 deleted the knob's
        only reader in ``_validate_wiki_add``, so the env var is now an
        unread string in the environment — setting it must not reopen the
        hole it used to open.
        """
        drainer = _make_drainer(tmp_path)
        record = _missing_directory_record()

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "false"}):
            result = drainer._validate_wiki_add(record)

        assert result is not None, (
            "C5/ADR-0227: the enforcement escape hatch is deleted — "
            f"'false' must not pass a missing-directory record; got: {result!r}"
        )
        assert "directory" in result.lower()

    def test_enforcement_on_rejects_missing_directory(self, tmp_path):
        """K2: YADGAR_DIRECTORY_ENFORCEMENT=true (default) → rejects missing directory."""
        drainer = _make_drainer(tmp_path)
        record = _missing_directory_record()

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "true"}):
            result = drainer._validate_wiki_add(record)

        assert result is not None, "directory enforcement ON should reject missing-directory record"
        assert "directory" in result.lower()

    def test_the_two_knob_positions_are_indistinguishable(self, tmp_path):
        """The inertness stated as one assertion: OFF and ON produce the same answer.

        K1 and K2 each pin one position; a future car could satisfy both while
        still branching on the variable. This pins that there is no branch.
        """
        drainer = _make_drainer(tmp_path)

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "false"}):
            off = drainer._validate_wiki_add(_missing_directory_record())
        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "true"}):
            on = drainer._validate_wiki_add(_missing_directory_record())

        assert off == on


# ── K7: WARN log fires when enforcement is relaxed ───────────────────────────


class TestEnforcementRelaxedWarnLog:
    """K7 (inverted): there is no relaxation left to warn about."""

    def test_no_relaxation_warning_is_logged_when_the_knob_is_off(self, tmp_path, caplog):
        """K7 (inverted): the WARN existed to announce a relaxed write; none happen now.

        The old assertion required a WARN naming 'directory' and 'enforcement'.
        That log line was the audit trail for a write that passed *because*
        enforcement was off — C5 deleted the branch that emitted it, so its
        continued presence would mean the branch came back.
        """
        drainer = _make_drainer(tmp_path)
        record = _missing_directory_record()

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "false"}):
            with caplog.at_level(logging.WARNING):
                result = drainer._validate_wiki_add(record)

        assert result is not None, "the record must still be rejected"
        assert not any(
            "enforcement" in m.lower() and "relax" in m.lower() for m in caplog.messages
        ), f"a relaxation WARN implies the deleted escape hatch is live: {caplog.messages}"


# ── K9: metric counter increments when enforcement relaxed ───────────────────


class TestEnforcementRelaxedMetric:
    """K9 (inverted): the counter is retained at rest and never increments."""

    def test_directory_relaxed_metric_never_increments(self, tmp_path):
        """K9 (inverted): the metric NAME survives C5; the increment does not.

        C5 kept ``yadgar_writes_with_enforcement_relaxed`` registered on
        purpose — "a metric name that vanishes breaks dashboards and alert
        rules outliving the code" — so this is not a dead-symbol test. It is
        the assertion that the retained name stays at rest: any increment
        means a write passed because enforcement was relaxed, which is the
        exact event C5 made impossible.
        """
        drainer = _make_drainer(tmp_path)
        record = _missing_directory_record()

        before = _get_counter_value(
            "yadgar_writes_with_enforcement_relaxed_total", {"enforcement": "directory"}
        )

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "false"}):
            drainer._validate_wiki_add(record)

        after = _get_counter_value(
            "yadgar_writes_with_enforcement_relaxed_total", {"enforcement": "directory"}
        )
        assert after == before, (
            f"a relaxed-write increment means the escape hatch is live; before={before}, after={after}"
        )


def _get_counter_value(metric_name: str, labels: dict) -> float:
    """Read a prometheus counter value from yadgar's private registry.

    metric_name should include the _total suffix (e.g. 'foo_total').
    """
    from yadgar._shared.observability.metrics import _registry  # noqa: PLC0415

    for metric in _registry.collect():
        for sample in metric.samples:
            if sample.name == metric_name and sample.labels == labels:
                return sample.value
    return 0.0


# ── K11, K13-K14: env parsing edges ──────────────────────────────────────────


class TestEnforcementEnvParsing:
    """K11, K13-K14: env var parsing for the directory enforcement knob."""

    @pytest.mark.parametrize("val", ["false", "0", "FALSE", "False"])
    def test_directory_enforcement_false_values_are_inert(self, tmp_path, val):
        """K11 (inverted): every falsy spelling is inert, not just the default one.

        Parametrised deliberately: a resurrection of the knob would most
        plausibly re-add one spelling (``"false"``) while the others stayed
        dead, which a single-value assertion would miss.
        """
        drainer = _make_drainer(tmp_path)
        record = _missing_directory_record()

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": val}):
            result = drainer._validate_wiki_add(record)

        assert result is not None, (
            f"YADGAR_DIRECTORY_ENFORCEMENT={val!r} must not turn off a deleted knob; got: {result!r}"
        )

    def test_unset_env_defaults_on(self, tmp_path):
        """K13: unset YADGAR_DIRECTORY_ENFORCEMENT → defaults ON."""
        drainer = _make_drainer(tmp_path)

        # Remove var if set in environment
        clean_env = {k: v for k, v in os.environ.items() if k != "YADGAR_DIRECTORY_ENFORCEMENT"}

        with patch.dict(os.environ, clean_env, clear=True):
            dir_result = drainer._validate_wiki_add(_missing_directory_record())

        assert dir_result is not None, "unset YADGAR_DIRECTORY_ENFORCEMENT should default ON"

    @pytest.mark.parametrize("val", ["true", "1", "TRUE", "True", "banana", "yes"])
    def test_garbage_or_truthy_values_default_on(self, tmp_path, val):
        """K14: garbage or truthy values → fail-safe ON."""
        drainer = _make_drainer(tmp_path)

        with patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": val}):
            dir_result = drainer._validate_wiki_add(_missing_directory_record())

        assert dir_result is not None, (
            f"YADGAR_DIRECTORY_ENFORCEMENT={val!r} should be ON (fail-safe)"
        )


# ── K16: MCP boundary enforcement knob ────────────────────────────────────────


class TestMCPBoundaryEnforcementKnobs:
    """K16: the enforcement knob gates the MCP boundary rejection site in wiki.py.

    Tests call wiki_add() directly (not via drainer). is_draining() is patched
    to False so the MCP boundary code runs. The file-queue enqueue is patched
    out so no real queue write occurs.
    """

    def test_directory_enforcement_off_still_rejects_at_the_mcp_boundary(self):
        """K16 (inverted): the knob does not buy a pass at the MCP boundary either."""
        from unittest.mock import MagicMock, patch

        import yadgar._shared.runtime.state as _st
        from yadgar.core.server.tools.wiki import wiki_add

        _fake_queue = MagicMock()
        _fake_queue.enqueue.return_value = None
        _fake_wiki = MagicMock()

        with (
            patch.dict(os.environ, {"YADGAR_DIRECTORY_ENFORCEMENT": "false"}),
            # R3 Car 1: wiki.py no longer imports is_draining (wiki_add always
            # enqueues); the symbol lives in the backend queue_drainer.
            patch("yadgar.backend.queue_drainer.is_draining", return_value=False),
            patch("yadgar.core.server.tools.wiki._get_file_queue", return_value=_fake_queue),
            patch.object(_st, "_wiki", _fake_wiki),
        ):
            result = wiki_add(
                title="Test Page",
                content="Test content for MCP boundary knob test.",
                category="reference",
                tags=["test"],
                # directory omitted — would normally hard-reject
                directory=None,
            )

        # The old assertion was `!= "missing_directory"`, which C5 satisfied
        # without testing anything: the boundary now raises unresolved_project
        # instead, so a negative assertion passes even if the call were to
        # succeed outright. Name the error it must BE.
        assert result.get("error") == "unresolved_project", (
            "C5 replaced _missing_directory_error with the structured raise; a call "
            f"naming no project must still be refused with the knob off. got: {result!r}"
        )
        assert result.get("stored") is not True, (
            f"the knob must not let an unscoped page through to the queue: {result!r}"
        )
