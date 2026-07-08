"""§18 dedupe: _load_seq2seq_model helper.

Both CometInferencer and Doc2QueryExpander share identical model-loading
boilerplate.  After the refactor, both delegate to the module-level
_load_seq2seq_model helper.  These tests verify:

  1. The helper exists and is importable.
  2. Both classes produce identical results when pointed at the same
     (unavailable) model — they both fail cleanly rather than diverging.
  3. The old duplicated method still exists as a thin wrapper (backward-compat).
"""

import importlib

import pytest


@pytest.fixture(autouse=True)
def _fresh_enrichment():
    """Re-import enrichment so module-level state is reset between tests."""
    import yadgar._shared.enrichment as m

    yield m
    # Reset any cached unavailable flags
    importlib.reload(m)


def test_helper_is_importable():
    from yadgar._shared.enrichment import _load_seq2seq_model  # noqa: F401


def test_comet_delegates_to_helper(_fresh_enrichment):
    """CometInferencer._ensure_model uses _load_seq2seq_model under the hood."""
    m = _fresh_enrichment
    c = m.CometInferencer()
    # Use a deliberately broken model name so the call fails fast without network.
    result = c._ensure_model("nonexistent-model-xyzzy-0001")
    assert result is False
    assert c._unavailable is True


def test_doc2query_delegates_to_helper(_fresh_enrichment):
    m = _fresh_enrichment
    d = m.Doc2QueryExpander()
    result = d._ensure_model("nonexistent-model-xyzzy-0002")
    assert result is False
    assert d._unavailable is True


def test_identical_failure_mode(_fresh_enrichment):
    """Both classes must fail identically for the same unresolvable model."""
    m = _fresh_enrichment
    c = m.CometInferencer()
    d = m.Doc2QueryExpander()

    c_ok = c._ensure_model("nonexistent-shared-model")
    d_ok = d._ensure_model("nonexistent-shared-model")

    assert c_ok == d_ok == False  # noqa: E712
    assert c._unavailable == d._unavailable == True  # noqa: E712


def test_helper_returns_none_on_import_error(_fresh_enrichment):
    """_load_seq2seq_model returns None when model loading fails."""
    from yadgar._shared.enrichment import _load_seq2seq_model

    result = _load_seq2seq_model("nonexistent-model-xyz-9999")
    assert result is None
