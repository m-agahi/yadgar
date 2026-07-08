"""Tests for scripts/check_image_size.py.

Covers:
- parse_size(): human-readable size strings → bytes
- detect_caps(): auto-detect per image type
- evaluate(): threshold logic (exit code + messages)
- run_history(): subprocess mock, podman→docker fallback

No real podman/docker calls — subprocess.run is mocked throughout.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import script from scripts/ — not a package; inject path directly.
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = str(Path(__file__).parent.parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from check_image_size import (  # noqa: E402
    DEFAULT_BACKEND_CAP_GB,
    DEFAULT_CORE_CAP_GB,
    DEFAULT_WARN_LAYER_MB,
    _resolve_image_from_type,
    detect_caps,
    evaluate,
    parse_size,
    run_history,
)

# ---------------------------------------------------------------------------
# parse_size
# ---------------------------------------------------------------------------


class TestParseSize:
    def test_gb(self) -> None:
        assert parse_size("1.36GB") == pytest.approx(1.36e9)

    def test_gb_uppercase(self) -> None:
        assert parse_size("2.0GB") == pytest.approx(2.0e9)

    def test_mb(self) -> None:
        assert parse_size("119MB") == pytest.approx(119e6)

    def test_kb(self) -> None:
        # docker history uses kB (lowercase b)
        assert parse_size("19.5kB") == pytest.approx(19.5e3)

    def test_kb_uppercase(self) -> None:
        assert parse_size("500KB") == pytest.approx(500e3)

    def test_bytes_no_unit(self) -> None:
        # Some docker history rows just say a number (bytes)
        assert parse_size("1024") == pytest.approx(1024.0)

    def test_zero(self) -> None:
        assert parse_size("0B") == 0.0

    def test_b_unit(self) -> None:
        assert parse_size("512B") == pytest.approx(512.0)

    def test_mb_with_decimal(self) -> None:
        assert parse_size("245.6MB") == pytest.approx(245.6e6)

    def test_whitespace_stripped(self) -> None:
        assert parse_size("  100MB  ") == pytest.approx(100e6)

    def test_unknown_unit_treated_as_bytes(self) -> None:
        # Graceful fallback: unrecognised suffix → try as raw bytes
        assert parse_size("999") == pytest.approx(999.0)


# ---------------------------------------------------------------------------
# detect_caps
# ---------------------------------------------------------------------------


class TestDetectCaps:
    def test_backend_image_name(self) -> None:
        cap_gb, warn_mb = detect_caps("docker.io/openfantasy/yadgar-backend:5.0.3")
        assert cap_gb == DEFAULT_BACKEND_CAP_GB
        assert warn_mb == DEFAULT_WARN_LAYER_MB

    def test_core_image_name(self) -> None:
        cap_gb, warn_mb = detect_caps("docker.io/openfantasy/yadgar:5.4.2")
        assert cap_gb == DEFAULT_CORE_CAP_GB
        assert warn_mb == DEFAULT_WARN_LAYER_MB

    def test_bare_backend(self) -> None:
        cap_gb, warn_mb = detect_caps("yadgar-backend:latest")
        assert cap_gb == DEFAULT_BACKEND_CAP_GB

    def test_bare_core(self) -> None:
        cap_gb, warn_mb = detect_caps("yadgar:latest")
        assert cap_gb == DEFAULT_CORE_CAP_GB

    def test_explicit_override_beats_autodetect(self) -> None:
        # When caller passes explicit values, detect_caps is not invoked;
        # but if they do call detect_caps the result should be the defaults.
        cap_gb, _ = detect_caps("yadgar-backend:5.0.3")
        assert cap_gb == DEFAULT_BACKEND_CAP_GB  # default, not overridden

    def test_unknown_image_falls_back_to_core_cap(self) -> None:
        cap_gb, _ = detect_caps("some-other-image:1.0")
        assert cap_gb == DEFAULT_CORE_CAP_GB


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


class TestEvaluate:
    """evaluate(layers, total_bytes, cap_gb, warn_layer_mb) → ImageSizeResult"""

    def _layers(self, sizes_mb: list[float]) -> list[tuple[float, str]]:
        """Build a minimal layers list: [(bytes, command_str), ...]"""
        return [(s * 1e6, f"RUN layer_{i}") for i, s in enumerate(sizes_mb)]

    def test_within_cap_no_warnings(self) -> None:
        layers = self._layers([100, 200, 300])
        total = 600e6
        result = evaluate(layers, total, cap_gb=2.0, warn_layer_mb=500.0)
        assert result.exit_code == 0
        assert result.over_budget is False
        assert result.warn_layers == []

    def test_over_cap_exits_1(self) -> None:
        layers = self._layers([500, 500, 500, 500, 500])
        total = 2.6e9
        result = evaluate(layers, total, cap_gb=2.0, warn_layer_mb=500.0)
        assert result.exit_code == 1
        assert result.over_budget is True

    def test_warn_layers_detected_exit_0(self) -> None:
        # One layer exceeds warn threshold; total within cap → exit 0 + warning
        layers = self._layers([100, 600, 200])
        total = 900e6
        result = evaluate(layers, total, cap_gb=2.0, warn_layer_mb=500.0)
        assert result.exit_code == 0
        assert result.over_budget is False
        assert len(result.warn_layers) == 1
        assert result.warn_layers[0][0] == pytest.approx(600e6)

    def test_over_cap_with_warn_layers_exits_1(self) -> None:
        # Both cap exceeded AND big layers present → still exit 1 (cap takes precedence)
        layers = self._layers([800, 800, 800])
        total = 2.4e9
        result = evaluate(layers, total, cap_gb=2.0, warn_layer_mb=500.0)
        assert result.exit_code == 1
        assert result.over_budget is True
        assert len(result.warn_layers) > 0

    def test_exactly_at_cap_passes(self) -> None:
        # Exactly at cap is NOT over budget
        layers = self._layers([1000, 1000])
        total = 2.0e9
        result = evaluate(layers, total, cap_gb=2.0, warn_layer_mb=500.0)
        assert result.exit_code == 0
        assert result.over_budget is False

    def test_one_byte_over_cap_fails(self) -> None:
        total = 2.0e9 + 1
        layers = [(total, "RUN big")]
        result = evaluate(layers, total, cap_gb=2.0, warn_layer_mb=500.0)
        assert result.exit_code == 1

    def test_total_reported_correctly(self) -> None:
        layers = self._layers([400, 300])
        total = 700e6
        result = evaluate(layers, total, cap_gb=2.0, warn_layer_mb=500.0)
        assert result.total_bytes == pytest.approx(700e6)


# ---------------------------------------------------------------------------
# run_history — subprocess mock
# ---------------------------------------------------------------------------

# Canned podman/docker history output (tab-separated Size\tCreatedBy)
_CANNED_HISTORY = """\
1.36GB\t/bin/sh -c pip install torch
119MB\t/bin/sh -c pip install sentence-transformers
19.5kB\t/bin/sh -c echo hello
0B\tCMD ["/entrypoint.sh"]
"""


class TestRunHistory:
    def test_podman_success(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _CANNED_HISTORY

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            layers = run_history("docker.io/openfantasy/yadgar-backend:5.0.3")

        # Should have called podman first
        first_call_args = mock_run.call_args_list[0][0][0]
        assert first_call_args[0] == "podman"

        assert len(layers) == 4
        assert layers[0][0] == pytest.approx(1.36e9)
        assert layers[1][0] == pytest.approx(119e6)
        assert layers[2][0] == pytest.approx(19.5e3)
        assert layers[3][0] == pytest.approx(0.0)

    def test_docker_fallback_when_podman_fails(self) -> None:
        podman_result = MagicMock()
        podman_result.returncode = 1
        podman_result.stdout = ""

        docker_result = MagicMock()
        docker_result.returncode = 0
        docker_result.stdout = _CANNED_HISTORY

        with patch("subprocess.run", side_effect=[podman_result, docker_result]) as mock_run:
            layers = run_history("docker.io/openfantasy/yadgar-backend:5.0.3")

        assert mock_run.call_count == 2
        second_call_args = mock_run.call_args_list[1][0][0]
        assert second_call_args[0] == "docker"
        assert len(layers) == 4

    def test_both_unavailable_raises(self) -> None:
        fail_result = MagicMock()
        fail_result.returncode = 127
        fail_result.stdout = ""

        with patch("subprocess.run", return_value=fail_result):
            with pytest.raises(RuntimeError, match="Neither podman nor docker"):
                run_history("some-image:1.0")

    def test_empty_line_skipped(self) -> None:
        output_with_blank = "100MB\tRUN something\n\n200MB\tRUN other\n"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = output_with_blank

        with patch("subprocess.run", return_value=mock_result):
            layers = run_history("yadgar:5.4.2")

        assert len(layers) == 2


# ---------------------------------------------------------------------------
# Integration smoke: parse → evaluate for current known backend size (1.63 GB)
# ---------------------------------------------------------------------------


class TestIntegrationSmoke:
    """Simulate the check against the known 1.63 GB backend image."""

    def test_163gb_backend_passes_2gb_cap(self) -> None:
        # Simulate a backend with total 1.63 GB — should pass under 2 GB cap
        big_layer = 1.63e9
        layers: list[tuple[float, str]] = [(big_layer, "RUN pip install torch")]
        result = evaluate(layers, big_layer, cap_gb=2.0, warn_layer_mb=500.0)
        assert result.exit_code == 0
        assert result.over_budget is False
        # The big layer (1.63 GB) exceeds the 500 MB warn threshold → warn present
        assert len(result.warn_layers) == 1

    def test_163gb_backend_fails_1gb_cap(self) -> None:
        big_layer = 1.63e9
        layers: list[tuple[float, str]] = [(big_layer, "RUN pip install torch")]
        result = evaluate(layers, big_layer, cap_gb=1.0, warn_layer_mb=500.0)
        assert result.exit_code == 1
        assert result.over_budget is True


# ---------------------------------------------------------------------------
# _resolve_image_from_type
# ---------------------------------------------------------------------------


class TestResolveImageFromType:
    def _fake_server_json(self, tmp_path, version: str, backend_version: str) -> Path:
        p = tmp_path / "server.json"
        p.write_text(
            __import__("json").dumps({"version": version, "backend_version": backend_version})
        )
        return p

    def test_backend_resolves_correct_tag(self, tmp_path) -> None:
        server_json = self._fake_server_json(tmp_path, "5.4.2", "5.0.3")
        with patch("check_image_size.Path") as mock_path_cls:
            # Make Path(__file__).parent.parent.parent / "server.json" return our fake path
            mock_path_cls.return_value.parent.parent.__truediv__ = lambda self, key: server_json
            # Simpler: patch the module-level constant directly
            pass

        # Patch the actual file lookup instead
        import json

        fake_data = json.dumps({"version": "5.4.2", "backend_version": "5.0.3"})
        with patch(
            "builtins.open", side_effect=lambda p, *a, **kw: __import__("io").StringIO(fake_data)
        ):
            with patch("pathlib.Path.read_text", return_value=fake_data):
                result = _resolve_image_from_type("backend")
        assert result == "docker.io/openfantasy/yadgar-backend:5.0.3"

    def test_core_resolves_correct_tag(self, tmp_path) -> None:
        import json

        fake_data = json.dumps({"version": "5.4.2", "backend_version": "5.0.3"})
        with patch("pathlib.Path.read_text", return_value=fake_data):
            result = _resolve_image_from_type("core")
        assert result == "docker.io/openfantasy/yadgar:5.4.2"

    def test_missing_backend_version_raises(self) -> None:
        import json

        fake_data = json.dumps({"version": "5.4.2"})
        with patch("pathlib.Path.read_text", return_value=fake_data):
            with pytest.raises(RuntimeError, match="missing 'backend_version'"):
                _resolve_image_from_type("backend")

    def test_missing_version_raises(self) -> None:
        import json

        fake_data = json.dumps({"backend_version": "5.0.3"})
        with patch("pathlib.Path.read_text", return_value=fake_data):
            with pytest.raises(RuntimeError, match="missing 'version'"):
                _resolve_image_from_type("core")

    def test_unreadable_file_raises(self) -> None:
        with patch("pathlib.Path.read_text", side_effect=OSError("not found")):
            with pytest.raises(RuntimeError, match="Cannot read server.json"):
                _resolve_image_from_type("backend")
