"""Runtime config CRUD — moved to _LedgerMixin (MariaDB, task #0119 / ADR-0163).

The four CRUD methods (set/get/list/delete_config_row) were moved from this
SurrealDB mixin to ``_LedgerMixin`` in ``ledger.py`` as part of the spine
knob-move (PR #32 review fix B1). ``_RuntimeConfigMixin`` is retained as an
empty placeholder so the ``StorageEngine`` MRO ordering stays stable; all
four methods now resolve to ``_LedgerMixin`` (MariaDB-backed).

When MariaDB is not configured (``YADGAR_MARIADB_URL`` unset), the
``_LedgerMixin`` methods raise ``RuntimeError("ledger: MariaDB engine not
initialized")`` — that is the accepted tradeoff of the knob-move decision.
"""

from __future__ import annotations


class _RuntimeConfigMixin:
    """Runtime config CRUD — moved to _LedgerMixin (MariaDB).

    Retained as an empty placeholder for MRO ordering stability. All four
    CRUD methods (set/get/list/delete_config_row) now live on ``_LedgerMixin``
    and resolve there via the ``StorageEngine`` MRO.
    """
