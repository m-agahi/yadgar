"""BC-EN2b: COMET disabled → config reports it disabled + exactly ONE startup warning.

COMET retired to dormant per ADR-0004 (en2a ablation: net-negative recall). The
daemon emits exactly one startup WARNING announcing the dormant state when the
flag is off, and emits none when it is (re-)enabled. Hermetic — no model load,
no engine init: we call the startup-warning helper directly with a stub Settings.
"""

from __future__ import annotations

import logging

from yadgar._shared.config import Settings
from yadgar._shared.config.config_registry import build_config_table, warn_comet_dormant


class TestBCEN2bStartupWarning:
    def test_disabled_emits_exactly_one_warning(self, caplog):
        settings = Settings(COMET_ENRICHMENT_ENABLED=False)
        with caplog.at_level(logging.WARNING, logger="yadgar._shared.config.config_registry"):
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
        with caplog.at_level(logging.WARNING, logger="yadgar._shared.config.config_registry"):
            warn_comet_dormant(settings)
        comet_warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING and "COMET" in r.getMessage()
        ]
        assert comet_warnings == [], (
            f"expected no COMET warning when enabled, got {[r.getMessage() for r in comet_warnings]}"
        )


class TestBCEN2bWarningNotSwallowed:
    """BC-EN2b hardening: the dormant warning must fire even if the sibling
    startup-diagnostics calls (emit_startup_config_log / _set_config_gauges)
    raise. Previously all three shared ONE try/except whose `except` logged at
    DEBUG, so a raise in either sibling silently skipped the warning.
    """

    def test_warning_fires_even_when_config_log_raises(self, caplog, monkeypatch):
        import yadgar._shared.config.config_registry as _cr
        from yadgar._shared.runtime.lifecycle import _emit_startup_diagnostics

        def _boom(*_a, **_k):
            raise RuntimeError("emit_startup_config_log exploded in the container")

        monkeypatch.setattr(_cr, "emit_startup_config_log", _boom)

        settings = Settings(COMET_ENRICHMENT_ENABLED=False)
        with caplog.at_level(logging.WARNING, logger="yadgar._shared.config.config_registry"):
            # Must NOT raise (diagnostics are non-fatal) AND must still warn.
            _emit_startup_diagnostics(settings)

        comet_warnings = [
            r for r in caplog.records if r.levelno == logging.WARNING and "COMET" in r.getMessage()
        ]
        assert len(comet_warnings) == 1, (
            "warn_comet_dormant must still emit even when emit_startup_config_log raises; "
            f"got {[r.getMessage() for r in comet_warnings]}"
        )
        assert "ADR-0004" in comet_warnings[0].getMessage()


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
