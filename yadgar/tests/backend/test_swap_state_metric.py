"""P0 #37 item 5c — yadgar_store_swap_state gauge (silence must be impossible).

The 07-09 split-brain persisted silently for 16 h and the torn-stop warning
lived only in journalctl. This gauge surfaces the on-disk stop/swap state at
every backend /metrics scrape so the PLT dashboard/alerting (task #23) can see:

  torn_marker  — SURREAL_UNCLEAN_STOP marker present (safe-stop detected a torn stop)
  split_brain  — SURREAL_SPLIT_BRAIN marker present (inode guard caught fds
                 outside the canonical store dir)
  retained_old — a surreal_db.old-* dir exists (a half-swapped state that the
                 #37 rollback should have made impossible — investigate)
  clean        — none of the above

Flags are computed at scrape time from SURREAL_DATA_ROOT / YADGAR_LOG_DIR.
"""

from __future__ import annotations

from pathlib import Path

from prometheus_client import generate_latest

from yadgar.backend.embed_service import embed_service_metrics as m

# ---------------------------------------------------------------------------
# _swap_state_flags (pure)
# ---------------------------------------------------------------------------


class TestSwapStateFlags:
    def test_clean_when_nothing_present(self, tmp_path):
        data = tmp_path / "swap-data"
        logs = tmp_path / "swap-logs"
        data.mkdir(exist_ok=True)
        logs.mkdir(exist_ok=True)
        flags = m._swap_state_flags(data_dir=data, log_dir=logs)
        assert flags == {"torn_marker": 0, "split_brain": 0, "retained_old": 0, "clean": 1}

    def test_torn_marker_flag(self, tmp_path):
        (tmp_path / "SURREAL_UNCLEAN_STOP").write_text("reason=timeout")
        flags = m._swap_state_flags(data_dir=tmp_path, log_dir=tmp_path)
        assert flags["torn_marker"] == 1
        assert flags["clean"] == 0

    def test_split_brain_flag(self, tmp_path):
        (tmp_path / "SURREAL_SPLIT_BRAIN").write_text("fd_target=/data/surreal_db.old-x/f")
        flags = m._swap_state_flags(data_dir=tmp_path, log_dir=tmp_path)
        assert flags["split_brain"] == 1
        assert flags["clean"] == 0

    def test_retained_old_flag(self, tmp_path):
        (tmp_path / "surreal_db.old-20260709_191332").mkdir()
        flags = m._swap_state_flags(data_dir=tmp_path, log_dir=tmp_path)
        assert flags["retained_old"] == 1
        assert flags["clean"] == 0

    def test_missing_dirs_do_not_raise(self, tmp_path):
        flags = m._swap_state_flags(
            data_dir=tmp_path / "absent-data", log_dir=tmp_path / "absent-logs"
        )
        assert flags["clean"] == 1


# ---------------------------------------------------------------------------
# Gauge export at scrape time (env-driven)
# ---------------------------------------------------------------------------


class TestGaugeScrape:
    def _scrape(self) -> str:
        return generate_latest(m._registry).decode()

    def test_all_states_present_in_scrape(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SURREAL_DATA_ROOT", str(tmp_path))
        monkeypatch.setenv("YADGAR_LOG_DIR", str(tmp_path))
        text = self._scrape()
        for state in ("clean", "retained_old", "torn_marker", "split_brain"):
            assert f'yadgar_store_swap_state{{state="{state}"}}' in text

    def test_scrape_reflects_torn_marker_live(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SURREAL_DATA_ROOT", str(tmp_path))
        monkeypatch.setenv("YADGAR_LOG_DIR", str(tmp_path))
        assert 'yadgar_store_swap_state{state="torn_marker"} 0.0' in self._scrape()
        (Path(tmp_path) / "SURREAL_UNCLEAN_STOP").write_text("reason=nonzero-exit")
        text = self._scrape()
        assert 'yadgar_store_swap_state{state="torn_marker"} 1.0' in text, (
            "the gauge must reflect the marker at SCRAPE time (no restart needed)"
        )
        assert 'yadgar_store_swap_state{state="clean"} 0.0' in text

    def test_scrape_reflects_retained_old_live(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SURREAL_DATA_ROOT", str(tmp_path))
        monkeypatch.setenv("YADGAR_LOG_DIR", str(tmp_path))
        (Path(tmp_path) / "surreal_db.old-20260709_191332").mkdir()
        text = self._scrape()
        assert 'yadgar_store_swap_state{state="retained_old"} 1.0' in text
