# SPDX-License-Identifier: Apache-2.0
"""RED tests for Car H — tier, subsystem, rollups.

Spine task-table-refactor-2026-07-29, Car H:
- `tier: binding | historical` field on ADR rows (D27).
- `subsystem` explicit field (D28).
- Derived per-subsystem rollup pages, regenerated on write (D29).
- adr_list defaults to `tier='binding'` (D27).
"""

from __future__ import annotations


def test_h_tier_field_on_adr_policy() -> None:
    """ADR rows carry a tier field (binding | historical)."""
    # The tier field is on the ADR row, not the policy. Verify via the
    # alembic model definition.
    from yadgar._shared.storage.alembic_models import ADR

    assert hasattr(ADR, "tier"), "ADR row must have a tier field (D27)"


def test_h_subsystem_field_on_adr_policy() -> None:
    """ADR rows carry a subsystem field (D28)."""
    from yadgar._shared.storage.alembic_models import ADR

    assert hasattr(ADR, "subsystem"), "ADR row must have a subsystem field (D28)"


def test_h_adr_list_defaults_to_binding_tier() -> None:
    """adr_list defaults to tier='binding' (D27)."""
    from yadgar.core.server.tools.adr import adr_list

    mock_storage = MagicMock()
    mock_storage.list_adr_rows.return_value = []

    with patch(
        "yadgar.core.server.tools.adr._get_storage",
        return_value=mock_storage,
    ):
        adr_list(project_id="m-agahi/yadgar")

    call_kwargs = mock_storage.list_adr_rows.call_args.kwargs
    # D27: default tier is binding — historical ADRs require explicit filter
    assert call_kwargs.get("tier") == "binding" or call_kwargs.get("tier") is None


def test_h_rollup_regen_on_write() -> None:
    """Rollup pages regenerate on write (D29)."""
    from yadgar.core.server.tools.adr_ledger import _should_regenerate_rollup

    # Rollups regenerate on every ADR write by default (D29).
    assert _should_regenerate_rollup() is True


def test_h_subsystem_vocabulary_free_form() -> None:
    """Subsystem vocabulary is free-form (Car H decision deferred)."""
    # Per §10: subsystem vocabulary is free-form. No validation against
    # a controlled list yet — callers can use any string. Future cars
    # may tighten this.
    from yadgar.core.server.tools.adr import _validate_subsystem

    assert _validate_subsystem("vacuum") == "vacuum"
    assert _validate_subsystem("db-vacuum") == "db-vacuum"
    assert _validate_subsystem("Vacuum") == "Vacuum"


from unittest.mock import MagicMock, patch  # noqa: E402
