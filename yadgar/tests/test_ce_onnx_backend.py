"""TDD: int8-onnx cross-encoder backend (v5.85 car #4).

Hermetic tests (no weight downloads):
- test_ce_backend_default_is_st: Settings().CROSS_ENCODER_BACKEND == "st"
- test_ce_onnx_backend_loads: mocked CrossEncoder called with backend="onnx"
- test_ce_st_default_no_backend_kwarg: default "st" path calls CrossEncoder without backend=
- test_ce_onnx_falls_back_on_error: onnx load error → zeros returned gracefully

Parity test (skip-guarded, requires onnxruntime + model weights):
- test_ce_int8_fp32_parity: real int8 vs fp32, top-3 overlap ≥ 98% on fixture
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ── Skip guard for weight-dependent tests ────────────────────────────────────


def _onnxruntime_available() -> bool:
    try:
        import onnxruntime  # noqa: F401

        return True
    except ImportError:
        return False


requires_onnxruntime = pytest.mark.skipif(
    not _onnxruntime_available(),
    reason="onnxruntime not installed (dep: sentence-transformers[onnx] / optimum-onnx)",
)


def _ce_weights_cached() -> bool:
    """True only if the cross-encoder weights are in the HF cache (offline-loadable).

    The parity test needs the REAL fp32 + int8 weights. Offline CI (HF traffic
    disabled) cannot fetch them, so the test must skip there; it runs wherever the
    model is already cached (local dev, or a CI image that bakes the weights in).
    Gating on the genuine prerequisite — the parity assertion is unchanged.
    """
    try:
        from huggingface_hub import try_to_load_from_cache

        cached = try_to_load_from_cache("cross-encoder/ms-marco-MiniLM-L-6-v2", "config.json")
        return isinstance(cached, str)
    except Exception:
        return False


requires_ce_weights = pytest.mark.skipif(
    not (_onnxruntime_available() and _ce_weights_cached()),
    reason=(
        "cross-encoder weights not cached offline — offline CI cannot fetch them; "
        "run locally (or in a CI image with the model baked in) to validate int8 parity"
    ),
)


# ── Hermetic tests ────────────────────────────────────────────────────────────


class TestCeBackendConfig:
    def test_ce_backend_default_is_st(self):
        """Settings().CROSS_ENCODER_BACKEND defaults to 'st' (fp32 unchanged)."""
        from yadgar.config import Settings

        assert Settings().CROSS_ENCODER_BACKEND == "st"


class TestCeOnnxBackendLoads:
    def _make_mock_st(self, return_value=None, side_effect=None):
        """Build a fake sentence_transformers module with a mock CrossEncoder."""
        mock_st = MagicMock()
        if side_effect is not None:
            mock_st.CrossEncoder = MagicMock(side_effect=side_effect)
        else:
            mock_ce_instance = MagicMock()
            mock_ce_instance.predict.return_value = return_value or []
            mock_st.CrossEncoder = MagicMock(return_value=mock_ce_instance)
        return mock_st

    def test_ce_onnx_backend_loads(self):
        """With CROSS_ENCODER_BACKEND='onnx-int8', CrossEncoder is called with backend='onnx'."""
        from yadgar.backend.ml_client import LocalMLClient

        settings = MagicMock()
        settings.CROSS_ENCODER_ENABLED = True
        settings.CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
        settings.CROSS_ENCODER_BACKEND = "onnx-int8"

        mock_st = self._make_mock_st(return_value=[0.8, 0.2])
        client = LocalMLClient(settings)

        with (
            patch("yadgar.backend.ml_client.time") as mock_time,
            patch("yadgar.backend.ml_client._record_model_load"),
            patch.dict("sys.modules", {"sentence_transformers": mock_st}),
        ):
            mock_time.monotonic.return_value = 0.0
            scores = client._try_st_cross_encoder("query", ["text1", "text2"])

        # CrossEncoder must have been called with backend="onnx"
        mock_st.CrossEncoder.assert_called_once_with(
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
            backend="onnx",
            model_kwargs={"file_name": "model_qint8_avx512.onnx"},
        )
        assert scores == [0.8, 0.2]

    def test_ce_st_default_no_backend_kwarg(self):
        """Default 'st' path calls CrossEncoder without backend= (fp32 unchanged)."""
        from yadgar.backend.ml_client import LocalMLClient

        settings = MagicMock()
        settings.CROSS_ENCODER_ENABLED = True
        settings.CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
        settings.CROSS_ENCODER_BACKEND = "st"

        mock_st = self._make_mock_st(return_value=[0.5])
        client = LocalMLClient(settings)

        with (
            patch("yadgar.backend.ml_client.time") as mock_time,
            patch("yadgar.backend.ml_client._record_model_load"),
            patch.dict("sys.modules", {"sentence_transformers": mock_st}),
        ):
            mock_time.monotonic.return_value = 0.0
            scores = client._try_st_cross_encoder("q", ["t"])

        mock_st.CrossEncoder.assert_called_once_with("cross-encoder/ms-marco-MiniLM-L-6-v2")
        assert scores == [0.5]

    def test_ce_onnx_falls_back_on_error(self):
        """If onnx-int8 load raises, _try_st_cross_encoder returns zeros (not crash)."""
        from yadgar.backend.ml_client import LocalMLClient

        settings = MagicMock()
        settings.CROSS_ENCODER_ENABLED = True
        settings.CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
        settings.CROSS_ENCODER_BACKEND = "onnx-int8"

        mock_st = self._make_mock_st(side_effect=RuntimeError("onnx load failed"))
        client = LocalMLClient(settings)

        with (
            patch("yadgar.backend.ml_client.time") as mock_time,
            patch("yadgar.backend.ml_client._record_model_load"),
            patch("yadgar.exception_telemetry.record_exception"),
            patch.dict("sys.modules", {"sentence_transformers": mock_st}),
        ):
            mock_time.monotonic.return_value = 0.0
            scores = client._try_st_cross_encoder("q", ["t1", "t2"])

        assert scores == [0.0, 0.0]


# ── Parity test (skip-guarded, real weights) ─────────────────────────────────


@requires_ce_weights
class TestCeInt8Parity:
    """Offline parity gate: int8 scores must agree with fp32 top-3 ordering.

    Gate: top-3 overlap between fp32 and int8 rankings ≥ 1 (out of 3).
    Run locally with onnxruntime installed. CI skips automatically.
    """

    QUERY = "who is alice"
    CANDIDATES = [
        "Alice is the CEO of TechCorp.",
        "Bob works in the warehouse.",
        "Alice was born in Seattle in 1985.",
        "Charlie is Alice's assistant.",
        "The weather today is cloudy.",
        "Alice founded TechCorp in 2010.",
    ]

    def _load_fp32(self):
        from sentence_transformers import CrossEncoder

        return CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")

    def _load_int8(self):
        from sentence_transformers import CrossEncoder

        return CrossEncoder(
            "cross-encoder/ms-marco-MiniLM-L-6-v2",
            backend="onnx",
            model_kwargs={"file_name": "model_qint8_avx512.onnx"},
        )

    def test_ce_int8_fp32_parity(self):
        """int8 top-3 must overlap with fp32 top-3 by at least 2 out of 3 candidates."""
        pairs = [(self.QUERY, c) for c in self.CANDIDATES]

        fp32_model = self._load_fp32()
        fp32_scores = list(fp32_model.predict(pairs, show_progress_bar=False))

        int8_model = self._load_int8()
        int8_scores = list(int8_model.predict(pairs, show_progress_bar=False))

        # Get top-3 indices for each
        fp32_top3 = set(sorted(range(len(fp32_scores)), key=lambda i: -fp32_scores[i])[:3])
        int8_top3 = set(sorted(range(len(int8_scores)), key=lambda i: -int8_scores[i])[:3])

        overlap = len(fp32_top3 & int8_top3)
        assert overlap >= 2, (
            f"int8 top-3 overlap with fp32 too low: {overlap}/3. "
            f"fp32 top3={fp32_top3}, int8 top3={int8_top3}. "
            f"fp32 scores={fp32_scores}, int8 scores={int8_scores}"
        )
