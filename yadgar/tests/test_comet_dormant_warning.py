"""BC-EN2b: COMET disabled → config reports it disabled + exactly ONE startup warning.

COMET retired to dormant per ADR-0004 (en2a ablation: net-negative recall). The
daemon emits exactly one startup WARNING announcing the dormant state when the
flag is off, and emits none when it is (re-)enabled. Hermetic — no model load,
no engine init: we call the startup-warning helper directly with a stub Settings.
"""

from __future__ import annotations

import logging

from yadgar.config import Settings
from yadgar.config_registry import build_config_table, warn_comet_dormant


class TestBCEN2bStartupWarning:
    def test_disabled_emits_exactly_one_warning(self, caplog):
        settings = Settings(COMET_ENRICHMENT_ENABLED=False)
        with caplog.at_level(logging.WARNING, logger="yadgar.config_registry"):
            warn_comet_dormant(settings)
        comet_warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING and "COMET" in r.getMessage()
        ]
        assert len(comet_warnings) == 1, (
            f"expected exactly one COMET dormant warning, got {len(comet_warnings)}: "
            f"{[r.getMessage() for r in comet_warnings]}"
        )
        assert "ADR-0004" in comet_warnings[0].getMessage()

    def test_enabled_emits_no_warning(self, caplog):
        settings = Settings(COMET_ENRICHMENT_ENABLED=True)
        with caplog.at_level(logging.WARNING, logger="yadgar.config_registry"):
            warn_comet_dormant(settings)
        comet_warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING and "COMET" in r.getMessage()
        ]
        assert comet_warnings == [], (
            f"expected no COMET warning when enabled, got {[r.getMessage() for r in comet_warnings]}"
        )


class TestBCEN2bConfigReportsDisabled:
    """Second clause: config surfaces the COMET flag (via the registry / /admin/config)."""

    def test_comet_flag_present_in_config_table(self):
        table = build_config_table()
        entry = next((e for e in table if e["name"] == "YADGAR_COMET_ENRICHMENT_ENABLED"), None)
        assert entry is not None, (
            "COMET flag must be surfaced in the config table so /admin/config reports it"
        )
        # "config reports it DISABLED" — the resolved value must be false by default.
        assert str(entry["value"]).lower() == "false", (
            f"COMET flag must report disabled by default, got {entry['value']!r}"
        )
